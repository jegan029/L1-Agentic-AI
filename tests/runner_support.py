"""A control plane in remote mode with one runner attached to it, in one process.

The runner here is the real `Runner`, talking to the real application over
HTTP (through an in-memory transport). The control plane and the runner hold
two separate sets of mock adapters: a write is expected to land on the
runner's and never on the control plane's.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.api.app import AppContext
from src.l1_agent.audit.log import Actor
from src.l1_agent.config.settings import AgentSettings, LLMSettings, RunnerSettings, Settings
from src.l1_agent.connectors.catalog import connectors_for_adapters
from src.l1_agent.connectors.hub import ConnectorHub
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from src.l1_agent.demo import build_mock_adapters
from src.l1_agent.runner import keys, protocol
from src.l1_agent.runner import store as runner_store
from src.l1_agent.runner.agent import Runner
from src.l1_agent.runner.ceiling import Ceiling
from src.l1_agent.runner.dispatch import RunnerDispatcher
from tests.api.support import BASE_URL, build
from tests.conftest import Backend

ADMIN = Actor("user", "https://idp.test|admin-1")
# The runner's own allow list for the one write there is.
RUNNER_TARGETS = {"autosys": {"force_start_jobs": ("NIGHTLY_*", "PAYROLL_*")}}


class Tripwire(BaseAdapter):
    """Stands where a connector's target would be in the control plane. Using it is a failure."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.used: List[str] = []

    def _trip(self, what: str) -> Any:
        self.used.append(what)
        raise AssertionError(f"{self._name}.{what} was called in the control plane")

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        return self._trip("execute")

    async def health_check(self) -> bool:
        return self._trip("health_check")

    async def force_start(self, job_name: str) -> AdapterResult:
        return self._trip("force_start")

    @property
    def adapter_name(self) -> str:
        return f"{self._name} (control plane)"


@dataclass
class Keys:
    dispatch_pem: str
    dispatch_public: str
    grant_pem: str
    grant_public: str
    runner_pem: str
    runner_public: str

    @classmethod
    def new(cls) -> "Keys":
        return cls(*keys.generate(), *keys.generate(), *keys.generate())

    @property
    def grant_seed(self) -> bytes:
        return keys.private_seed(self.grant_pem)


@dataclass
class Remote:
    backend: Backend
    tenant: uuid.UUID
    app: FastAPI
    ctx: AppContext
    keys: Keys
    dispatcher: RunnerDispatcher
    hub: ConnectorHub
    control_adapters: Dict[str, "Tripwire"]  # must stay untouched
    runner_adapters: Dict[str, Any]  # where calls land
    identity: str
    runner_id: uuid.UUID
    runner: Optional[Runner] = None
    _http: Optional[httpx.AsyncClient] = None
    _task: Optional[asyncio.Task] = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def db(self):
        return self.backend.db

    def settings(self, **runner: Any) -> Settings:
        values: Dict[str, Any] = dict(
            mode="remote",
            control_plane_url=BASE_URL,
            identity=self.identity,
            dispatch_public_key=self.keys.dispatch_public,
            grant_public_key=self.keys.grant_public,
        )
        values.update(runner)
        return Settings(
            agent=AgentSettings(demo_mode=True),
            llm=LLMSettings(enabled=False),
            runner=RunnerSettings(**values),
        )

    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url=BASE_URL
            )
        return self._http

    def make_runner(
        self,
        *,
        ceiling: Optional[Ceiling] = None,
        cls: type = Runner,
        adapters: Optional[Dict[str, Any]] = None,
        **runner_settings: Any,
    ) -> Runner:
        """A runner for this tenant. By default it may go to T1 on its own target list."""
        return cls(
            self.settings(**runner_settings),
            self.keys.runner_pem,
            http=self.http(),
            adapters=self.runner_adapters if adapters is None else adapters,
            ceiling=ceiling or Ceiling(max_tier="T2", targets=RUNNER_TARGETS),
        )

    def start(self, runner: Optional[Runner] = None) -> Runner:
        """Let a runner work in the background until `stop`."""
        self.runner = runner or self.make_runner()

        async def pump() -> None:
            while not self._stop.is_set():
                try:
                    await self.runner.poll_once(wait_seconds=0.5)
                except Exception:
                    await asyncio.sleep(0.05)

        self._task = asyncio.create_task(pump())
        return self.runner

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.runner is not None:
            await self.runner.close()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def untouched(self) -> bool:
        """True while no connector call has been made in the control plane itself."""
        return all(adapter.used == [] for adapter in self.control_adapters.values())


async def make_remote(
    backend: Backend,
    *,
    tenant: Optional[uuid.UUID] = None,
    connector_settings: Optional[Dict[str, dict]] = None,
    wait_seconds: Optional[float] = None,
) -> Remote:
    """A tenant, the application, and an enrolled runner that has not been started yet."""
    tenant = tenant or await backend.new_tenant("rem")
    app, ctx = await build(backend, tenant)
    made = Keys.new()
    dispatcher = RunnerDispatcher(
        backend.db,
        keys.Signer(keys.private_seed(made.dispatch_pem), protocol.CALL_DOMAIN),
        wait_seconds=wait_seconds,
        poll_seconds=0.02,
    )
    control_adapters = {name: Tripwire(name) for name in build_mock_adapters()}
    hub = ConnectorHub(
        backend.db,
        ctx.settings,
        SecretResolver({}),
        fixed=connectors_for_adapters(control_adapters, connector_settings),
        dispatcher=dispatcher,
    )
    ctx.hub = hub
    ctx.runner_keys = {"dispatch": made.dispatch_public, "grant": made.grant_public}
    runner_id = await runner_store.enrol(backend.db, tenant, ADMIN, "runner-1", made.runner_public)
    return Remote(
        backend=backend,
        tenant=tenant,
        app=app,
        ctx=ctx,
        keys=made,
        dispatcher=dispatcher,
        hub=hub,
        control_adapters=control_adapters,
        runner_adapters=build_mock_adapters(),
        identity=protocol.identity(tenant, runner_id),
        runner_id=runner_id,
    )
