"""IR360 MQ adapter for queue manager checks.

IR360 provides visibility into IBM MQ queue managers.  This adapter
supports two modes:
  1. API mode  - calls the IR360 REST API (when base_url is configured)
  2. CLI mode  - wraps a local CLI binary (when use_cli=True)

Both modes implement the same BaseAdapter interface.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import aiohttp

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import IR360Settings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("ir360_adapter")

_VALID_ACTIONS = frozenset({"depth", "browse", "status"})


class IR360Adapter(BaseAdapter):
    """MQ checks via IR360 API or CLI wrapper."""

    def __init__(self, settings: IR360Settings) -> None:
        self._settings = settings
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def adapter_name(self) -> str:
        return "IR360_MQ"

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health_check(self) -> bool:
        if self._settings.use_cli:
            return await self._cli_health()
        return await self._api_health()

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Execute an MQ check.

        Parameters:
            queue_manager (str): Queue manager name
            queue (str): Queue name
            action (str): One of 'depth', 'browse', 'status'
        """
        action = parameters.get("action", "status")
        qm = parameters.get("queue_manager", "")
        queue = parameters.get("queue", "")

        if not qm:
            return AdapterResult(success=False, error="queue_manager is required")

        if action not in _VALID_ACTIONS:
            return AdapterResult(
                success=False,
                error=f"Invalid action '{action}'. Must be one of: {', '.join(sorted(_VALID_ACTIONS))}",
            )

        if self._settings.use_cli:
            return await self._execute_cli(action, qm, queue)
        return await self._execute_api(action, qm, queue)

    # ── API mode ──────────────────────────────────────────────────────

    async def _api_health(self) -> bool:
        if not self._settings.base_url:
            return False
        try:
            session = self._get_session()
            url = f"{self._settings.base_url.rstrip('/')}/api/v1/health"
            async with session.get(url, headers=self._auth_headers(), ssl=False) as resp:
                healthy = resp.status == 200
                if healthy:
                    metrics.increment("ir360.health_checks_ok")
                else:
                    metrics.increment("ir360.health_checks_fail")
                return healthy
        except Exception as exc:
            logger.warning("IR360 API health check failed: %s", exc)
            metrics.increment("ir360.health_checks_fail")
            return False

    async def _execute_api(self, action: str, qm: str, queue: str) -> AdapterResult:
        """Call IR360 REST API to execute an MQ action.

        Endpoints:
          GET /api/v1/queuemanagers/{qm}/queues/{queue}/depth
          GET /api/v1/queuemanagers/{qm}/queues/{queue}/status
          GET /api/v1/queuemanagers/{qm}/queues/{queue}/browse?limit=5
          GET /api/v1/queuemanagers/{qm}/status   (no queue)
        """
        if not self._settings.base_url:
            return AdapterResult(success=False, error="IR360 base_url is not configured")

        base = self._settings.base_url.rstrip("/")
        endpoint = f"/api/v1/queuemanagers/{qm}"
        if queue:
            endpoint += f"/queues/{queue}"
        endpoint += f"/{action}"

        params: Dict[str, str] = {}
        if action == "browse":
            params["limit"] = "5"

        try:
            session = self._get_session()
            async with session.get(
                f"{base}{endpoint}",
                headers=self._auth_headers(),
                params=params,
                ssl=False,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                evidence = self._format_evidence(action, qm, queue, data)
                metrics.increment("ir360.api_calls")
                logger.debug("IR360 API %s %s/%s → %s", action, qm, queue, resp.status)
                return AdapterResult(
                    success=True,
                    data=data,
                    raw_output=json.dumps(data),
                    evidence_snippet=evidence,
                )
        except aiohttp.ClientResponseError as exc:
            metrics.increment("ir360.api_errors")
            logger.error("IR360 API HTTP error %s: %s", exc.status, exc.message)
            return AdapterResult(success=False, error=f"HTTP {exc.status}: {exc.message}")
        except aiohttp.ClientConnectionError as exc:
            metrics.increment("ir360.api_errors")
            logger.error("IR360 API connection error: %s", exc)
            return AdapterResult(success=False, error=f"Connection error: {exc}")
        except Exception as exc:
            metrics.increment("ir360.api_errors")
            logger.error("IR360 API unexpected error: %s", exc)
            return AdapterResult(success=False, error=str(exc))

    def _auth_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self._settings.api_key:
            headers["X-API-Key"] = self._settings.api_key
        return headers

    # ── CLI mode ──────────────────────────────────────────────────────

    async def _cli_health(self) -> bool:
        if not self._settings.cli_path:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._settings.cli_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0
        except Exception:
            return False

    async def _execute_cli(self, action: str, qm: str, queue: str) -> AdapterResult:
        """Execute IR360 CLI command for MQ checks (read-only)."""
        if not self._settings.cli_path:
            return AdapterResult(success=False, error="IR360 cli_path is not configured")

        cmd_args = [self._settings.cli_path, "--qm", qm]
        if queue:
            cmd_args.extend(["--queue", queue])
        cmd_args.append(f"--action={action}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")
                metrics.increment("ir360.cli_errors")
                return AdapterResult(
                    success=False,
                    error=f"CLI exit {proc.returncode}: {err}",
                    raw_output=output,
                )
            metrics.increment("ir360.cli_calls")
            return AdapterResult(
                success=True,
                data={"stdout": output},
                raw_output=output,
                evidence_snippet=f"MQ {action} on {qm}/{queue}:\n{output[:300]}",
            )
        except asyncio.TimeoutError:
            metrics.increment("ir360.cli_errors")
            return AdapterResult(success=False, error="CLI command timed out after 30s")
        except Exception as exc:
            metrics.increment("ir360.cli_errors")
            return AdapterResult(success=False, error=str(exc))

    @staticmethod
    def _format_evidence(action: str, qm: str, queue: str, data: dict) -> str:
        return f"MQ {action} | QM={qm} Queue={queue} | Result: {json.dumps(data)[:300]}"
