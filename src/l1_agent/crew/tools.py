"""CrewAI @tool wrappers around the existing L1 Agent adapters.

All adapters are async; _run_sync() bridges them to the synchronous
interface that CrewAI tools require. Adapters and SOPs are injected at
service startup via register_adapters() / register_sops() before the
first Crew.kickoff() call.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from crewai.tools import tool

# ── Module-level registries populated at startup ──────────────────────────────

_adapters: Dict[str, Any] = {}
_sops: List[Any] = []  # List[SOP] — avoid importing SOP at module level


def register_adapters(adapters: Dict[str, Any]) -> None:
    global _adapters
    _adapters = adapters


def register_sops(sops: List[Any]) -> None:
    global _sops
    _sops = sops


# ── Async → sync bridge ───────────────────────────────────────────────────────

def _run_sync(coro) -> Any:
    """Run an async coroutine synchronously for use inside CrewAI tool calls."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _fmt(result: Any) -> str:
    """Format an AdapterResult as a readable string for the LLM."""
    if result.success:
        return result.evidence_snippet or json.dumps(result.data, default=str)
    return f"ERROR: {result.error}"


# ── Adapter-backed tools ──────────────────────────────────────────────────────

@tool("Splunk Log Search")
def splunk_search(query: str, time_range: str = "now-1h") -> str:
    """Search Splunk logs using the provided query string over the given time range.
    Returns matching log events and a summary of findings.
    Use this to investigate application errors, OOM events, or job failures.
    Example query: 'index=app_logs host=app-server-prod01 "OutOfMemoryError"'"""
    adapter = _adapters.get("splunk")
    if not adapter:
        return "Splunk adapter not available."
    return _fmt(_run_sync(adapter.execute({"query": query, "time_range": time_range})))


@tool("Dynatrace VM Health Check")
def dynatrace_vm_health(host_name: str) -> str:
    """Check the health of a VM or host in Dynatrace.
    Returns active problems, problem severity, availability status, and open tickets.
    Use this first when an infrastructure incident arrives to confirm the host is degraded.
    Example: host_name='app-server-prod01'"""
    adapter = _adapters.get("dynatrace")
    if not adapter:
        return "Dynatrace adapter not available."
    return _fmt(_run_sync(adapter.execute({"action": "vm_health", "host_name": host_name})))


@tool("Dynatrace Metrics Query")
def dynatrace_metrics(
    host_name: str,
    metric_selector: str = "builtin:host.cpu.usage,builtin:host.mem.usage",
    time_range: str = "now-1h",
) -> str:
    """Query Dynatrace performance metrics for a specific host.
    metric_selector uses Dynatrace selector syntax, e.g.:
      'builtin:host.mem.usage,builtin:host.mem.availableBytes'
      'builtin:host.cpu.usage'
    Returns per-metric latest values and a CRITICAL/WARNING/OK assessment."""
    adapter = _adapters.get("dynatrace")
    if not adapter:
        return "Dynatrace adapter not available."
    return _fmt(_run_sync(adapter.execute({
        "action": "metrics",
        "metric_selector": metric_selector,
        "entity_selector": f'type("HOST"),entityName("{host_name}")',
        "time_range": time_range,
    })))


@tool("MQ Queue Check")
def mq_check(queue_manager: str, queue_name: str, action: str = "depth") -> str:
    """Check IBM MQ queue status for the given queue manager and queue.
    action can be:
      'depth'  — current message count (use to detect high queue depth)
      'status' — queue health and connection info
      'browse' — inspect queued messages without consuming them
    Returns queue metrics and a summary."""
    adapter = _adapters.get("ir360")
    if not adapter:
        return "MQ adapter not available."
    return _fmt(_run_sync(adapter.execute({
        "action": action,
        "queue_manager": queue_manager,
        "queue": queue_name,
    })))


@tool("Autosys Job Status")
def autosys_status(job_name: str, query_type: str = "status") -> str:
    """Check the status of an Autosys batch job.
    query_type can be:
      'status'       — current run state (RUNNING, SUCCESS, FAILURE, ON_HOLD)
      'history'      — last N run results and durations
      'dependencies' — upstream/downstream job dependencies
    Returns job details and a pass/fail assessment."""
    adapter = _adapters.get("autosys")
    if not adapter:
        return "Autosys adapter not available."
    return _fmt(_run_sync(adapter.execute({"job_name": job_name, "query_type": query_type})))


