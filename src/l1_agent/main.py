"""Service assembly: HTTP listener, ServiceNow poller and run workers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI

from src.l1_agent.actions.service import ActionService
from src.l1_agent.ai.gateway import ModelGateway
from src.l1_agent.ai.providers import Provider, build_provider
from src.l1_agent.api.app import AppContext, create_app
from src.l1_agent.api.security import demo_open, validate_security_config, webhook_open
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth.oidc import OidcClient
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import ConfigError, Settings
from src.l1_agent.connectors.catalog import connectors_for_adapters
from src.l1_agent.connectors.hub import ConnectorHub, demo_connectors, servicenow_secrets
from src.l1_agent.connectors.itsm.handle import ItsmHandle, check_servicenow_config
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from src.l1_agent.db.engine import Database
from src.l1_agent.demo import (
    MockServiceNowClient,
    build_mock_adapters,
    seed_demo_model_policy,
    seed_demo_policy,
    seed_demo_sops,
)
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.runner import protocol
from src.l1_agent.runner.dispatch import RunnerDispatcher
from src.l1_agent.runner.keys import Signer, private_seed, public_of, public_text
from src.l1_agent.service.runs import DbEventBus, RunService
from src.l1_agent.sop import ingest
from src.l1_agent.utils.logging import get_logger, setup_logging
from src.l1_agent.utils.retry import CircuitBreaker
from src.l1_agent.utils.secrets import SecretsProvider

logger = get_logger("main")

POLLER = Actor.system("poller")


class L1AgentService:
    """Builds the engine and, once `setup` has run, the HTTP application around it."""

    def __init__(self, settings: Settings) -> None:
        validate_security_config(settings)
        self._settings = settings

        # Demo mode has no ServiceNow: notes and updates go to an in-memory stand-in.
        # SERVICENOW_LIVE_IN_DEMO connects demo mode to a real instance for testing.
        # Credentials given as references are read when needed, so a rotation takes effect.
        self._resolver = SecretResolver.from_settings(settings.secrets)
        self._live_servicenow = not settings.agent.demo_mode or settings.servicenow.live_in_demo
        snow = settings.servicenow
        if not self._live_servicenow:
            self._snow_client: Any = MockServiceNowClient()
        elif snow.base_url or snow.username or snow.assignment_group or snow.oauth_client_id:
            itsm_secrets = servicenow_secrets(settings, self._resolver)
            check_servicenow_config(snow, itsm_secrets.slots if itsm_secrets else ())
            self._snow_client = ServiceNowClient(
                snow, itsm_secrets, demo_mode=settings.agent.demo_mode
            )
        else:
            # Nothing in the environment. The connection may have been entered for the
            # tenant instead (connectors/itsm/handle.py); until it is, no incident is taken.
            self._snow_client = None
        # What the engine talks to. Set in `setup`, once the tenant is known.
        self._itsm: Any = self._snow_client
        self._hub: Optional[ConnectorHub] = None

        # The model provider this deployment has, if any (ADR 0009). It only advises,
        # and whether a tenant uses it at all is that tenant's model policy.
        key_ref = settings.secret_refs.get("LLM_API_KEY", "")

        async def api_key() -> str:
            return await self._resolver.get(key_ref)

        self._provider: Optional[Provider] = build_provider(settings, api_key if key_ref else None)
        self._model: Optional[ModelGateway] = None

        self._db: Optional[Database] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._ctx: Optional[AppContext] = None
        self._stop = asyncio.Event()

    # ── Engine ────────────────────────────────────────────────────────

    def _build_processor(
        self, db: Database, hub: ConnectorHub, actions: ActionService, model: ModelGateway
    ) -> IncidentProcessor:
        settings = self._settings
        agent = settings.agent
        events = DbEventBus(db)
        return IncidentProcessor(
            snow_client=self._itsm,  # type: ignore[arg-type]
            sop_matcher=SOPMatcher(agent.sop_confidence_threshold),
            sop_parser=None,
            executor=SOPExecutor(
                connectors=hub,
                breaker_factory=lambda: CircuitBreaker(
                    failure_threshold=agent.circuit_breaker_failure_threshold,
                    reset_timeout_seconds=agent.circuit_breaker_reset_seconds,
                ),
                retry_max_attempts=agent.retry_max_attempts,
                retry_base_delay=agent.retry_base_delay_seconds,
                event_bus=events,
                actions=actions,
            ),
            confidence_threshold=agent.sop_confidence_threshold,
            advisors=model if model.provider is not None else None,
            event_bus=events,
            l2_router=L2Router(Path(agent.l2_routing_path))
            if agent.l2_routing_path
            else L2Router(),
        )

    # ── Set-up ────────────────────────────────────────────────────────

    async def setup(self) -> AppContext:
        """Connect to the database and build everything that depends on it."""
        if self._ctx is not None:
            return self._ctx
        settings = self._settings
        demo = settings.agent.demo_mode
        db = Database.create(settings.database.url, demo_mode=demo)
        self._db = db
        if db.is_postgres:
            # Tenant isolation depends on the role this process connects as.
            await db.check_role_is_safe()
            tenant_id = await db.tenant_id_for(settings.database.tenant_slug)
            if tenant_id is None:
                raise ConfigError(
                    f"Tenant '{settings.database.tenant_slug}' does not exist. Create it with "
                    "`l1-agent tenant create` using the migration role."
                )
        else:
            await db.create_schema_for_demo()
            tenant_id = await db.ensure_tenant(settings.database.tenant_slug, "Demo tenant")
        if demo and settings.agent.demo_seed:
            # Demo mode starts with the sample SOPs published and one waiting for review.
            await seed_demo_sops(db, tenant_id)
            await seed_demo_policy(db, tenant_id)
            await seed_demo_model_policy(db, tenant_id)
            logger.info("Demo mode: mock connectors and the sample SOPs")

        self._itsm = self._snow_client
        if self._live_servicenow:
            self._itsm = ItsmHandle(
                db, tenant_id, settings, self._resolver, lambda: self._snow_client
            )
            if await self._itsm.current() is None:
                logger.warning(
                    "No usable ServiceNow connection (%s): no incident is taken until one is "
                    "entered in the connector set-up screen or given in the environment",
                    await self._itsm.reason(),
                )
        dispatcher, grant_key, runner_keys = await self._runner_side(db)
        self._hub = ConnectorHub(
            db,
            settings,
            self._resolver,
            # In demo mode the mocks stand in for the targets. With a runner they are the
            # runner's to hold (RUNNER_MOCK_CONNECTORS), and connections are entered as usual.
            fixed=demo_connectors(
                settings, self._resolver, connectors_for_adapters(build_mock_adapters())
            )
            if demo and dispatcher is None
            else None,
            dispatcher=dispatcher,
        )
        # Every write goes through this: policy, approvals, change record, kill switch.
        actions = ActionService(db, self._itsm, settings.actions, grant_key=grant_key)
        # Every model request goes through this: model policy, redaction, budgets, audit.
        self._model = ModelGateway(db, self._provider, timeout_seconds=settings.llm.timeout_seconds)
        if self._provider is not None:
            logger.info(
                "Model provider configured: %s (%s)", self._provider.id, self._provider.model
            )
        runs = RunService(
            db,
            self._build_processor(db, self._hub, actions, self._model),
            actions=actions,
            visibility_timeout_seconds=settings.worker.visibility_timeout_seconds,
            max_attempts=settings.worker.max_attempts,
            sops_from_store=True,
        )
        oidc = saml = None
        if settings.auth.oidc_issuer:
            self._http = httpx.AsyncClient(timeout=10.0, follow_redirects=False)
            oidc = OidcClient(settings.auth, self._http, allow_http=demo)
        if settings.auth.saml_idp_metadata_path:
            from src.l1_agent.auth.saml import SamlProvider

            saml = SamlProvider(settings.auth, allow_http=demo)
        self._ctx = AppContext(
            settings=settings,
            db=db,
            runs=runs,
            tenant_id=tenant_id,
            oidc=oidc,
            saml=saml,
            demo_open=demo_open(settings),
            webhook_open=webhook_open(settings),
            hub=self._hub,
            itsm=self._itsm,
            actions=actions,
            model=self._model,
            runner_keys=runner_keys,
        )
        return self._ctx

    async def _runner_side(self, db: Database):
        """With RUNNER_MODE=remote: what signs calls and grants for the runner (ADR 0010).

        Returns (dispatcher, grant key, the public keys a runner is configured with).
        """
        settings = self._settings
        if settings.runner.mode != "remote":
            return None, None, {}

        async def pem(name: str, value: str) -> str:
            reference = settings.secret_refs.get(name, "")
            return await self._resolver.get(reference) if reference else value

        dispatch_pem = await pem("RUNNER_DISPATCH_KEY", settings.runner.dispatch_key)
        if not dispatch_pem:
            raise ConfigError(
                "RUNNER_MODE=remote needs RUNNER_DISPATCH_KEY_REF: the key calls to the runner "
                "are signed with. Make one with `l1-agent runner keygen`."
            )
        signer = Signer(private_seed(dispatch_pem), protocol.CALL_DOMAIN)
        public = {"dispatch": public_text(signer.public_key), "grant": ""}
        grant_pem = await pem("ACTIONS_GRANT_KEY", settings.runner.grant_key)
        grant_key = private_seed(grant_pem) if grant_pem else None
        if grant_key is None:
            # Workers hold the grant key. A process without it (the API tier) mints nothing
            # a runner would accept.
            logger.warning(
                "RUNNER_MODE=remote without ACTIONS_GRANT_KEY_REF: no write this process "
                "authorises will be accepted by a runner"
            )
        else:
            public["grant"] = public_text(public_of(grant_key))
        logger.info("Connector calls are made by a runner (RUNNER_MODE=remote)")
        return RunnerDispatcher(db, signer), grant_key, public

    async def create_app(self) -> FastAPI:
        return create_app(await self.setup())

    async def close(self) -> None:
        if isinstance(self._itsm, ItsmHandle):
            await self._itsm.close()  # the stored connection and the environment's
        elif self._snow_client is not None:
            await self._snow_client.close()
        if self._provider is not None:
            await self._provider.close()
        if self._http is not None:
            await self._http.aclose()
        if self._hub is not None:
            await self._hub.close()
        if self._db is not None:
            await self._db.close()

    # ── Background work ───────────────────────────────────────────────

    async def _poll_loop(self, ctx: AppContext) -> None:
        """Poll ServiceNow for new incidents and queue each one once."""
        interval = self._settings.servicenow.poll_interval_seconds
        logger.info("Starting poll loop (interval=%ds)", interval)
        while not self._stop.is_set():
            try:
                source = self._snow_client
                if isinstance(self._itsm, ItsmHandle):
                    source = await self._itsm.current()
                if source is None:
                    incidents = []  # no ITSM connection yet
                else:
                    incidents = await source.get_new_incidents()
                for incident in incidents:
                    result = await ctx.runs.ingest(ctx.tenant_id, incident, POLLER)
                    if result.accepted:
                        logger.info("Polled incident %s queued", incident.number)
            except Exception as exc:
                logger.error("Poll cycle error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _knowledge_loop(self, ctx: AppContext) -> None:
        """Pull changed articles from the knowledge sources and add them as drafts."""
        interval = self._settings.knowledge.sync_interval_seconds
        logger.info("Starting knowledge sync (interval=%ds)", interval)
        while not self._stop.is_set():
            try:
                sources = await ctx.hub.knowledge_sources(ctx.tenant_id, ctx.itsm)
                for result in await ingest.sync_all(
                    ctx.db,
                    ctx.tenant_id,
                    sources,
                    drafter=ingest.model_drafter(ctx.model, ctx.tenant_id, ctx.hub.registry),
                    registry=ctx.hub.registry,
                ):
                    if result.drafts or result.errors:
                        logger.info("Knowledge sync: %s", result.to_dict())
            except Exception as exc:
                logger.error("Knowledge sync error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def start_workers(self, ctx: AppContext) -> List[asyncio.Task]:
        worker = self._settings.worker
        return [
            asyncio.create_task(ctx.runs.run_worker(self._stop, worker.poll_interval_seconds))
            for _ in range(max(1, self._settings.agent.max_concurrent_incidents))
        ]

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Serve HTTP, and run the poller and the embedded workers, until stopped."""
        settings = self._settings
        setup_logging(settings.agent.log_level)
        logger.info("L1 Agent Service starting (demo_mode=%s)", settings.agent.demo_mode)
        ctx = await self.setup()
        app = create_app(ctx)

        tasks: List[asyncio.Task] = []
        if settings.worker.embedded:
            tasks += self.start_workers(ctx)
        if self._live_servicenow:
            tasks.append(asyncio.create_task(self._poll_loop(ctx)))
        if settings.knowledge.sync_interval_seconds > 0 and not settings.agent.demo_mode:
            tasks.append(asyncio.create_task(self._knowledge_loop(ctx)))

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.agent.webhook_host,
                port=settings.agent.webhook_port,
                log_config=None,
                access_log=False,
                server_header=False,
                ssl_certfile=settings.agent.tls_cert_file or None,
                ssl_keyfile=settings.agent.tls_key_file or None,
            )
        )
        try:
            await server.serve()  # returns on SIGINT or SIGTERM
        finally:
            logger.info("Shutting down...")
            self._stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close()

    async def run_worker_only(self) -> None:
        """A worker process with no listener: `l1-agent worker`."""
        setup_logging(self._settings.agent.log_level)
        ctx = await self.setup()
        try:
            await asyncio.gather(*self.start_workers(ctx))
        finally:
            await self.close()


def load_settings() -> Settings:
    # Secrets go straight into the settings object, never into os.environ.
    return Settings.from_env(secrets=SecretsProvider.from_env().fetch())


def main() -> None:
    """Entry point for the L1 Agent Service."""
    asyncio.run(L1AgentService(load_settings()).start())


if __name__ == "__main__":
    main()
