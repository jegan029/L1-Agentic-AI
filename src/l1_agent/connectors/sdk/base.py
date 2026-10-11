"""Connector base class (ADR 0005).

`invoke` is the only way into a connector. Before any connector code runs it
checks that the capability is declared, that the caller supplied a step
context, that the parameters satisfy the capability's closed schema and that
every allow-listed parameter has something to be checked against. A write
capability also needs a grant, which only the policy engine mints after it
has recorded a decision (ADR 0008). A connector that has not been given a
grant verifier refuses every write.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, ClassVar, Dict, List, Mapping, Optional

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import ConfigError
from src.l1_agent.connectors.sdk.grant import GrantVerifier, call_digest
from src.l1_agent.connectors.sdk.manifest import Capability, Kind, Manifest, validate_parameters
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, SecretError, SecretResolver
from src.l1_agent.utils.logging import get_logger

logger = get_logger("connector")

PURPOSE_RUN = "run"
PURPOSE_TEST = "test_connection"


@dataclass(frozen=True)
class StepContext:
    """Why a capability is being called. A read cannot be made without one."""

    tenant_id: Optional[uuid.UUID]
    run_id: str
    step_id: str
    purpose: str = PURPOSE_RUN


class WriteNotAuthorized(RuntimeError):
    """A write capability was invoked without an action grant from the policy engine."""


class Refused(ValueError):
    """The parameters break the capability's schema or an allow list. Nothing was called."""


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Health:
    status: HealthStatus
    reason: str = ""


@dataclass(frozen=True)
class CheckResult:
    """One line of `test_connection`: a scope or step, and whether it worked."""

    name: str
    ok: bool
    detail: str = ""


def no_secrets() -> ConnectionSecrets:
    return ConnectionSecrets(SecretResolver({}), {})


