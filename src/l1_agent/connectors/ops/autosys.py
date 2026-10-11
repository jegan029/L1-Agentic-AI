"""Autosys: job status and dependencies, and the force start.

The reads go through the read-only adapter. The force start is the one write
(ADR 0008): it is made here, as an argument list, and only against a grant.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Mapping

from src.l1_agent.adapters.autosys_adapter import AutosysAdapter
from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import AutosysSettings
from src.l1_agent.connectors.sdk.base import AdapterConnector
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    PreCheck,
    RiskTier,
    Verify,
)
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("autosys")

FORCE_START = "autosys.job.force_start"
_JOB_NAME = r"[A-Za-z0-9_.\-]{1,64}"
_JOB_NAME_RE = re.compile(_JOB_NAME)
_OUTPUT_LIMIT = 2000


class AutosysConnector(AdapterConnector):
    manifest = Manifest(
        id="autosys",
        version="1.1.0",
        family=Family.OPERATIONS,
        display_name="Autosys",
        notes=(
            "The force start runs `sendevent -E FORCE_STARTJOB -J <job>` on the host the agent "
            "runs on. Only jobs matching `force_start_jobs` can be started; an empty list "
            "means none."
        ),
        config=closed(
            {
                "cli_path": {"type": "string"},
                "allowed_commands": {"type": "array"},
                "sendevent_path": {"type": "string"},
                # Exact job names or shell-style patterns (`NIGHTLY_*`).
                "force_start_jobs": {"type": "array"},
            }
        ),
        capabilities=(
            Capability(
                id="autosys.job.status",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Status of a job or box (autorep), or its dependencies (job_depends)",
                parameters=closed(
                    {
                        "job_name": {"type": "string", "maxLength": 64},
                        "query_type": {"type": "string", "enum": ["status", "dependencies"]},
                    },
                    required=["job_name"],
                ),
                scopes=("autorep", "job_depends"),
                target_parameters=("job_name",),
                timeout_seconds=30,
            ),
            Capability(
                id=FORCE_START,
                kind=Kind.WRITE,
                tier=RiskTier.T1,
                summary="Start a failed job now (sendevent -E FORCE_STARTJOB)",
                parameters=closed(
                    {"job_name": {"type": "string", "maxLength": 64, "pattern": _JOB_NAME}},
                    required=["job_name"],
                ),
                scopes=("sendevent", "autorep"),
                allow_lists=(AllowListBinding("job_name", "force_start_jobs", "pattern"),),
                target_parameters=("job_name",),
                supports_dry_run=False,  # sendevent has none; the pre-check carries the weight
                idempotent=False,
                pre_checks=(
                    # Failed or terminated. A job that is running or starting is left alone.
                    PreCheck(
                        "autosys.job.status",
                        'result.jobs[0].status in ["FA", "TE"]',
                        {"query_type": "status"},
                    ),
                ),
                verify=Verify(
                    "autosys.job.status",
                    'result.jobs[0].status == "SU"',
                    {"query_type": "status"},
                    failed='result.jobs[0].status in ["FA", "TE"]',
                    retries=10,
                    wait_seconds=30,
                ),
                rollback="none: a started job cannot be un-started",
                timeout_seconds=60,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return AutosysAdapter(
            AutosysSettings(
                cli_path=self.config.get("cli_path", "autorep"),
                allowed_commands=tuple(self.config.get("allowed_commands") or ("autorep",)),
            )
        )

    async def _execute_write(
        self, capability: Capability, parameters: Dict[str, Any]
    ) -> AdapterResult:
        if capability.id != FORCE_START:
            raise NotImplementedError(capability.id)
        job_name = str(parameters.get("job_name", ""))
        if not _JOB_NAME_RE.fullmatch(job_name):  # fullmatch: `$` would allow a trailing newline
            return AdapterResult(success=False, error="Invalid job name")
        if self._fixed is not None:
            # The mock stands in for the scheduler.
            return await self._fixed.force_start(job_name)  # type: ignore[attr-defined]
        return await self._sendevent(job_name, capability.timeout_seconds)

    async def _sendevent(self, job_name: str, timeout: int) -> AdapterResult:
        """`sendevent -E FORCE_STARTJOB -J <job>` as an argument list, never a shell string.

        The executable comes from the connection's settings, not from the SOP or the ticket.
        """
        argv = [
            self.config.get("sendevent_path") or "sendevent",
            "-E",
            "FORCE_STARTJOB",
            "-J",
            job_name,
        ]
        logger.info("Executing: sendevent -E FORCE_STARTJOB -J %s", job_name)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            metrics.increment("autosys.write_failures")
            # The event may or may not have been accepted. Verification decides.
            return AdapterResult(success=False, error=f"sendevent did not answer in {timeout}s")
        except OSError as exc:
            metrics.increment("autosys.write_failures")
            return AdapterResult(success=False, error=f"sendevent could not be run: {exc}")
        output = stdout.decode("utf-8", "replace")[:_OUTPUT_LIMIT]
        errors = stderr.decode("utf-8", "replace")[:_OUTPUT_LIMIT]
        if proc.returncode != 0:
            metrics.increment("autosys.write_failures")
            return AdapterResult(
                success=False,
                error=f"Exit code {proc.returncode}: {errors or output}".strip(),
                raw_output=output,
            )
        metrics.increment("autosys.force_starts")
        return AdapterResult(
            success=True,
            data={"job_name": job_name, "event": "FORCE_STARTJOB"},
            raw_output=output,
            evidence_snippet=f"Command: sendevent -E FORCE_STARTJOB -J {job_name}\nAccepted.",
        )
