"""Main service: webhook listener + polling worker for L1 Virtual Engineer Agent."""

from __future__ import annotations

import asyncio
import json
import re
import signal
from pathlib import Path
from typing import Any, Dict, Optional, Set

import aiohttp_cors
from aiohttp import web

from src.l1_agent.adapters.autosys_adapter import AutosysAdapter
from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.dynatrace_adapter import DynatraceAdapter
from src.l1_agent.adapters.ir360_adapter import IR360Adapter
from src.l1_agent.adapters.mainframe_adapter import MainframeAdapter
from src.l1_agent.adapters.mock_adapters import (
    MockAutosysAdapter,
    MockDynatraceAdapter,
    MockIR360Adapter,
    MockMainframeAdapter,
    MockSplunkAdapter,
    MockWebUIScraperAdapter,
    MockWindowsShareAdapter,
)
from src.l1_agent.adapters.splunk_adapter import SplunkAdapter
from src.l1_agent.adapters.webui_scraper_adapter import WebUIScraperAdapter
from src.l1_agent.adapters.windows_share_adapter import WindowsShareAdapter
from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.llm_client import LLMClient
from src.l1_agent.ai.mock_llm import MockLLMClient
from src.l1_agent.api.dashboard import make_dashboard_handler
from src.l1_agent.api.incidents import make_incidents_handler
from src.l1_agent.api.security import (
    MAX_BODY_BYTES,
    build_security_middleware,
    validate_security_config,
)
from src.l1_agent.api.sops import make_sops_handler
from src.l1_agent.api.stream import handle_stream
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import Settings
from src.l1_agent.demo import MockServiceNowClient
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.resolution_memory import ResolutionMemory
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.escalation.l2_router import L2Router
from src.l1_agent.events.event_bus import EventBus
from src.l1_agent.models.incident import Incident
from src.l1_agent.sop.validator import validate_sop
from src.l1_agent.store.incident_history import IncidentHistoryStore
from src.l1_agent.ui.sop_editor import SOPEditorRoutes
from src.l1_agent.utils.logging import get_logger, setup_logging
from src.l1_agent.utils.metrics import metrics
from src.l1_agent.utils.retry import CircuitBreaker
from src.l1_agent.utils.secrets import SecretsProvider

logger = get_logger("main")

