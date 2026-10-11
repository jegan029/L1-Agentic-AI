"""Windows shared folder log reader (read-only, SMB-based)."""

from __future__ import annotations

import asyncio
import posixpath
from typing import Any, Dict, List

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import WindowsShareSettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("windows_share_adapter")

MAX_LINES = 5000
MAX_NEEDLES = 20


class WindowsShareAdapter(BaseAdapter):
    """Read-only adapter for inspecting log files on Windows shared folders.

    Uses SMB client library (smbprotocol) when available, otherwise falls
    back to OS-mounted UNC path access.  Always validates the UNC path
    against an allow-list before reading.
    """

    def __init__(self, settings: WindowsShareSettings) -> None:
        self._settings = settings
        self._allowed_prefixes = [p.replace("\\", "/") for p in settings.allowed_unc_prefixes]

    @property
    def adapter_name(self) -> str:
        return "WindowsShare"

    async def health_check(self) -> bool:
        return True  # Best-effort; real check depends on mount/SMB availability

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Read log lines from a Windows share.

        Parameters:
            unc_path (str): UNC path to the log file (e.g. //server/share/logs/app.log)
            pattern (str): Literal text to filter lines; '|' separates alternatives (optional)
            last_n_lines (int): Tail N lines (default 100)
            time_window (str): ISO duration to filter recent entries (optional, not yet impl)
        """
        unc_path = parameters.get("unc_path", "")
        pattern = parameters.get("pattern", "")
        last_n = max(1, min(int(parameters.get("last_n_lines", 100)), MAX_LINES))

        if not unc_path:
            return AdapterResult(success=False, error="unc_path is required")

        # Validate against allow-list
        normalised = posixpath.normpath(unc_path.replace("\\", "/"))
        if not self._is_allowed(normalised):
            return AdapterResult(
                success=False,
                error=f"Path {normalised} is not in the allowed UNC prefixes",
            )

        try:
            lines = await self._read_file(normalised, last_n)
            if pattern:
                # Literal, case-insensitive matching. No pattern is ever compiled.
                needles = [p.lower() for p in str(pattern).split("|") if p][:MAX_NEEDLES]
                lines = [ln for ln in lines if any(n in ln.lower() for n in needles)]

            evidence = self._format_evidence(normalised, lines, last_n)
            metrics.increment("windows_share.reads")
            return AdapterResult(
                success=True,
                data={"line_count": len(lines), "path": normalised},
                raw_output="\n".join(lines),
                evidence_snippet=evidence,
            )
        except FileNotFoundError:
            return AdapterResult(success=False, error=f"File not found: {normalised}")
        except PermissionError:
            return AdapterResult(success=False, error=f"Permission denied: {normalised}")
        except Exception as exc:
            metrics.increment("windows_share.errors")
            return AdapterResult(success=False, error=str(exc))

    def _is_allowed(self, path: str) -> bool:
        """True when the path is an allowed prefix or sits below one.

        Compared by path segment, so //host/logs does not admit //host/logs-private.
        An empty allow list denies everything.
        """
        if not self._allowed_prefixes:
            return False
        normalised = posixpath.normpath(path)
        for prefix in self._allowed_prefixes:
            base = posixpath.normpath(prefix).rstrip("/")
            if normalised == base or normalised.startswith(base + "/"):
                return True
        return False

    async def _read_file(self, path: str, last_n: int) -> List[str]:
        """Read last N lines from a file.

        In production, this would use smbprotocol for true SMB access.
        Currently uses OS-level file access (works with mounted shares).
        """
        return await asyncio.to_thread(self._read_file_sync, path, last_n)

    def _read_file_sync(self, path: str, last_n: int) -> List[str]:
        """Synchronous file read, run in a thread to avoid blocking the event loop."""
        # Convert UNC to OS path if needed
        os_path = self._unc_to_os_path(path)

        with open(os_path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()

        return [ln.rstrip("\n\r") for ln in all_lines[-last_n:]]

    @staticmethod
    def _unc_to_os_path(unc: str) -> str:
        """Convert //server/share/path to a local mount path.

        Assumes shares are mounted under /mnt/ (configurable).
        """
        # //server/share/path -> /mnt/server/share/path
        cleaned = unc.lstrip("/")
        return f"/mnt/{cleaned}"

    @staticmethod
    def _format_evidence(path: str, lines: List[str], requested: int) -> str:
        count = len(lines)
        preview = "\n".join(lines[-10:]) if lines else "(empty)"
        return (
            f"File: {path}\n"
            f"Lines returned: {count} (requested last {requested})\n"
            f"--- Last 10 lines ---\n{preview}"
        )
