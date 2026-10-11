"""Service assembly: HTTP listener, ServiceNow poller and run workers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI

from src.l1_agent.adapters.autosys_adapter import AutosysAdapter
from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.dynatrace_adapter import DynatraceAdapter
from src.l1_agent.adapters.ir360_adapter import IR360Adapter
from src.l1_agent.adapters.mainframe_adapter import MainframeAdapter
from src.l1_agent.adapters.splunk_adapter import SplunkAdapter
from src.l1_agent.adapters.webui_scraper_adapter import WebUIScraperAdapter
from src.l1_agent.adapters.windows_share_adapter import WindowsShareAdapter
from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.llm_client import LLMClient
from src.l1_agent.ai.mock_llm import MockLLMClient
from src.l1_agent.api.app import AppContext, create_app
from src.l1_agent.api.security import demo_open, validate_security_config, webhook_open
from src.l1_agent.audit.log import Actor
from src.l1_agent.auth.oidc import OidcClient
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import ConfigError, ServiceNowSettings, Settings
from src.l1_agent.db.engine import Database
from src.l1_agent.demo import MockServiceNowClient, build_mock_adapters
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.service.runs import DbEventBus, RunService
from src.l1_agent.sop.validator import validate_sop
from src.l1_agent.utils.logging import get_logger, setup_logging
from src.l1_agent.utils.retry import CircuitBreaker
from src.l1_agent.utils.secrets import SecretsProvider

logger = get_logger("main")

POLLER = Actor.system("poller")


def check_servicenow_config(snow: ServiceNowSettings) -> None:
    """Refuse a real ServiceNow connection that is incomplete or unscoped.

    Without an assignment group the poller would pick up every new incident on
    the instance, write work notes to it and reassign it.
    """
    missing = [
        name
        for name, value in (
            ("SERVICENOW_BASE_URL", snow.base_url),
            ("SERVICENOW_USERNAME", snow.username),
            ("SERVICENOW_PASSWORD", snow.password),
            ("SERVICENOW_ASSIGNMENT_GROUP", snow.assignment_group),
        )
        if not value
    ]
    if missing:
        raise ConfigError("ServiceNow connection needs: " + ", ".join(missing))
    if not snow.base_url.lower().startswith("https://"):
        raise ConfigError("SERVICENOW_BASE_URL must start with https://")


class L1AgentService:
    """Builds the engine and, once `setup` has run, the HTTP application around it."""

    def __init__(self, settings: Settings) -> None:
        validate_security_config(settings)
        self._settings = settings

        # Demo mode has no ServiceNow: notes and updates go to an in-memory stand-in.
        # SERVICENOW_LIVE_IN_DEMO connects demo mode to a real instance for testing.
        self._live_servicenow = not settings.agent.demo_mode or settings.servicenow.live_in_demo
        if self._live_servicenow:
            check_servicenow_config(settings.servicenow)
        self._snow_client = (
            ServiceNowClient(settings.servicenow)
            if self._live_servicenow
            else MockServiceNowClient()
        )
        self._adapters = self._build_adapters(settings)

        # The model only advises on SOP ranking; it never executes anything.
        self._llm_client: Optional[LLMClient] = None
        if settings.llm.enabled:
            if settings.agent.demo_mode and not settings.llm.api_key:
                self._llm_client = MockLLMClient()
                logger.info("Model advice enabled (demo: deterministic mock)")
            elif settings.llm.endpoint and settings.llm.api_key:
                self._llm_client = LLMClient(settings.llm)
                logger.info("Model advice enabled (model=%s)", settings.llm.model)
            else:
                logger.warning("LLM_ENABLED is true but no endpoint or key is set; model disabled")

        self._db: Optional[Database] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._ctx: Optional[AppContext] = None
        self._stop = asyncio.Event()

    # ── Engine ────────────────────────────────────────────────────────

    def _load_demo_sops(self) -> list:
        """Load the JSON SOPs from data/sample_sops/ for demo mode."""
        from src.l1_agent.models.sop import SOP

        sops: list = []
        sop_dir = Path(__file__).resolve().parent.parent.parent / "data" / "sample_sops"
        if not sop_dir.exists():
            sop_dir = Path("data/sample_sops")
        if not sop_dir.exists():
            logger.warning("Demo SOP directory not found: %s", sop_dir)
            return sops
        for f in sorted(sop_dir.glob("*.json")):
            try:
                sop = SOP.from_dict(json.loads(f.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.error("Failed to load demo SOP %s: %s", f.name, exc)
                continue
            problems = validate_sop(sop)
            if problems:
                logger.error("Demo SOP %s rejected: %s", f.name, "; ".join(problems[:5]))
                continue
            sops.append(sop)
        logger.info("Demo mode: pre-loaded %d SOPs", len(sops))
        return sops

    def _build_adapters(self, settings: Settings) -> Dict[str, BaseAdapter]:
        if settings.agent.demo_mode:
            logger.info("Demo mode: using mock adapters")
            return build_mock_adapters()
        return {
            "splunk": SplunkAdapter(settings.splunk),
            "ir360": IR360Adapter(settings.ir360),
            "windows_share": WindowsShareAdapter(settings.windows_share),
            "autosys": AutosysAdapter(settings.autosys),
            "dynatrace": DynatraceAdapter(settings.dynatrace),
            "webui": WebUIScraperAdapter(settings.webui),
            "mainframe": MainframeAdapter(settings.mainframe),
        }

    def _build_processor(self, db: Database) -> IncidentProcessor:
        settings = self._settings
        agent = settings.agent
        events = DbEventBus(db)
        breakers = {
            name: CircuitBreaker(
                failure_threshold=agent.circuit_breaker_failure_threshold,
                reset_timeout_seconds=agent.circuit_breaker_reset_seconds,
            )
            for name in self._adapters
        }
        return IncidentProcessor(
            snow_client=self._snow_client,  # type: ignore[arg-type]
            sop_matcher=SOPMatcher(agent.sop_confidence_threshold),
            sop_parser=SOPParser(),
            executor=SOPExecutor(
                adapters=self._adapters,
                circuit_breakers=breakers,
                retry_max_attempts=agent.retry_max_attempts,
                retry_base_delay=agent.retry_base_delay_seconds,
                event_bus=events,
            ),
            sop_cache=self._load_demo_sops() if agent.demo_mode else None,
            confidence_threshold=agent.sop_confidence_threshold,
            llm_client=self._llm_client,
            ai_analyzer=AIAnalyzer(self._llm_client) if self._llm_client else None,
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

        runs = RunService(
            db,
            self._build_processor(db),
            visibility_timeout_seconds=settings.worker.visibility_timeout_seconds,
            max_attempts=settings.worker.max_attempts,
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
        )
        return self._ctx

    async def create_app(self) -> FastAPI:
        return create_app(await self.setup())

    async def close(self) -> None:
        await self._snow_client.close()
        if self._llm_client is not None:
            await self._llm_client.close()
        if self._http is not None:
            await self._http.aclose()
        if self._db is not None:
            await self._db.close()

    # ── Background work ───────────────────────────────────────────────

    async def _poll_loop(self, ctx: AppContext) -> None:
        """Poll ServiceNow for new incidents and queue each one once."""
        interval = self._settings.servicenow.poll_interval_seconds
        logger.info("Starting poll loop (interval=%ds)", interval)
        while not self._stop.is_set():
            try:
                for incident in await self._snow_client.get_new_incidents():
                    result = await ctx.runs.ingest(ctx.tenant_id, incident, POLLER)
                    if result.accepted:
                        logger.info("Polled incident %s queued", incident.number)
            except Exception as exc:
                logger.error("Poll cycle error: %s", exc)
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

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.agent.webhook_host,
                port=settings.agent.webhook_port,
                log_config=None,
                access_log=False,
                server_header=False,
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