_INCIDENT_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class L1AgentService:
    """Main service that manages the webhook listener and polling worker."""

    def __init__(self, settings: Settings) -> None:
        validate_security_config(settings.agent)
        self._settings = settings
        # Demo mode has no ServiceNow: notes and updates go to an in-memory stand-in.
        self._snow_client = (
            MockServiceNowClient()
            if settings.agent.demo_mode
            else ServiceNowClient(settings.servicenow)
        )
        self._sop_matcher = SOPMatcher(settings.agent.sop_confidence_threshold)
        self._sop_parser = SOPParser()
        self._adapters = self._build_adapters(settings)
        self._circuit_breakers = self._build_circuit_breakers(settings)
        self._event_bus = EventBus()
        self._history_store = IncidentHistoryStore()
        self._l2_router = (
            L2Router(Path(settings.agent.l2_routing_path))
            if settings.agent.l2_routing_path
            else L2Router()
        )
        self._executor = SOPExecutor(
            adapters=self._adapters,
            circuit_breakers=self._circuit_breakers,
            retry_max_attempts=settings.agent.retry_max_attempts,
            retry_base_delay=settings.agent.retry_base_delay_seconds,
            event_bus=self._event_bus,
        )

        # The model only advises on SOP ranking; it never executes anything.
        self._llm_client: Optional[LLMClient] = None
        ai_analyzer = None
        if settings.llm.enabled:
            if settings.agent.demo_mode and not settings.llm.api_key:
                self._llm_client = MockLLMClient()
                logger.info("Model advice enabled (demo: deterministic mock)")
            elif settings.llm.endpoint and settings.llm.api_key:
                self._llm_client = LLMClient(settings.llm)
                logger.info("Model advice enabled (model=%s)", settings.llm.model)
            else:
                logger.warning("LLM_ENABLED is true but no endpoint or key is set; model disabled")
            if self._llm_client is not None:
                ai_analyzer = AIAnalyzer(self._llm_client)

        self._memory = ResolutionMemory()
        # In demo mode, pre-load sample SOPs from disk
        demo_sop_cache = self._load_demo_sops() if settings.agent.demo_mode else None

        self._processor = IncidentProcessor(
            snow_client=self._snow_client,
            sop_matcher=self._sop_matcher,
            sop_parser=self._sop_parser,
            executor=self._executor,
            sop_cache=demo_sop_cache,
            confidence_threshold=settings.agent.sop_confidence_threshold,
            llm_client=self._llm_client,
            ai_analyzer=ai_analyzer,
            memory=self._memory,
            event_bus=self._event_bus,
            history_store=self._history_store,
            l2_router=self._l2_router,
        )
        self._running = False
        self._semaphore = asyncio.Semaphore(settings.agent.max_concurrent_incidents)
        # Keep references so in-flight work is not garbage collected mid-run.
        self._tasks: Set[asyncio.Task] = set()

    def _load_demo_sops(self) -> list:
        """Load the JSON SOPs from data/sample_sops/ for demo mode."""
        from src.l1_agent.models.sop import SOP

        sops: list = []
        # Use path relative to this file so it works regardless of cwd
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
            logger.info("Demo: loaded SOP %s v%s from %s", sop.sop_id, sop.version, f.name)
        logger.info("Demo mode: pre-loaded %d SOPs", len(sops))
        return sops

    def _build_adapters(self, settings: Settings) -> Dict[str, BaseAdapter]:
        if settings.agent.demo_mode:
            logger.info("Demo mode: using mock adapters")
            return {
                "splunk": MockSplunkAdapter(),
                "ir360": MockIR360Adapter(),
                "windows_share": MockWindowsShareAdapter(),
                "autosys": MockAutosysAdapter(),
                "dynatrace": MockDynatraceAdapter(),
                "webui": MockWebUIScraperAdapter(),
                "mainframe": MockMainframeAdapter(),
            }
        return {
            "splunk": SplunkAdapter(settings.splunk),
            "ir360": IR360Adapter(settings.ir360),
            "windows_share": WindowsShareAdapter(settings.windows_share),
            "autosys": AutosysAdapter(settings.autosys),
            "dynatrace": DynatraceAdapter(settings.dynatrace),
            "webui": WebUIScraperAdapter(settings.webui),
            "mainframe": MainframeAdapter(settings.mainframe),
        }

    def _build_circuit_breakers(self, settings: Settings) -> Dict[str, CircuitBreaker]:
        cb_args = {
            "failure_threshold": settings.agent.circuit_breaker_failure_threshold,
            "reset_timeout_seconds": settings.agent.circuit_breaker_reset_seconds,
        }
        return {name: CircuitBreaker(**cb_args) for name in self._adapters}

    # ── Webhook listener ──────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming ServiceNow webhook POST (signature checked by middleware)."""
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "Invalid JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "Expected a JSON object"}, status=400)

        incident = Incident.from_servicenow(payload)
        if not _INCIDENT_ID_RE.match(incident.number) or not _INCIDENT_ID_RE.match(incident.sys_id):
            return web.json_response({"error": "Missing or invalid number or sys_id"}, status=400)

        logger.info("Webhook received incident: %s", incident.number)
        self._spawn(incident)
        return web.json_response({"status": "accepted", "incident": incident.number})

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Liveness only. No detail, because this route is unauthenticated."""
        return web.json_response({"status": "healthy"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "healthy",
                "demo_mode": self._settings.agent.demo_mode,
                "metrics": metrics.snapshot(),
                "memory": self._memory.stats(),
            }
        )

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus-compatible /metrics endpoint for Grafana scraping."""
        return web.Response(
            text=metrics.prometheus_text(),
            content_type="text/plain",
            charset="utf-8",
        )

    def _create_app(self) -> web.Application:
        agent = self._settings.agent
        app = web.Application(
            middlewares=[build_security_middleware(agent)],
            client_max_size=MAX_BODY_BYTES,
        )
        app["event_bus"] = self._event_bus
        app["history_store"] = self._history_store

        app.router.add_post("/webhook/incident", self._handle_webhook)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/metrics", self._handle_metrics)

        # Dashboard API routes
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/dashboard/summary", make_dashboard_handler(self._history_store))
        app.router.add_get("/api/incidents", make_incidents_handler(self._history_store))
        app.router.add_get(
            "/api/incidents/{incident_number}", make_incidents_handler(self._history_store)
        )
        app.router.add_get("/api/sops/stats", make_sops_handler(self._history_store))
        app.router.add_get("/api/stream", handle_stream)

        # The file-based SOP editor has no review or approval step, so it is a
        # demo-only tool. It is replaced by the SOP lifecycle in a later phase.
        if agent.demo_mode:
            SOPEditorRoutes(sop_dir="data/sample_sops").register(app)

        # CORS only for explicitly configured origins. The dev UI needs none:
        # the Vite proxy makes API calls same-origin.
        if agent.cors_allowed_origins:
            cors = aiohttp_cors.setup(
                app,
                defaults={
                    origin: aiohttp_cors.ResourceOptions(
                        allow_credentials=False,
                        expose_headers=(),
                        allow_headers=("Authorization", "Content-Type"),
                        allow_methods=["GET", "OPTIONS"],
                    )
                    for origin in agent.cors_allowed_origins
                },
            )
            for route in list(app.router.routes()):
                if route.resource and route.resource.canonical.startswith("/api/"):
                    cors.add(route)

        return app

    # ── Polling worker ────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Continuously poll ServiceNow for new incidents."""
        interval = self._settings.servicenow.poll_interval_seconds
        logger.info("Starting poll loop (interval=%ds)", interval)

        while self._running:
            try:
                incidents = await self._snow_client.get_new_incidents()
                if incidents:
                    logger.info("Polled %d new incident(s)", len(incidents))
                for inc in incidents:
                    self._spawn(inc)
            except Exception as exc:
                logger.error("Poll cycle error: %s", exc)

            await asyncio.sleep(interval)

    def _spawn(self, incident: Incident) -> None:
        task = asyncio.create_task(self._process_with_semaphore(incident))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_with_semaphore(self, incident: Incident) -> None:
        """Process an incident with concurrency control."""
        async with self._semaphore:
            try:
                await self._processor.process_incident(incident)
            except Exception as exc:
                logger.error("Unhandled error processing %s: %s", incident.number, exc)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start both the webhook server and the polling worker."""
        self._running = True
        setup_logging(self._settings.agent.log_level)
        logger.info("L1 Agent Service starting (demo_mode=%s)", self._settings.agent.demo_mode)

        app = self._create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            self._settings.agent.webhook_host,
            self._settings.agent.webhook_port,
        )
        await site.start()
        logger.info(
            "Listener on %s:%d",
            self._settings.agent.webhook_host,
            self._settings.agent.webhook_port,
        )

        # Start poll loop as background task. Demo mode has no ServiceNow to poll.
        poll_task = (
            None if self._settings.agent.demo_mode else asyncio.create_task(self._poll_loop())
        )

        # Wait for shutdown signal (Windows-compatible)
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows does not support add_signal_handler; use signal.signal instead

            def _win_handler(signum, frame):
                loop.call_soon_threadsafe(stop_event.set)

            signal.signal(signal.SIGINT, _win_handler)
            signal.signal(signal.SIGTERM, _win_handler)
        await stop_event.wait()

        logger.info("Shutting down...")
        self._running = False
        if poll_task:
            poll_task.cancel()
        await runner.cleanup()
        await self._snow_client.close()
        if self._llm_client is not None:
            await self._llm_client.close()

    async def process_single(self, incident: Incident) -> Dict[str, Any]:
        """Process a single incident (for demo/testing)."""
        setup_logging(self._settings.agent.log_level)
        result = await self._processor.process_incident(incident)
        await self._snow_client.close()
        if self._llm_client is not None:
            await self._llm_client.close()
        summary = result.summary
        if summary is None:
            return {"incident_number": result.incident_number, "status": result.status.value}
        return {
            "incident_number": summary.incident_number,
            "status": result.status.value,
            "outcome": summary.outcome_label,
            "sop_id": summary.sop_id,
            "sop_title": summary.sop_title,
            "steps_executed": len(summary.step_results),
            "duration_ms": summary.total_duration_ms,
            "escalation_reason": summary.escalation_reason,
            "verified_by": summary.verified_by,
            "work_note": summary.to_work_note(),
        }


def main() -> None:
    """Entry point for the L1 Agent Service."""
    # Secrets go straight into the settings object, never into os.environ.
    secrets = SecretsProvider.from_env().fetch()
    settings = Settings.from_env(secrets=secrets)
    service = L1AgentService(settings)
    asyncio.run(service.start())


if __name__ == "__main__":
    main()
