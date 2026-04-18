"""Main service: webhook listener + polling worker for L1 Virtual Engineer Agent."""

from __future__ import annotations

import asyncio
import json
import signal
from typing import Any, Dict

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
from src.l1_agent.ai.ai_executor import AIExecutor
from src.l1_agent.ai.analyzer import AIAnalyzer
from src.l1_agent.ai.llm_client import LLMClient
from src.l1_agent.ai.mock_llm import MockLLMClient
from src.l1_agent.clients.servicenow_client import ServiceNowClient
from src.l1_agent.config.settings import Settings
from src.l1_agent.engine.executor import SOPExecutor
from src.l1_agent.engine.incident_processor import IncidentProcessor
from src.l1_agent.engine.sop_matcher import SOPMatcher
from src.l1_agent.engine.sop_parser import SOPParser
from src.l1_agent.models.incident import Incident
from src.l1_agent.engine.resolution_memory import ResolutionMemory
from src.l1_agent.ui.sop_editor import SOPEditorRoutes
from src.l1_agent.utils.logging import get_logger, setup_logging
from src.l1_agent.utils.metrics import metrics
from src.l1_agent.utils.retry import CircuitBreaker
from src.l1_agent.utils.secrets import SecretsProvider

logger = get_logger("main")


class L1AgentService:
    """Main service that manages the webhook listener and polling worker."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._snow_client = ServiceNowClient(settings.servicenow)
        self._sop_matcher = SOPMatcher(settings.agent.sop_confidence_threshold)
        self._sop_parser = SOPParser()
        self._adapters = self._build_adapters(settings)
        self._circuit_breakers = self._build_circuit_breakers(settings)
        self._executor = SOPExecutor(
            adapters=self._adapters,
            circuit_breakers=self._circuit_breakers,
            retry_max_attempts=settings.agent.retry_max_attempts,
            retry_base_delay=settings.agent.retry_base_delay_seconds,
        )
        # Wire AI components when LLM is enabled
        self._llm_client = None
        ai_analyzer = None
        ai_executor = None
        if settings.llm.enabled:
            if settings.agent.demo_mode:
                self._llm_client = MockLLMClient()
                logger.info("AI mode enabled (demo: using MockLLMClient)")
            else:
                self._llm_client = LLMClient(settings.llm)
                logger.info("AI mode enabled (endpoint: %s)", settings.llm.endpoint)
            ai_analyzer = AIAnalyzer(self._llm_client)
            ai_executor = AIExecutor(
                llm_client=self._llm_client,
                adapters=self._adapters,
            )

        self._memory = ResolutionMemory()
        self._processor = IncidentProcessor(
            snow_client=self._snow_client,
            sop_matcher=self._sop_matcher,
            sop_parser=self._sop_parser,
            executor=self._executor,
            confidence_threshold=settings.agent.sop_confidence_threshold,
            llm_client=self._llm_client,
            ai_analyzer=ai_analyzer,
            ai_executor=ai_executor,
            memory=self._memory,
        )
        self._running = False
        self._semaphore = asyncio.Semaphore(settings.agent.max_concurrent_incidents)

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
        return {
            "splunk": CircuitBreaker(**cb_args),
            "ir360": CircuitBreaker(**cb_args),
            "windows_share": CircuitBreaker(**cb_args),
            "autosys": CircuitBreaker(**cb_args),
            "dynatrace": CircuitBreaker(**cb_args),
            "webui": CircuitBreaker(**cb_args),
            "mainframe": CircuitBreaker(**cb_args),
        }

    # ── Webhook listener ──────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Handle incoming ServiceNow webhook POST."""
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        incident = Incident.from_servicenow(payload)
        if not incident.number:
            return web.json_response({"error": "Missing incident number"}, status=400)

        logger.info("Webhook received incident: %s", incident.number)
        asyncio.create_task(self._process_with_semaphore(incident))
        return web.json_response({"status": "accepted", "incident": incident.number})

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "healthy",
            "metrics": metrics.snapshot(),
            "memory": self._memory.stats(),
        })

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus-compatible /metrics endpoint for Grafana scraping."""
        return web.Response(
            text=metrics.prometheus_text(),
            content_type="text/plain",
            charset="utf-8",
        )

    def _create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/webhook/incident", self._handle_webhook)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/metrics", self._handle_metrics)
        SOPEditorRoutes(sop_dir="data/sample_sops").register(app)
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
                    asyncio.create_task(self._process_with_semaphore(inc))
            except Exception as exc:
                logger.error("Poll cycle error: %s", exc)

            await asyncio.sleep(interval)

    async def _process_with_semaphore(self, incident: Incident) -> None:
        """Process an incident with concurrency control."""
        async with self._semaphore:
            try:
                await self._processor.process_incident(incident)
            except Exception as exc:
                logger.error(
                    "Unhandled error processing %s: %s", incident.number, exc
                )

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
            "Webhook listener on %s:%d",
            self._settings.agent.webhook_host,
            self._settings.agent.webhook_port,
        )

        # Start poll loop as background task
        poll_task = asyncio.create_task(self._poll_loop())

        # Wait for shutdown signal
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        logger.info("Shutting down...")
        self._running = False
        poll_task.cancel()
        await runner.cleanup()
        await self._snow_client.close()
        if self._llm_client is not None:
            await self._llm_client.close()

    async def process_single(self, incident: Incident) -> Dict[str, Any]:
        """Process a single incident (for demo/testing)."""
        setup_logging(self._settings.agent.log_level)
        summary = await self._processor.process_incident(incident)
        await self._snow_client.close()
        if self._llm_client is not None:
            await self._llm_client.close()
        return {
            "incident_number": summary.incident_number,
            "outcome": summary.outcome.value,
            "sop_id": summary.sop_id,
            "sop_title": summary.sop_title,
            "steps_executed": len(summary.step_results),
            "duration_ms": summary.total_duration_ms,
            "escalation_reason": summary.escalation_reason,
            "work_note": summary.to_work_note(),
        }


def main() -> None:
    """Entry point for the L1 Agent Service."""
    # Inject secrets from vault/SSM before loading settings
    SecretsProvider.from_env().inject()
    settings = Settings.from_env()
    service = L1AgentService(settings)
    asyncio.run(service.start())


if __name__ == "__main__":
    main()