@tool("Windows File and Log Check")
def file_check(unc_path: str, pattern: str = "", last_n_lines: int = 100) -> str:
    """Read a log or config file from a Windows UNC share path.
    Optionally filter lines by a regex pattern and limit to the last N lines.
    Use this to inspect application logs, batch output files, or config dumps.
    Example: unc_path='\\\\fileserver\\logs\\app.log', pattern='ERROR|WARN'"""
    adapter = _adapters.get("windows_share")
    if not adapter:
        return "Windows share adapter not available."
    return _fmt(_run_sync(adapter.execute({
        "unc_path": unc_path,
        "pattern": pattern,
        "last_n_lines": last_n_lines,
    })))


@tool("Web Application Health Check")
def web_ui_check(url: str, check_type: str = "availability", expected_text: str = "") -> str:
    """Check a web application URL using browser automation.
    check_type can be:
      'availability' — verify the page loads with HTTP 200
      'content'      — verify expected_text appears on the page
      'screenshot'   — capture current page state
    Returns page status and any errors found."""
    adapter = _adapters.get("webui")
    if not adapter:
        return "Web UI adapter not available."
    return _fmt(_run_sync(adapter.execute({
        "url": url,
        "check_type": check_type,
        "expected_text": expected_text,
    })))


@tool("Mainframe Job Status Check")
def mainframe_check(job_name: str, expected_status: str = "COMPLETE", action: str = "status") -> str:
    """Check mainframe batch job status via TN3270 terminal.
    action can be 'status' (current state) or 'output' (SYSOUT/error log).
    expected_status is the target state, e.g. 'COMPLETE', 'RUNNING', 'ABEND'.
    Returns job completion code and any mainframe error messages."""
    adapter = _adapters.get("mainframe")
    if not adapter:
        return "Mainframe adapter not available."
    return _fmt(_run_sync(adapter.execute({
        "job_name": job_name,
        "expected_status": expected_status,
        "action": action,
    })))


# ── SOP lookup tools ──────────────────────────────────────────────────────────

@tool("List Available SOPs")
def list_sops() -> str:
    """List all Standard Operating Procedures (SOPs) currently loaded in the system.
    Returns SOP IDs, titles, keywords, applicable categories, and tool requirements.
    Use this to identify which SOP best matches an incident before calling get_sop_details."""
    if not _sops:
        return "No SOPs are loaded. Escalation required."
    lines = []
    for sop in _sops:
        keywords = ", ".join(sop.keywords[:10]) if sop.keywords else "none"
        categories = ", ".join(sop.applicable_categories) if sop.applicable_categories else "any"
        services = ", ".join(sop.applicable_services[:4]) if sop.applicable_services else "any"
        tools_req = ", ".join(getattr(sop, "tools_required", []))
        lines.append(
            f"SOP ID: {sop.sop_id}\n"
            f"  Title: {sop.title}\n"
            f"  Keywords: {keywords}\n"
            f"  Categories: {categories}\n"
            f"  Services/CIs: {services}\n"
            f"  Tools required: {tools_req}\n"
            f"  Steps: {len(sop.steps)}"
        )
    return "\n\n".join(lines)


@tool("Get SOP Details")
def get_sop_details(sop_id: str) -> str:
    """Get full details of a specific SOP including every step, parameters, decision
    logic, and escalation criteria. Call this after identifying a candidate SOP
    with list_sops to confirm it matches the incident and understand what tools to call."""
    sop = next((s for s in _sops if s.sop_id == sop_id), None)
    if not sop:
        ids = ", ".join(s.sop_id for s in _sops) or "none"
        return f"SOP '{sop_id}' not found. Available SOPs: {ids}"
    lines = [
        f"SOP: {sop.sop_id} — {sop.title}",
        f"Keywords: {', '.join(sop.keywords)}",
        f"Applicable categories: {', '.join(sop.applicable_categories)}",
        f"Applicable services/CIs: {', '.join(sop.applicable_services)}",
        f"Tools required: {', '.join(getattr(sop, 'tools_required', []))}",
        "",
        "Steps:",
    ]
    for step in sop.steps:
        params_str = json.dumps(step.parameters, indent=4) if step.parameters else "{}"
        lines.append(
            f"\n  [{step.step_id}] {step.step_type.value}"
            f"\n    Description: {step.description}"
            f"\n    Parameters:\n{params_str}"
            f"\n    On success → {step.on_success or 'END'}"
            f"\n    On failure → {step.on_failure or 'END'}"
        )
    return "\n".join(lines)
