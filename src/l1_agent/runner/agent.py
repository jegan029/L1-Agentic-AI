"""The runner: the process that holds connectors and makes their calls (ADR 0010).

It has no database and listens on no port. It asks the control plane for
calls over one outbound HTTPS connection, makes each one through
`Connector.invoke` and returns the result. Before a call is made it checks,
in this order:

  1. the control plane's signature on the call, and that the call is for this
     runner's tenant, has not expired and has not been seen before;
  2. its own ceiling (`ceiling.py`), which the control plane cannot change;
  3. everything `Connector.invoke` checks: schema, allow lists, and for a
     write a grant signed by the policy engine, good for this call only.

Credentials are read here, from the references in the connection, at the
moment of the call. They are never sent to the control plane.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

from src.l1_agent import __version__
from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.audit.log import digest
from src.l1_agent.config.settings import ConfigError, Settings
from src.l1_agent.connectors.catalog import ADAPTER_CONNECTORS, default_registry
from src.l1_agent.connectors.sdk.base import (
    Connector,
    Refused,
    StepContext,
    WriteNotAuthorized,
)
from src.l1_agent.connectors.sdk.grant import Grant, GrantCheck
from src.l1_agent.connectors.sdk.manifest import Family
from src.l1_agent.connectors.sdk.registry import ConnectorSet, Registry
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, SecretError, SecretResolver
from src.l1_agent.runner import keys, protocol
from src.l1_agent.runner.ceiling import Ceiling
from src.l1_agent.utils.logging import get_logger

logger = get_logger("runner")

SEEN_LIMIT = 10_000
RESULT_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 30.0


class _Verifier:
    """What a connector asks before a write: the local switch, then the grant itself."""

    def __init__(self, check: Optional[GrantCheck], ceiling: Ceiling) -> None:
        self._check = check
        self._ceiling = ceiling

    async def redeem(self, grant: Any, **call: Any) -> None:
        if self._ceiling.kill_switch_on():
            raise WriteNotAuthorized("the runner's kill switch is on")
        if self._check is None:
            raise WriteNotAuthorized("this runner has no grant key, so it makes no write")
        self._check.redeem(grant, **call)


@dataclass
class _Held:
    digest: str
    connectors: ConnectorSet


class Runner:
    def __init__(
        self,
        settings: Settings,
        private_key_pem: str,
        *,
        http: Optional[httpx.AsyncClient] = None,
        resolver: Optional[SecretResolver] = None,
        adapters: Optional[Mapping[str, BaseAdapter]] = None,
        registry: Optional[Registry] = None,
        ceiling: Optional[Ceiling] = None,
        clock=time.time,
    ) -> None:
        runner = settings.runner
        demo = settings.agent.demo_mode
        self._signer = keys.Signer(keys.private_seed(private_key_pem), protocol.REQUEST_DOMAIN)
        # With an identity the runner knows its tenant and makes no call for any other.
        # Without one it names itself by its key, which a control plane that serves one
        # tenant resolves; the calls it is handed then say which tenant they are for.
        self._tenant_id: Optional[uuid.UUID] = None
        self._identity = protocol.key_identity(keys.public_text(self._signer.public_key))
        if runner.identity:
            parsed = protocol.parse_identity(runner.identity)
            if parsed is None:
                raise ConfigError("RUNNER_ID is the identity shown when the runner was enrolled")
            self._tenant_id = parsed[0]
            self._identity = runner.identity
        if not runner.control_plane_url:
            raise ConfigError("RUNNER_CONTROL_PLANE_URL is required")
        if not runner.control_plane_url.lower().startswith("https://") and not demo:
            raise ConfigError("RUNNER_CONTROL_PLANE_URL must start with https://")
        self._url = runner.control_plane_url
        if not runner.dispatch_public_key:
            raise ConfigError("RUNNER_DISPATCH_PUBLIC_KEY is required: whose calls to accept")
        self._dispatch_key = keys.parse_public(runner.dispatch_public_key)
        check = None
        if runner.grant_public_key:
            check = GrantCheck(keys.parse_public(runner.grant_public_key), clock)
        self._ceiling = ceiling or Ceiling.from_settings(runner)
        self._verifier = _Verifier(check, self._ceiling)
        self._writes = check is not None
        self._settings = settings
        self._demo = demo
        self._clock = clock
        self._registry = registry or default_registry()
        self._resolver = resolver or SecretResolver.from_settings(settings.secrets)
        # Mock connectors, keyed by connector id. Demo mode and tests.
        self._mocks: Dict[str, BaseAdapter] = {
            ADAPTER_CONNECTORS.get(key, key): adapter for key, adapter in (adapters or {}).items()
        }
        self._held: Dict[str, _Held] = {}
        self._seen: Dict[str, float] = {}
        self._http = http
        self._owns_http = http is None
        self._concurrency = runner.concurrency

    # ── Talking to the control plane ──────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            runner = self._settings.runner
            verify: Any = runner.verify_ssl
            if runner.verify_ssl and runner.ca_file:
                verify = ssl.create_default_context(cafile=runner.ca_file)
            cert = None
            if runner.client_cert_file:
                cert = (runner.client_cert_file, runner.client_key_file or runner.client_cert_file)
            self._http = httpx.AsyncClient(
                base_url=self._url,
                verify=verify,
                cert=cert,
                follow_redirects=False,
                timeout=httpx.Timeout(protocol.MAX_WAIT_SECONDS + 15.0, connect=10.0),
            )
        return self._http

    async def _post(self, path: str, payload: Dict[str, Any]) -> httpx.Response:
        body = protocol.canonical(payload).encode("ascii")
        timestamp = str(int(self._clock()))
        signature = self._signer.sign(protocol.request_message("POST", path, timestamp, body))
        return await self._client().post(
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                protocol.ID_HEADER: self._identity,
                protocol.TIMESTAMP_HEADER: timestamp,
                protocol.SIGNATURE_HEADER: signature,
            },
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "version": __version__,
            "writes": self._writes,
            "mock": bool(self._mocks),
            **self._ceiling.describe(),
        }

    async def poll_once(self, wait_seconds: float = protocol.MAX_WAIT_SECONDS) -> int:
        """Ask for calls, make them, return their results. Returns how many there were."""
        response = await self._post(
            protocol.CLAIM_PATH,
            {
                "max": self._concurrency,
                "wait_seconds": wait_seconds,
                "nonce": uuid.uuid4().hex,
                "runner": self.describe(),
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"the control plane answered {response.status_code}")
        calls = response.json().get("calls")
        if not isinstance(calls, list):
            raise RuntimeError("the control plane's answer was not understood")
        await asyncio.gather(*(self._handle(item) for item in calls))
        return len(calls)

    async def _handle(self, item: Any) -> None:
        call_id = str(item.get("id", "")) if isinstance(item, dict) else ""
        if not call_id:
            return
        try:
            outcome, result = await self._make(item)
        except Exception as exc:  # the runner itself failed; the caller must still be told
            logger.error("Call %s failed in the runner: %s", call_id, type(exc).__name__)
            outcome, result = protocol.ERROR, {"error": f"runner error: {type(exc).__name__}"}
        await self._return(call_id, outcome, result)

    async def _return(self, call_id: str, outcome: str, result: Dict[str, Any]) -> None:
        delay = 0.5
        for attempt in range(1, RESULT_ATTEMPTS + 1):
            # A new nonce each time: a request is accepted once, so a second attempt that
            # looked the same as the first would be refused as a replay.
            payload = {"outcome": outcome, "result": result, "nonce": uuid.uuid4().hex}
            try:
                response = await self._post(protocol.result_path(call_id), payload)
                if response.status_code < 500:
                    if response.status_code != 200:
                        logger.warning(
                            "Result of call %s was not accepted (%d)", call_id, response.status_code
                        )
                    return
            except httpx.HTTPError as exc:
                logger.warning("Returning call %s failed: %s", call_id, type(exc).__name__)
            if attempt < RESULT_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
        # The control plane will time the call out. For a write it then reads the target
        # to learn what happened; it does not send the write again.
        logger.error("The result of call %s could not be returned", call_id)

    # ── Making one call ───────────────────────────────────────────────

    async def _make(self, item: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        text, signature = item.get("envelope"), item.get("signature")
        if not isinstance(text, str) or not isinstance(signature, str):
            return protocol.REFUSED, {"error": "the call is not in a form this runner accepts"}
        if not keys.verify(
            self._dispatch_key, protocol.CALL_DOMAIN, text.encode("utf-8"), signature
        ):
            return protocol.REFUSED, {"error": "the call was not signed by the control plane"}
        try:
            call = json.loads(text)
        except ValueError:
            return protocol.REFUSED, {"error": "the call could not be read"}
        problem = self._envelope_problem(call, str(item.get("id", "")))
        if problem:
            return protocol.REFUSED, {"error": problem}

        connector_id, kind = call["connector_id"], call["kind"]
        cls = self._registry.connector_class(connector_id)
        if cls is None or cls.manifest.family is not Family.OPERATIONS:
            return protocol.UNKNOWN, {"error": f"this runner has no connector '{connector_id}'"}
        connection = call["connection"]
        refs = {str(k): str(v) for k, v in connection["secret_refs"].items()}

        if kind == protocol.KIND_TEST:
            refused = self._ceiling.refuse_test(connector_id, refs)
            if refused:
                return protocol.REFUSED, {"error": refused}
            connector = await self._connector(cls, connector_id, connection["config"], refs)
            checks = await connector.test_connection()
            return protocol.OK, {"checks": protocol.checks_to_wire(checks)}

        capability_id = call["capability_id"]
        capability = cls.manifest.capability(capability_id)
        if capability is None:
            return protocol.UNKNOWN, {
                "error": f"{connector_id} has no capability '{capability_id}'"
            }
        parameters = call["parameters"]
        refused = self._ceiling.refuse(capability, connector_id, parameters, refs)
        if refused:
            logger.warning("Refused %s: %s", capability_id, refused)
            return protocol.REFUSED, {"error": refused}

        connector = await self._connector(cls, connector_id, connection["config"], refs)
        context = StepContext(
            uuid.UUID(call["tenant_id"]),
            str(call["context"]["run_id"]),
            str(call["context"]["step_id"]),
            str(call["context"]["purpose"]),
        )
        try:
            result = await connector.invoke(
                capability_id,
                parameters,
                context,
                Grant.from_dict(call["grant"]),
                dry_run=kind == protocol.KIND_DRY_RUN,
            )
        except WriteNotAuthorized as exc:
            return protocol.NOT_AUTHORIZED, {"error": str(exc)}
        except Refused as exc:
            return protocol.REFUSED, {"error": str(exc)}
        except LookupError as exc:
            return protocol.UNKNOWN, {"error": str(exc)}
        try:
            return protocol.OK, protocol.result_to_wire(result)
        except ValueError as exc:
            return protocol.ERROR, {"error": str(exc)}

    def _envelope_problem(self, call: Any, claimed_id: str) -> str:
        """Why a signed call is still not one to make, or an empty string."""
        if not isinstance(call, dict) or call.get("v") != 1:
            return "the call is in a version this runner does not know"
        shape = (
            isinstance(call.get("call_id"), str)
            and isinstance(call.get("connector_id"), str)
            and isinstance(call.get("capability_id"), str)
            and isinstance(call.get("parameters"), dict)
            and isinstance(call.get("context"), dict)
            and isinstance(call.get("connection"), dict)
            and isinstance(call["connection"].get("config"), dict)
            and isinstance(call["connection"].get("secret_refs"), dict)
            and isinstance(call.get("expires_at"), (int, float))
            and call.get("kind") in protocol.KINDS
            and all(k in call["context"] for k in ("run_id", "step_id", "purpose"))
        )
        if not shape:
            return "the call is not in a form this runner accepts"
        if call["call_id"] != claimed_id:
            return "the call's id does not match the one it was handed over under"
        try:
            tenant_id = uuid.UUID(str(call.get("tenant_id")))
        except ValueError:
            return "the call does not say which tenant it is for"
        if self._tenant_id is not None and tenant_id != self._tenant_id:
            return "the call is for another tenant"
        now = self._clock()
        if now >= float(call["expires_at"]):
            return "the call has expired"
        if call["call_id"] in self._seen:
            return "the call has already been made by this runner"
        if len(self._seen) >= SEEN_LIMIT:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        self._seen[call["call_id"]] = float(call["expires_at"])
        return ""

    async def _connector(
        self, cls: type, connector_id: str, config: Mapping[str, Any], refs: Mapping[str, str]
    ) -> Connector:
        """The connector for this connection, built once per distinct set of settings."""
        local = self._ceiling.connections.get(connector_id)
        if local is not None:
            config, refs = local.config, local.secret_refs
        key = digest([dict(config), dict(refs)])
        held = self._held.get(connector_id)
        if held is None or held.digest != key:
            if held is not None:
                await held.connectors.close()
            mock = self._mocks.get(connector_id)
            try:
                if mock is not None:
                    built = cls(dict(config), demo_mode=True, fixed=mock)
                else:
                    built = cls(
                        dict(config),
                        ConnectionSecrets(self._resolver, refs),
                        demo_mode=self._demo,
                    )
            except (ConfigError, SecretError) as exc:
                raise Refused(f"the connection's settings are not usable: {exc}") from None
            connectors = ConnectorSet(self._registry, {connector_id: built})
            # A write by this connector is checked against the policy engine's public key.
            connectors.authorise_writes(self._verifier)
            held = self._held[connector_id] = _Held(key, connectors)
        return held.connectors.connector(connector_id)  # type: ignore[return-value]

    # ── The loop ──────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        logger.info(
            "Runner started as %s: max tier %s, writes %s, %d mock connector(s)",
            self._identity,
            self._ceiling.max_tier,
            "possible" if self._writes else "not possible (no grant key)",
            len(self._mocks),
        )
        delay = 1.0
        while not stop.is_set():
            try:
                await self.poll_once()
                delay = 1.0
                continue
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                logger.warning("Asking for work failed: %s", exc or type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)

    async def close(self) -> None:
        for held in self._held.values():
            await held.connectors.close()
        self._held.clear()
        if self._http is not None and self._owns_http:
            await self._http.aclose()


def _mock_adapters(settings: Settings) -> Optional[Mapping[str, BaseAdapter]]:
    if not settings.runner.mock_connectors:
        return None
    from src.l1_agent.demo import build_mock_adapters

    return build_mock_adapters()


async def serve(settings: Settings, stop: Optional[asyncio.Event] = None) -> None:
    """`l1-agent runner run`: work until stopped."""
    resolver = SecretResolver.from_settings(settings.secrets)
    reference = settings.secret_refs.get("RUNNER_PRIVATE_KEY", "")
    pem = await resolver.get(reference) if reference else settings.runner.private_key
    if not pem:
        raise ConfigError("RUNNER_PRIVATE_KEY_REF (or RUNNER_PRIVATE_KEY) is required")
    runner = Runner(settings, pem, resolver=resolver, adapters=_mock_adapters(settings))
    try:
        await runner.run(stop or asyncio.Event())
    finally:
        await runner.close()
