"""The control plane's side of the runner protocol (ADR 0010).

With `RUNNER_MODE=remote` no connector call is made in the control plane.
`RemoteConnectorSet` has the same shape as a `ConnectorSet`, so the engine and
the action pipeline use it without knowing; its `invoke` signs the call, puts
it in the queue and waits for a runner's answer.

What the control plane still does itself: check the parameters against the
capability's schema and the tenant's allow lists before a call is queued
(`refusals`), and read the kill switches again before a queued write is
handed to a runner (`hand_out`).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from src.l1_agent.actions import store as action_store
from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.audit import log as audit
from src.l1_agent.connectors.sdk.base import (
    PURPOSE_TEST,
    CheckResult,
    Connector,
    OutcomeUnknown,
    Refused,
    StepContext,
    WriteNotAuthorized,
)
from src.l1_agent.connectors.sdk.grant import Grant
from src.l1_agent.connectors.sdk.manifest import Kind
from src.l1_agent.connectors.sdk.registry import ConnectorSet, Registry
from src.l1_agent.db.engine import Database, utcnow
from src.l1_agent.runner import protocol, store
from src.l1_agent.runner.keys import Signer
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics
from src.l1_agent.utils.retry import NotRetryable

logger = get_logger("runner.dispatch")

# Added to a capability's own timeout: time for a runner to pick the call up and answer.
MARGIN_SECONDS = 20.0
POLL_SECONDS = 0.2


class RunnerUnavailable(NotRetryable, RuntimeError):
    """No answer came from a runner. For a read: nothing is known and nothing was changed."""


class RunnerDispatcher:
    def __init__(
        self,
        db: Database,
        signer: Signer,
        *,
        margin_seconds: float = MARGIN_SECONDS,
        wait_seconds: Optional[float] = None,
        poll_seconds: float = POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db = db
        self._signer = signer
        self._margin = margin_seconds
        # A fixed wait for every call, in place of each capability's own. For tests.
        self._wait = wait_seconds
        self._poll = poll_seconds
        self._sleep = sleep

    @property
    def public_key(self) -> bytes:
        return self._signer.public_key

    async def call(
        self,
        tenant_id: uuid.UUID,
        *,
        kind: str,
        connector: Connector,
        capability_id: str = "",
        parameters: Optional[Mapping[str, Any]] = None,
        context: Optional[StepContext] = None,
        grant: Optional[Grant] = None,
        is_write: bool = False,
        timeout_seconds: float = 60.0,
    ) -> Any:
        """Queue one call and wait for its answer: the queue row, with outcome and result."""
        call_id = uuid.uuid4()
        wait = self._wait if self._wait is not None else float(timeout_seconds) + self._margin
        issued = time.time()
        text = protocol.envelope(
            call_id=call_id,
            tenant_id=tenant_id,
            kind=kind,
            connector_id=connector.id,
            capability_id=capability_id,
            parameters=parameters or {},
            run_id=context.run_id if context else "",
            step_id=context.step_id if context else "",
            purpose=context.purpose if context else PURPOSE_TEST,
            config=connector.config,
            secret_refs=connector.secrets.references,
            grant=grant.to_dict() if grant else None,
            issued_at=issued,
            expires_at=issued + wait,
        )
        await store.enqueue(
            self._db,
            tenant_id,
            call_id=call_id,
            kind=kind,
            connector_id=connector.id,
            capability_id=capability_id,
            is_write=is_write,
            envelope=text,
            signature=self._signer.sign(text.encode("ascii")),
            deadline=utcnow() + timedelta(seconds=wait),
        )
        metrics.increment("runner.calls", labels={"kind": kind})
        ends = time.monotonic() + wait
        try:
            while True:
                row = await store.answer(self._db, tenant_id, call_id)
                if row is not None:
                    await store.remove(self._db, tenant_id, call_id)
                    return row
                if time.monotonic() >= ends:
                    break
                await self._sleep(self._poll)
        except asyncio.CancelledError:
            # The caller is going away (a worker shutting down). A call nobody has taken
            # yet must not be made after it has gone.
            await asyncio.shield(store.cancel(self._db, tenant_id, call_id))
            raise

        metrics.increment("runner.calls_unanswered", labels={"kind": kind})
        if await store.cancel(self._db, tenant_id, call_id):
            await store.remove(self._db, tenant_id, call_id)
            raise RunnerUnavailable(
                "No runner took the call in time; it was withdrawn and was not made"
            )
        # A runner holds it. One last look, in case the answer arrived just now.
        row = await store.answer(self._db, tenant_id, call_id)
        if row is not None:
            await store.remove(self._db, tenant_id, call_id)
            return row
        if is_write:
            raise OutcomeUnknown(
                "A runner took the write and returned no result in time; "
                "whether it was made is not known"
            )
        raise RunnerUnavailable("A runner took the call and did not answer in time")


def _raise_for(outcome: str, error: str) -> None:
    """Turn a runner's refusal back into the exception the connector itself would raise."""
    if outcome == protocol.REFUSED:
        raise Refused(error or "the runner refused the call")
    if outcome == protocol.NOT_AUTHORIZED:
        raise WriteNotAuthorized(error or "the runner did not accept the grant")
    if outcome == protocol.UNKNOWN:
        raise LookupError(error or "the runner does not have this capability")


