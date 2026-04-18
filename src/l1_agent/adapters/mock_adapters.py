"""Mock adapters for demo mode and testing.

These adapters return realistic-looking responses without connecting
to any real external systems.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter


class MockSplunkAdapter(BaseAdapter):
    """Returns sample Splunk search results."""

    @property
    def adapter_name(self) -> str:
        return "MockSplunk"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        _query = parameters.get("query", "")  # noqa: F841 - kept for interface clarity
        results = [
            {
                "_raw": "2026-04-14 09:55:00 ERROR [PaymentService] Connection timeout to MQ broker mqprod01:1414",
                "_time": "2026-04-14T09:55:00.000+00:00",
                "host": "app-server-01",
                "source": "/var/log/payment-service/app.log",
                "sourcetype": "log4j",
            },
            {
                "_raw": "2026-04-14 09:55:05 WARN [PaymentService] Retry 1/3 for queue PAYMENT.REQUEST",
                "_time": "2026-04-14T09:55:05.000+00:00",
                "host": "app-server-01",
                "source": "/var/log/payment-service/app.log",
                "sourcetype": "log4j",
            },
            {
                "_raw": "2026-04-14 09:56:00 ERROR [PaymentService] All retries exhausted for PAYMENT.REQUEST",
                "_time": "2026-04-14T09:56:00.000+00:00",
                "host": "app-server-01",
                "source": "/var/log/payment-service/app.log",
                "sourcetype": "log4j",
            },
        ]
        evidence = f"Splunk search returned {len(results)} result(s):\n"
        for i, r in enumerate(results):
            evidence += f"  [{i+1}] {r['_raw']}\n"
        return AdapterResult(
            success=True,
            data={"result_count": len(results), "results": results},
            raw_output=json.dumps(results),
            evidence_snippet=evidence,
        )


class MockIR360Adapter(BaseAdapter):
    """Returns sample MQ queue status."""

    @property
    def adapter_name(self) -> str:
        return "MockIR360"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        qm = parameters.get("queue_manager", "QMPROD01")
        queue = parameters.get("queue", "PAYMENT.REQUEST")
        action = parameters.get("action", "depth")

        data: Dict[str, Any] = {}
        if action == "depth":
            data = {
                "queue_manager": qm,
                "queue": queue,
                "current_depth": 1523,
                "max_depth": 5000,
                "oldest_message_age_seconds": 3642,
                "input_count": 45,
                "output_count": 0,
            }
            evidence = (
                f"MQ depth check | QM={qm} Queue={queue}\n"
                f"  Current depth: 1523 / 5000 (30.5%)\n"
                f"  Oldest message age: 3642s (~1h)\n"
                f"  Input: 45, Output: 0 (CONSUMERS STALLED)"
            )
        elif action == "status":
            data = {
                "queue_manager": qm,
                "queue": queue,
                "status": "RUNNING",
                "open_input_count": 2,
                "open_output_count": 0,
                "last_put_time": "2026-04-14T09:55:00Z",
                "last_get_time": "2026-04-14T08:30:00Z",
            }
            evidence = (
                f"MQ status | QM={qm} Queue={queue}\n"
                f"  Status: RUNNING\n"
                f"  Open inputs: 2, Open outputs: 0\n"
                f"  Last put: 09:55:00, Last get: 08:30:00 (STALE)"
            )
        else:
            data = {"queue_manager": qm, "queue": queue, "action": action}
            evidence = f"MQ {action} | QM={qm} Queue={queue} | OK"

        return AdapterResult(
            success=True,
            data=data,
            raw_output=json.dumps(data),
            evidence_snippet=evidence,
        )


class MockWindowsShareAdapter(BaseAdapter):
    """Returns sample log file content."""

    @property
    def adapter_name(self) -> str:
        return "MockWindowsShare"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        unc_path = parameters.get("unc_path", "//fileserver/logs/app.log")
        pattern = parameters.get("pattern", "")
        lines = [
            "2026-04-14 09:50:00 INFO  Application started successfully",
            "2026-04-14 09:52:30 WARN  High memory usage detected: 85%",
            "2026-04-14 09:54:00 ERROR Connection refused to database server db-prod-01:5432",
            "2026-04-14 09:54:05 ERROR Retry 1/3 failed for db-prod-01:5432",
            "2026-04-14 09:54:15 ERROR Retry 2/3 failed for db-prod-01:5432",
            "2026-04-14 09:54:30 ERROR Retry 3/3 failed for db-prod-01:5432",
            "2026-04-14 09:54:31 FATAL Service degraded - database unavailable",
            "2026-04-14 09:55:00 INFO  Health check: DEGRADED",
        ]
        if pattern:
            import re as _re
            try:
                regex = _re.compile(pattern, _re.IGNORECASE)
                lines = [ln for ln in lines if regex.search(ln)]
            except _re.error:
                pass

        evidence = f"File: {unc_path}\nLines: {len(lines)}\n---\n" + "\n".join(lines[-10:])
        return AdapterResult(
            success=True,
            data={"line_count": len(lines), "path": unc_path},
            raw_output="\n".join(lines),
            evidence_snippet=evidence,
        )


class MockAutosysAdapter(BaseAdapter):
    """Returns sample Autosys job status."""

    @property
    def adapter_name(self) -> str:
        return "MockAutosys"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        job_name = parameters.get("job_name", "BATCH_PAYMENT_PROCESS")
        query_type = parameters.get("query_type", "status")

        if query_type == "dependencies":
            data = {
                "jobs": [
                    {"job_name": job_name, "status": "SU", "condition": "ROOT"},
                    {"job_name": f"{job_name}_STEP1", "status": "SU", "condition": f"s({job_name})"},
                    {"job_name": f"{job_name}_STEP2", "status": "FA", "condition": f"s({job_name}_STEP1)"},
                ],
            }
            evidence = (
                f"Autosys dependencies for {job_name}:\n"
                f"  {job_name} -> SU (ROOT)\n"
                f"  {job_name}_STEP1 -> SU\n"
                f"  {job_name}_STEP2 -> FA (FAILED)"
            )
        else:
            data = {
                "jobs": [
                    {
                        "job_name": job_name,
                        "status": "FA",
                        "last_start": "2026-04-14 09:00:00",
                        "last_end": "2026-04-14 09:05:23",
                        "exit_code": "1",
                    },
                ],
            }
            evidence = (
                f"Autosys status for {job_name}:\n"
                f"  Status: FA (FAILURE)\n"
                f"  Last run: 09:00:00 - 09:05:23\n"
                f"  Exit code: 1"
            )

        return AdapterResult(
            success=True,
            data=data,
            raw_output=json.dumps(data),
            evidence_snippet=evidence,
        )


class MockDynatraceAdapter(BaseAdapter):
    """Returns sample Dynatrace VM health and metrics data."""

    @property
    def adapter_name(self) -> str:
        return "MockDynatrace"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        action = parameters.get("action", "vm_health")

        if action == "vm_health":
            return await self._mock_vm_health(parameters)
        elif action == "metrics":
            return await self._mock_metrics(parameters)
        elif action == "problems":
            return await self._mock_problems(parameters)
        return AdapterResult(success=False, error=f"Unknown action: {action}")

    async def _mock_vm_health(self, parameters: Dict[str, Any]) -> AdapterResult:
        host_name = parameters.get("host_name", "app-server-01")
        hosts = [
            {
                "entity_id": "HOST-1A2B3C4D5E6F",
                "display_name": host_name,
                "properties": {
                    "osType": "LINUX",
                    "state": "RUNNING",
                    "cpuCores": 8,
                    "memoryTotal": 32768,
                },
            },
        ]
        evidence = (
            f"VM Health Check | Host filter: {host_name}\n"
            f"  Hosts found: 1\n"
            f"  - {host_name} (HOST-1A2B3C4D5E6F)\n"
            f"  Active problems: 0 (healthy)"
        )
        return AdapterResult(
            success=True,
            data={
                "hosts_found": 1,
                "hosts": hosts,
                "problems_count": 0,
                "problems": [],
                "healthy": True,
            },
            raw_output=json.dumps({"hosts": hosts, "problems": []}),
            evidence_snippet=evidence,
        )

    async def _mock_metrics(self, parameters: Dict[str, Any]) -> AdapterResult:
        metric_selector = parameters.get(
            "metric_selector",
            "builtin:host.cpu.usage,builtin:host.mem.usage",
        )
        metrics_data: List[Dict[str, Any]] = [
            {
                "metric_id": "builtin:host.cpu.usage",
                "dimensions": {"dt.entity.host": "HOST-1A2B3C4D5E6F"},
                "latest_value": 42.3,
            },
            {
                "metric_id": "builtin:host.mem.usage",
                "dimensions": {"dt.entity.host": "HOST-1A2B3C4D5E6F"},
                "latest_value": 67.8,
            },
        ]
        evidence = (
            f"Dynatrace Metrics | Selector: {metric_selector}\n"
            f"  builtin:host.cpu.usage [HOST-1A2B3C4D5E6F]: 42.3\n"
            f"  builtin:host.mem.usage [HOST-1A2B3C4D5E6F]: 67.8"
        )
        return AdapterResult(
            success=True,
            data={"metric_count": 2, "metrics": metrics_data},
            raw_output=json.dumps(metrics_data),
            evidence_snippet=evidence,
        )

    async def _mock_problems(self, parameters: Dict[str, Any]) -> AdapterResult:
        evidence = "Dynatrace Problems | Open: 0"
        return AdapterResult(
            success=True,
            data={"problem_count": 0, "problems": []},
            raw_output=json.dumps([]),
            evidence_snippet=evidence,
        )


class MockWebUIScraperAdapter(BaseAdapter):
    """Returns sample web UI scraping results."""

    @property
    def adapter_name(self) -> str:
        return "MockWebUIScraper"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        url = parameters.get("url", "https://app.example.com")
        check_type = parameters.get("check_type", "page_load")

        if check_type == "click_path":
            return await self._mock_click_path(url, parameters)
        return await self._mock_page_load(url, parameters)

    async def _mock_page_load(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        expected_text = parameters.get("expected_text", "")
        text_found = True

        evidence = (
            f"Page Load Check | URL: {url}\n"
            f"  Title: Application Dashboard\n"
            f"  Load time: 1.23s\n"
            f"  Page size: 45320 bytes\n"
            f"  No error indicators detected"
        )
        if expected_text:
            evidence += f"\n  Expected text '{expected_text}': FOUND"

        return AdapterResult(
            success=True,
            data={
                "url": url,
                "title": "Application Dashboard",
                "load_time_seconds": 1.23,
                "page_size_bytes": 45320,
                "expected_text_found": text_found,
                "error_indicators": [],
                "status": "healthy",
            },
            raw_output=json.dumps({"title": "Application Dashboard", "load_time": 1.23}),
            evidence_snippet=evidence,
        )

    async def _mock_click_path(
        self, url: str, parameters: Dict[str, Any]
    ) -> AdapterResult:
        click_steps = parameters.get("click_steps", [])
        step_results: List[Dict[str, Any]] = [
            {"step": "navigate", "url": url, "status": "success", "title": "Application Dashboard"},
        ]
        for i, step in enumerate(click_steps):
            step_results.append({
                "step": step.get("name", f"step-{i+1}"),
                "action": step.get("action", "click"),
                "status": "success",
            })

        evidence = (
            f"Click Path Check | URL: {url}\n"
            f"  Steps executed: {len(step_results)}\n"
            f"  Total time: 3.45s"
        )
        for sr in step_results:
            evidence += f"\n  [OK] {sr.get('step', '?')}: {sr.get('action', '?')}"

        return AdapterResult(
            success=True,
            data={
                "url": url,
                "steps_executed": len(step_results),
                "steps_passed": len(step_results),
                "total_time_seconds": 3.45,
                "step_results": step_results,
                "all_passed": True,
            },
            raw_output=json.dumps(step_results),
            evidence_snippet=evidence,
        )


class MockMainframeAdapter(BaseAdapter):
    """Returns sample mainframe BEIM ASYNC status data."""

    @property
    def adapter_name(self) -> str:
        return "MockMainframe"

    async def health_check(self) -> bool:
        return True

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        action = parameters.get("action", "async_status")
        job_name = parameters.get("job_name", "BEIM_ASYNC_JOB01")
        expected_status = parameters.get("expected_status", "inact ok")

        if action == "screen_check":
            return await self._mock_screen_check(parameters)

        jobs: List[Dict[str, Any]] = [
            {
                "job_name": job_name,
                "status": "inact ok",
                "status_description": "Inactive OK - Job completed successfully",
                "matches_expected": True,
            },
            {
                "job_name": f"{job_name}_SUB1",
                "status": "inact ok",
                "status_description": "Inactive OK - Job completed successfully",
                "matches_expected": True,
            },
        ]

        evidence = (
            f"BEIM ASYNC Status | Host: mainframe-prod:23\n"
            f"  [OK] {job_name}: inact ok (Inactive OK)\n"
            f"  [OK] {job_name}_SUB1: inact ok (Inactive OK)\n"
            f"  Expected: {expected_status} | All match: YES"
        )

        return AdapterResult(
            success=True,
            data={
                "jobs": jobs,
                "jobs_checked": len(jobs),
                "all_match_expected": True,
                "expected_status": expected_status,
            },
            raw_output=json.dumps(jobs),
            evidence_snippet=evidence,
        )

    async def _mock_screen_check(
        self, parameters: Dict[str, Any]
    ) -> AdapterResult:
        expected_text = parameters.get("expected_text", "")
        screen_text = (
            "CICS BEIM STATUS DISPLAY         DATE: 04/14/26  TIME: 09:55\n"
            "TRANSACTION: BEIM  STATUS: ACTIVE\n"
            "ASYNC JOBS:\n"
            "  BEIM_ASYNC_JOB01    INACT OK     LAST RUN: 04/14/26 09:00\n"
            "  BEIM_ASYNC_JOB02    INACT OK     LAST RUN: 04/14/26 09:15\n"
            "  BEIM_ASYNC_JOB03    ACTIVE       STARTED:  04/14/26 09:50\n"
            "PF3=EXIT  PF5=REFRESH  PF7=UP  PF8=DOWN"
        )

        text_found = True
        if expected_text:
            text_found = expected_text.lower() in screen_text.lower()

        evidence = (
            f"Mainframe Screen Check | Host: mainframe-prod:23\n"
            f"  Screen content ({len(screen_text)} chars):\n"
            f"  {screen_text[:300]}"
        )

        return AdapterResult(
            success=text_found,
            data={
                "screen_text": screen_text,
                "expected_text": expected_text,
                "text_found": text_found,
            },
            raw_output=screen_text,
            evidence_snippet=evidence,
        )
