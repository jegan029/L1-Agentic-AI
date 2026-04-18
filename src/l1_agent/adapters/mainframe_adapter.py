"""Mainframe TN3270 adapter for MF BEIM ASYNC status checks.

Connects to mainframe systems via TN3270 terminal emulator protocol
to navigate screens and check job statuses. The team looks for
"inact ok" status on the BEIM async status screen.

All operations are read-only (screen scraping only, no data entry).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import MainframeSettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("mainframe_adapter")

# Known job statuses and their meanings
JOB_STATUS_MAP = {
    "inact ok": "Inactive OK - Job completed successfully and is inactive",
    "active": "Active - Job is currently running",
    "active ok": "Active OK - Job is running normally",
    "inact error": "Inactive Error - Job stopped with an error",
    "inact abend": "Inactive Abend - Job terminated abnormally",
    "waiting": "Waiting - Job is waiting for a resource or event",
    "stopped": "Stopped - Job has been manually stopped",
    "not found": "Not Found - Job does not exist in the system",
}


class MainframeAdapter(BaseAdapter):
    """Adapter for mainframe screen scraping via TN3270 protocol.

    Connects to mainframe hosts, navigates BEIM screens, and checks
    ASYNC job statuses. Uses the py3270 library for TN3270 emulation.

    Configuration:
    - host: Mainframe hostname
    - port: TN3270 port (default 23)
    - screen_navigation: List of keystrokes/commands to reach status screen
    - allowed_transactions: Allow-list of CICS transactions
    """

    def __init__(self, settings: MainframeSettings) -> None:
        self._settings = settings
        self._host = settings.host
        self._port = settings.port

    @property
    def adapter_name(self) -> str:
        return "Mainframe"

    async def health_check(self) -> bool:
        """Check if the mainframe host is reachable via TN3270."""
        return await asyncio.to_thread(self._health_check_sync)

    def _health_check_sync(self) -> bool:
        """Synchronous health check (runs in a thread)."""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((self._host, self._port))
            sock.close()
            return result == 0
        except Exception as exc:
            logger.warning("Mainframe health check failed: %s", exc)
            return False

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Execute a mainframe screen check.

        Parameters:
            action (str): "async_status", "job_status", or "screen_check".
            job_name (str): Name of the job to check.
            transaction (str): CICS transaction ID to navigate to.
            screen_navigation (list): List of navigation commands to reach
                the desired screen. Each entry: {"key": "Enter|Tab|PF3|...",
                "input": "text to type", "wait": seconds}.
            expected_status (str): Expected status string (default "inact ok").
        """
        action = parameters.get("action", "async_status")

        # Validate transaction against allow-list
        transaction = parameters.get("transaction", "")
        if transaction and self._settings.allowed_transactions:
            if transaction.upper() not in [
                t.upper() for t in self._settings.allowed_transactions
            ]:
                return AdapterResult(
                    success=False,
                    error=f"Transaction '{transaction}' not in allowed list",
                )

        try:
            if action == "async_status":
                return await self._check_async_status(parameters)
            elif action == "job_status":
                return await self._check_job_status(parameters)
            elif action == "screen_check":
                return await self._screen_check(parameters)
            else:
                return AdapterResult(
                    success=False,
                    error=f"Unknown mainframe action: {action}",
                )
        except Exception as exc:
            metrics.increment("mainframe.requests_failed")
            logger.error("Mainframe %s failed: %s", action, exc)
            return AdapterResult(success=False, error=str(exc))

    async def _check_async_status(
        self, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Check MF BEIM ASYNC job status on mainframe screen.

        Connects via TN3270, navigates to the BEIM status screen,
        and looks for job status indicators.
        """
        job_name = parameters.get("job_name", "")
        expected_status = parameters.get("expected_status", "inact ok")
        screen_navigation = parameters.get(
            "screen_navigation",
            self._settings.default_navigation,
        )

        # Run all blocking TN3270 operations in a thread to avoid
        # blocking the asyncio event loop.
        screen_text = await asyncio.to_thread(
            self._connect_navigate_disconnect, screen_navigation
        )
        if screen_text is None:
            return AdapterResult(
                success=False,
                error=f"Failed to connect to mainframe {self._host}:{self._port}",
            )

        # Parse job statuses from the screen
        jobs = _parse_beim_screen(screen_text, job_name)

        if not jobs:
            evidence = (
                f"BEIM ASYNC Status | Host: {self._host}\n"
                f"  Job '{job_name}': NOT FOUND on screen\n"
                f"  Screen excerpt: {screen_text[:200]}"
            )
            return AdapterResult(
                success=True,
                data={
                    "job_name": job_name,
                    "status": "not found",
                    "status_description": JOB_STATUS_MAP.get("not found", ""),
                    "expected_status": expected_status,
                    "match": False,
                },
                raw_output=screen_text[:500],
                evidence_snippet=evidence,
            )

        # Check if job status matches expected
        evidence_lines = [f"BEIM ASYNC Status | Host: {self._host}"]
        all_ok = True
        job_data: List[Dict[str, Any]] = []

        for job in jobs:
            status = job["status"].lower()
            matches_expected = expected_status.lower() in status
            status_desc = JOB_STATUS_MAP.get(status, f"Unknown status: {status}")

            job_entry = {
                "job_name": job["job_name"],
                "status": status,
                "status_description": status_desc,
                "matches_expected": matches_expected,
            }
            job_data.append(job_entry)

            icon = "OK" if matches_expected else "WARN"
            evidence_lines.append(
                f"  [{icon}] {job['job_name']}: {status} ({status_desc})"
            )
            if not matches_expected:
                all_ok = False

        evidence_lines.append(
            f"  Expected: {expected_status} | All match: {'YES' if all_ok else 'NO'}"
        )

        metrics.increment("mainframe.async_status_checks")
        return AdapterResult(
            success=True,
            data={
                "jobs": job_data,
                "jobs_checked": len(job_data),
                "all_match_expected": all_ok,
                "expected_status": expected_status,
            },
            raw_output=json.dumps(job_data),
            evidence_snippet="\n".join(evidence_lines),
        )

    async def _check_job_status(
        self, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Check a specific mainframe job status."""
        job_name = parameters.get("job_name", "")
        if not job_name:
            return AdapterResult(success=False, error="No job_name provided")

        # Delegate to async_status with specific job filter
        return await self._check_async_status(parameters)

    async def _screen_check(
        self, parameters: Dict[str, Any]
    ) -> AdapterResult:
        """Navigate to a screen and capture its content."""
        screen_navigation = parameters.get("screen_navigation", [])
        expected_text = parameters.get("expected_text", "")

        # Run all blocking TN3270 operations in a thread.
        screen_text = await asyncio.to_thread(
            self._connect_navigate_disconnect, screen_navigation
        )
        if screen_text is None:
            return AdapterResult(
                success=False,
                error=f"Failed to connect to mainframe {self._host}:{self._port}",
            )

        text_found = True
        if expected_text:
            text_found = expected_text.lower() in screen_text.lower()

        evidence = (
            f"Mainframe Screen Check | Host: {self._host}\n"
            f"  Screen content ({len(screen_text)} chars):\n"
            f"  {screen_text[:300]}"
        )
        if expected_text:
            evidence += f"\n  Expected '{expected_text}': {'FOUND' if text_found else 'NOT FOUND'}"

        metrics.increment("mainframe.screen_checks")
        return AdapterResult(
            success=text_found,
            data={
                "screen_text": screen_text[:1000],
                "expected_text": expected_text,
                "text_found": text_found,
            },
            raw_output=screen_text[:500],
            evidence_snippet=evidence,
        )

    # ── TN3270 connection helpers (all synchronous, called via to_thread) ──

    def _connect_navigate_disconnect(
        self, navigation: List[Dict[str, str]]
    ) -> Optional[str]:
        """Connect, navigate, capture screen, and disconnect.

        This is the single blocking entry-point that runs entirely inside
        a worker thread (via ``asyncio.to_thread``) so the event loop is
        never blocked.

        Returns:
            The final screen text, or ``None`` if the connection failed.
        """
        emulator = self._connect()
        if not emulator:
            return None
        try:
            return self._navigate_screens(emulator, navigation)
        finally:
            self._disconnect(emulator)

    def _connect(self) -> Optional[Any]:
        """Establish a TN3270 connection to the mainframe."""
        try:
            from py3270 import Emulator

            emulator = Emulator(visible=False)
            emulator.connect(f"{self._host}:{self._port}")
            emulator.wait_for_field()
            logger.info("Connected to mainframe %s:%d", self._host, self._port)
            return emulator
        except ImportError:
            logger.error(
                "py3270 not installed. Install with: pip install py3270. "
                "Also requires x3270 terminal emulator on the system."
            )
            return None
        except Exception as exc:
            logger.error("Failed to connect to mainframe: %s", exc)
            return None

    def _navigate_screens(
        self, emulator: Any, navigation: List[Dict[str, str]]
    ) -> str:
        """Navigate through mainframe screens using key sequences.

        Args:
            emulator: py3270 Emulator instance.
            navigation: List of navigation steps.

        Returns:
            The final screen text content.
        """
        import time as _time

        for step in navigation:
            input_text = step.get("input", "")
            key = step.get("key", "Enter")
            wait_seconds = float(step.get("wait", "1"))

            if input_text:
                emulator.send_string(input_text)

            # Map common key names to py3270 methods
            key_map = {
                "enter": "send_enter",
                "tab": "send_tab",
                "pf1": "send_pf1",
                "pf2": "send_pf2",
                "pf3": "send_pf3",
                "pf4": "send_pf4",
                "pf5": "send_pf5",
                "pf6": "send_pf6",
                "pf7": "send_pf7",
                "pf8": "send_pf8",
                "pf9": "send_pf9",
                "pf10": "send_pf10",
                "pf11": "send_pf11",
                "pf12": "send_pf12",
                "clear": "send_clear",
            }

            method_name = key_map.get(key.lower(), "send_enter")
            method = getattr(emulator, method_name, emulator.send_enter)
            method()

            _time.sleep(wait_seconds)
            emulator.wait_for_field()

        # Capture the final screen content
        screen_text = emulator.string_get(1, 1, 80 * 24)
        return screen_text

    def _disconnect(self, emulator: Any) -> None:
        """Disconnect from the mainframe."""
        try:
            emulator.terminate()
        except Exception as exc:
            logger.warning("Error disconnecting from mainframe: %s", exc)


def _parse_beim_screen(screen_text: str, job_filter: str = "") -> List[Dict[str, str]]:
    """Parse BEIM ASYNC status screen to extract job statuses.

    The BEIM screen typically shows job names and their statuses in a
    tabular format. This parser looks for patterns like:
        JOBNAME    INACT OK
        JOBNAME    ACTIVE
        JOBNAME    INACT ERROR

    Args:
        screen_text: Raw screen content from TN3270.
        job_filter: Optional job name filter (partial match).

    Returns:
        List of dicts with job_name and status.
    """
    jobs: List[Dict[str, str]] = []

    # Pattern: job name followed by status keywords
    pattern = re.compile(
        r"(\S+)\s+(INACT\s+OK|INACT\s+ERROR|INACT\s+ABEND|ACTIVE\s*(?:OK)?|WAITING|STOPPED)",
        re.IGNORECASE,
    )

    for match in pattern.finditer(screen_text):
        job_name = match.group(1).strip()
        status = match.group(2).strip().lower()
        # Normalize whitespace in status
        status = re.sub(r"\s+", " ", status)

        if job_filter and job_filter.lower() not in job_name.lower():
            continue

        jobs.append({"job_name": job_name, "status": status})

    return jobs