class RemoteConnectorSet:
    """A tenant's connectors as the engine sees them when a runner makes the calls.

    The connectors in `local` are never invoked here. They carry the manifest
    and the connection's settings, which is what `refusals` checks and what
    is put in the envelope.
    """

    def __init__(
        self, local: ConnectorSet, dispatcher: RunnerDispatcher, tenant_id: uuid.UUID
    ) -> None:
        self._local = local
        self._dispatcher = dispatcher
        self._tenant_id = tenant_id

    @property
    def registry(self) -> Registry:
        return self._local.registry

    def connector(self, connector_id: str) -> Optional[Connector]:
        return self._local.connector(connector_id)

    def connectors(self) -> Dict[str, Connector]:
        return self._local.connectors()

    def authorise_writes(self, verifier: Any) -> None:
        """Nothing to attach: the runner checks each grant itself, with the public key."""

    def tool_name(self, capability_id: str) -> str:
        return self._local.tool_name(capability_id)

    def refusals(self, capability_id: str, parameters: Mapping[str, Any]) -> List[str]:
        return self._local.refusals(capability_id, parameters)

    async def invoke(
        self,
        capability_id: str,
        parameters: Mapping[str, Any],
        context: Optional[StepContext],
        grant: Any = None,
        *,
        dry_run: bool = False,
    ) -> AdapterResult:
        owner = self.registry.owner(capability_id)
        capability = self.registry.capability(capability_id)
        if owner is None or capability is None:
            raise LookupError(f"Unknown capability '{capability_id}'")
        connector = self._local.connector(owner)
        if connector is None:
            raise LookupError(f"Connector '{owner}' is not configured")
        if context is None:
            raise PermissionError("A capability can only be called with a step context")
        if context.tenant_id != self._tenant_id:
            raise PermissionError("The step belongs to another tenant")
        is_write = capability.kind is Kind.WRITE
        if is_write and not dry_run and not isinstance(grant, Grant):
            raise WriteNotAuthorized(
                f"'{capability_id}' is a write and needs a grant from the policy engine"
            )
        problems = self._local.refusals(capability_id, parameters)
        if problems:
            raise Refused("; ".join(problems))
        row = await self._dispatcher.call(
            self._tenant_id,
            kind=protocol.KIND_DRY_RUN if dry_run else protocol.KIND_INVOKE,
            connector=connector,
            capability_id=capability_id,
            parameters=parameters,
            context=context,
            grant=grant if is_write and not dry_run else None,
            is_write=is_write and not dry_run,
            timeout_seconds=capability.timeout_seconds,
        )
        result = row.result or {}
        _raise_for(row.outcome, str(result.get("error") or ""))
        if row.outcome != protocol.OK:
            return AdapterResult(
                success=False,
                error=str(result.get("error") or "the runner could not make the call"),
            )
        return protocol.result_from_wire(result)

    async def test(self, connector_id: str) -> List[CheckResult]:
        """Ask the runner to test one connection from where the target is reachable."""
        connector = self._local.connector(connector_id)
        if connector is None:
            return [CheckResult("connection", False, "not configured")]
        try:
            row = await self._dispatcher.call(
                self._tenant_id, kind=protocol.KIND_TEST, connector=connector, timeout_seconds=30.0
            )
        except RunnerUnavailable as exc:
            return [CheckResult("reach the runner", False, str(exc))]
        result = row.result or {}
        if row.outcome != protocol.OK:
            return [CheckResult("runner", False, str(result.get("error") or row.outcome))]
        checks = result.get("checks")
        if not isinstance(checks, list) or not checks:
            return [CheckResult("runner", False, "the runner returned no checks")]
        return [
            CheckResult(str(c.get("name", "")), c.get("ok") is True, str(c.get("detail", "")))
            for c in checks
            if isinstance(c, dict)
        ]

    async def close(self) -> None:
        await self._local.close()


async def hand_out(db: Database, runner: store.RunnerIdentity, limit: int) -> List[Dict[str, str]]:
    """The calls a runner may make now.

    A write that was queued before a kill switch was engaged is not handed
    out: the switches are read here, from the database, every time.
    """
    claimed = await store.claim(db, runner, limit)
    if not claimed:
        return []
    writes = [c for c in claimed if c.is_write]
    stopped = await action_store.switches_on(db, runner.tenant_id) if writes else ()
    out: List[Dict[str, str]] = []
    for call in claimed:
        if call.is_write and stopped:
            metrics.increment("actions.refused_by_kill_switch")
            await store.record_result(
                db,
                runner.tenant_id,
                call.id,
                protocol.NOT_AUTHORIZED,
                {"error": "kill switch is on: " + ", ".join(stopped)},
            )
            continue
        if call.is_write:
            await _record_hand_over(db, runner, call)
        out.append({"id": str(call.id), "envelope": call.envelope, "signature": call.signature})
    return out


async def _record_hand_over(
    db: Database, runner: store.RunnerIdentity, call: store.Claimed
) -> None:
    """Which runner was given a write, in the run's audit trail."""
    body = json.loads(call.envelope)
    async with db.tenant(runner.tenant_id) as conn:
        await audit.append(
            conn,
            runner.tenant_id,
            runner.actor,
            "runner.write_handed_over",
            {
                "call_id": str(call.id),
                "runner_id": str(runner.id),
                "capability": body.get("capability_id", ""),
                "step_id": (body.get("context") or {}).get("step_id", ""),
                "envelope_sha256": audit.digest(call.envelope),
            },
            correlation_id=str((body.get("context") or {}).get("run_id", "")),
        )
