"""Autosys CLI adapter for read-only job status queries."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import AutosysSettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("autosys_adapter")

# Read-only commands that are safe to execute
SAFE_COMMANDS = frozenset({"autorep", "job_depends"})


class AutosysAdapter(BaseAdapter):
    """Read-only Autosys adapter that wraps CLI commands.

    Only executes status-query commands (autorep -J, autorep -q).
    Never runs sendevent or any state-changing command.
    """

    def __init__(self, settings: AutosysSettings) -> None:
        self._settings = settings

    @property
    def adapter_name(self) -> str:
        return "Autosys"

    async def health_check(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._settings.cli_path,
                "-V",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Execute a read-only Autosys status query.

        Parameters:
            job_name (str): Job or box name (required)
            query_type (str): 'status' (default) | 'dependencies'
        """
        job_name = parameters.get("job_name", "")
        query_type = parameters.get("query_type", "status")

        if not job_name:
            return AdapterResult(success=False, error="job_name is required")

        # Validate job name to prevent injection
        if not self._validate_job_name(job_name):
            return AdapterResult(
                success=False,
                error=f"Invalid job name: {job_name}",
            )

        try:
            if query_type == "dependencies":
                result = await self._run_job_depends(job_name)
            else:
                result = await self._run_autorep(job_name)
            return result
        except Exception as exc:
            metrics.increment("autosys.errors")
            return AdapterResult(success=False, error=str(exc))

    async def _run_autorep(self, job_name: str) -> AdapterResult:
        """Execute autorep -J <job_name> to get job status."""
        cmd = [self._settings.cli_path, "-J", job_name]
        return await self._execute_safe(cmd, f"autorep -J {job_name}")

    async def _run_job_depends(self, job_name: str) -> AdapterResult:
        """Execute job_depends -J <job_name> to get dependencies."""
        cmd = ["job_depends", "-J", job_name]
        return await self._execute_safe(cmd, f"job_depends -J {job_name}")

    async def _execute_safe(
        self, cmd: List[str], description: str
    ) -> AdapterResult:
        """Run a command ensuring it's in the safe allow-list."""
        base_cmd = cmd[0].split("/")[-1]
        if base_cmd not in SAFE_COMMANDS and base_cmd not in self._settings.allowed_commands:
            return AdapterResult(
                success=False,
                error=f"Command '{base_cmd}' not in allow-list",
            )

        logger.info("Executing: %s", description)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        err_output = stderr.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            metrics.increment("autosys.command_failures")
            return AdapterResult(
                success=False,
                error=f"Exit code {proc.returncode}: {err_output}",
                raw_output=output,
            )

        parsed = self._parse_autorep_output(output)
        evidence = self._format_evidence(description, parsed, output)
        metrics.increment("autosys.commands_executed")
        return AdapterResult(
            success=True,
            data=parsed,
            raw_output=output,
            evidence_snippet=evidence,
        )

    @staticmethod
    def _validate_job_name(name: str) -> bool:
        """Validate job name to prevent command injection."""
        return bool(re.match(r"^[a-zA-Z0-9_.\-/]+$", name))

    @staticmethod
    def _parse_autorep_output(output: str) -> Dict[str, Any]:
        """Parse autorep output into structured data.

        autorep typically outputs:
        Job Name           Last Start           Last End             ST/Ex ...
        __________________ ____________________ ____________________ _____
        MY_JOB             04/14/2026 10:00:00  04/14/2026 10:05:00  SU
        """
        lines = output.strip().split("\n")
        jobs: List[Dict[str, str]] = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 4 and not line.startswith("_") and not line.startswith("Job Name"):
                job_info: Dict[str, str] = {"job_name": parts[0]}
                # Try to extract status (usually last or near-last field)
                job_info["status"] = parts[-1] if parts else "UNKNOWN"
                jobs.append(job_info)
        return {"jobs": jobs, "raw_line_count": len(lines)}

    @staticmethod
    def _format_evidence(
        description: str, parsed: Dict[str, Any], raw: str
    ) -> str:
        jobs = parsed.get("jobs", [])
        if not jobs:
            return f"Command: {description}\nNo jobs found in output."
        lines = [f"Command: {description}", f"Jobs found: {len(jobs)}"]
        for j in jobs[:10]:
            lines.append(f"  {j.get('job_name', '?')} -> {j.get('status', '?')}")
        return "\n".join(lines)