class Connector(ABC):
    manifest: ClassVar[Manifest]

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        secrets: Optional[ConnectionSecrets] = None,
        *,
        demo_mode: bool = False,
        check_config: bool = True,
    ) -> None:
        self.config: Dict[str, Any] = dict(config or {})
        self.secrets = secrets or no_secrets()
        self.demo_mode = demo_mode
        if self.manifest.config and check_config:
            problems = validate_parameters(
                self.manifest.config, self.config, subject=f"connection '{self.manifest.id}'"
            )
            if problems:
                raise ConfigError("; ".join(problems))
        self._seen: Dict[str, str] = {}
        # Set by the engine for a run. Without one, no write can be made.
        self.grant_verifier: Optional[GrantVerifier] = None

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def tool_name(self) -> str:
        return self.manifest.display_name

    # ── The entry point ───────────────────────────────────────────────

    async def invoke(
        self,
        capability_id: str,
        parameters: Mapping[str, Any],
        context: Optional[StepContext],
        grant: Any = None,
        *,
        dry_run: bool = False,
    ) -> AdapterResult:
        capability = self.manifest.capability(capability_id)
        if capability is None:
            raise LookupError(f"{self.id} has no capability '{capability_id}'")
        if context is None:
            raise PermissionError("A capability can only be called with a step context")
        if capability.kind is Kind.WRITE:
            return await self._invoke_write(capability, parameters, context, grant, dry_run)
        problems = self.refusals(capability_id, parameters)
        if problems:
            raise Refused("; ".join(problems))

        try:
            result = await self._execute(capability, dict(parameters))
            if not result.success and await self._credentials_changed():
                # The credential was rotated since it was read. Reads are safe to repeat.
                logger.info("%s: credential changed; repeating the call once", self.id)
                result = await self._execute(capability, dict(parameters))
        except SecretError as exc:
            return AdapterResult(success=False, error=f"credential unavailable: {exc}")
        return result

    async def _invoke_write(
        self,
        capability: Capability,
        parameters: Mapping[str, Any],
        context: StepContext,
        grant: Any,
        dry_run: bool,
    ) -> AdapterResult:
        """A write, or its dry run. The write itself is made only against a redeemed grant."""
        if not dry_run and (grant is None or self.grant_verifier is None):
            raise WriteNotAuthorized(
                f"'{capability.id}' is a write and needs a grant from the policy engine"
            )
        problems = self.refusals(capability.id, parameters)
        if problems:
            raise Refused("; ".join(problems))
        try:
            if dry_run:
                if not capability.supports_dry_run:
                    raise Refused(f"'{capability.id}' has no dry run")
                return await self._dry_run(capability, dict(parameters))
            # Redeemed last, so that a refused call does not use the grant up, and
            # immediately before the call, so that nothing can come between them.
            await self.grant_verifier.redeem(
                grant,
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                step_id=context.step_id,
                capability_id=capability.id,
                digest=call_digest(capability.id, parameters),
            )
            # A write is never repeated here: whether it is safe to is not this layer's call.
            return await self._execute_write(capability, dict(parameters))
        except SecretError as exc:
            return AdapterResult(success=False, error=f"credential unavailable: {exc}")

    def refusals(self, capability_id: str, parameters: Mapping[str, Any]) -> List[str]:
        """Why this call would be refused before it is made. Empty means it may proceed."""
        capability = self.manifest.capability(capability_id)
        if capability is None:
            return [f"{self.id} has no capability '{capability_id}'"]
        problems = validate_parameters(capability.parameters, parameters, subject=capability_id)
        return problems + self._allow_list_problems(capability, parameters)

    def _allow_list_problems(
        self, capability: Capability, parameters: Mapping[str, Any]
    ) -> List[str]:
        problems: List[str] = []
        for binding in capability.allow_lists:
            value = parameters.get(binding.parameter)
            if value in (None, ""):
                continue
            allowed = self.config.get(binding.config_key) or []
            if not allowed:
                problems.append(
                    f"parameter '{binding.parameter}' is refused: allow list "
                    f"'{binding.config_key}' is empty"
                )
            elif binding.match == "exact" and str(value) not in {str(a) for a in allowed}:
                problems.append(
                    f"parameter '{binding.parameter}' is not on allow list '{binding.config_key}'"
                )
            elif binding.match == "pattern" and not any(
                fnmatchcase(str(value), str(a)) for a in allowed
            ):
                problems.append(
                    f"parameter '{binding.parameter}' is not on allow list '{binding.config_key}'"
                )
        return problems

    # ── Credentials ───────────────────────────────────────────────────

    async def secret(self, slot: str, *, fresh: bool = False) -> str:
        """A credential value for this call. Remembers a digest, never the value."""
        value = await self.secrets.get(slot, fresh=fresh)
        self._seen[slot] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return value

    async def _credentials_changed(self) -> bool:
        """True when the backend now holds a different value from the one last used."""
        before = dict(self._seen)
        if not before:
            return False
        for slot in before:
            await self.secret(slot, fresh=True)
        return self._seen != before

    # ── For subclasses ────────────────────────────────────────────────

    @abstractmethod
    async def _execute(self, capability: Capability, parameters: Dict[str, Any]) -> AdapterResult:
        """Run a validated read capability."""

    async def _execute_write(
        self, capability: Capability, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Make a validated, granted write. Called once per grant."""
        raise NotImplementedError(f"{self.id} does not implement '{capability.id}'")

    async def _dry_run(self, capability: Capability, parameters: Dict[str, Any]) -> AdapterResult:
        """Ask the target whether the write would be accepted, changing nothing."""
        raise NotImplementedError(f"{self.id} has no dry run for '{capability.id}'")

    @abstractmethod
    async def health(self) -> Health:
        """Reachability of the target. An error from the target is never 'healthy'."""

    async def test_connection(self) -> List[CheckResult]:
        """Exercise authentication and each required scope. Default: the health check."""
        try:
            health = await self.health()
        except SecretError as exc:
            return [CheckResult("credentials", False, str(exc))]
        except Exception as exc:
            return [CheckResult("reach the target", False, type(exc).__name__)]
        return [
            CheckResult("reach the target", health.status is HealthStatus.HEALTHY, health.reason)
        ]

    async def close(self) -> None:
        return None


@dataclass
class _Built:
    adapter: BaseAdapter
    digest: str = ""
    seen: Dict[str, str] = field(default_factory=dict)


class AdapterConnector(Connector):
    """A connector that carries out its capabilities through one of the original adapters.

    The adapter is built from the connection's settings and its current
    credentials, and rebuilt when a credential changes. `fixed` supplies a
    ready adapter instead: the mocks used by demo mode and by tests.
    """

    # capability id -> parameters merged in before the adapter is called
    defaults: ClassVar[Mapping[str, Mapping[str, Any]]] = {}

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        secrets: Optional[ConnectionSecrets] = None,
        *,
        demo_mode: bool = False,
        fixed: Optional[BaseAdapter] = None,
    ) -> None:
        # A ready adapter (a mock) has no connection settings to check.
        super().__init__(config, secrets, demo_mode=demo_mode, check_config=fixed is None)
        self._fixed = fixed
        self._built: Optional[_Built] = None

    @property
    def tool_name(self) -> str:
        if self._fixed is not None:
            return self._fixed.adapter_name
        return self._built.adapter.adapter_name if self._built else self.manifest.display_name

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        raise NotImplementedError(f"{self.id} has no real adapter")

    async def _adapter(self) -> BaseAdapter:
        if self._fixed is not None:
            return self._fixed
        values = {slot: await self.secret(slot) for slot in self.secrets.slots}
        digest = hashlib.sha256(repr(sorted(self._seen.items())).encode("utf-8")).hexdigest()
        if self._built is None or self._built.digest != digest:
            await self._close_built()
            self._built = _Built(self._build(values), digest)
        return self._built.adapter

    async def _execute(self, capability: Capability, parameters: Dict[str, Any]) -> AdapterResult:
        adapter = await self._adapter()
        return await adapter.execute({**self.defaults.get(capability.id, {}), **parameters})

    async def health(self) -> Health:
        try:
            adapter = await self._adapter()
            ok = await adapter.health_check()
        except SecretError as exc:
            return Health(HealthStatus.UNAVAILABLE, f"credential unavailable: {exc}")
        except Exception as exc:
            return Health(HealthStatus.UNAVAILABLE, type(exc).__name__)
        if ok:
            return Health(HealthStatus.HEALTHY)
        return Health(HealthStatus.UNAVAILABLE, "the health check failed")

    async def _close_built(self) -> None:
        closer = getattr(self._built.adapter, "close", None) if self._built else None
        if closer:
            try:
                await closer()
            except Exception as exc:
                logger.warning(
                    "%s: closing the old adapter failed: %s", self.id, type(exc).__name__
                )

    async def close(self) -> None:
        await self._close_built()
        self._built = None
