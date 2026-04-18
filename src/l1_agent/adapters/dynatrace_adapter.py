"""Dynatrace REST API adapter for VM health checks and metrics queries.

Supports two primary use cases:
1. Cloud Controller / VM Health: Query host entities and problems via
   Dynatrace Entities API and Problems API.
2. Dynatrace Panels (CPU/Memory): Query metrics via Dynatrace Metrics API
   to verify CPU and memory are within normal thresholds.

All operations are read-only.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import aiohttp

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import DynatraceSettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("dynatrace_adapter")


class DynatraceAdapter(BaseAdapter):
    """Adapter for Dynatrace REST API v2.

    Provides:
    - Host/VM health checks via Entities + Problems APIs
    - CPU and memory metric queries via Metrics API
    """

    def __init__(self, settings: DynatraceSettings) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Api-Token {settings.api_token}",
            "Content-Type": "application/json",
        }

    @property
    def adapter_name(self) -> str:
        return "Dynatrace"

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self._base_url}/api/v2/entities"
                params = {"entitySelector": 'type("HOST")', "pageSize": "1"}
                async with session.get(
                    url,
                    headers=self._headers,
                    params=params,
                    ssl=self._settings.verify_ssl,
                ) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("Dynatrace health check failed: %s", exc)
            return False

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Execute a Dynatrace check.

        Parameters:
            action (str): One of "vm_health", "metrics", "problems".
            host_name (str): Host/VM name for vm_health checks.
            host_group (str): Optional host group filter.
            metric_selector (str): Dynatrace metric selector for metrics action.
            entity_selector (str): Entity selector for scoping queries.
            time_range (str): Relative time range (e.g. "now-1h").
        """
        action = parameters.get("action", "vm_health")

        try:
            if action == "vm_health":
                return await self._check_vm_health(parameters)
            elif action == "metrics":
                return await self._query_metrics(parameters)
            elif action == "problems":
                return await self._check_problems(parameters)
            else:
                return AdapterResult(
                    success=False,
                    error=f"Unknown Dynatrace action: {action}",
                )
        except Exception as exc:
            metrics.increment("dynatrace.requests_failed")
            logger.error("Dynatrace %s failed: %s", action, exc)
            return AdapterResult(success=False, error=str(exc))

    async def _check_vm_health(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Check VM/host health via Entities API + Problems API."""
        host_name = parameters.get("host_name", "")
        host_group = parameters.get("host_group", "")

        # Build entity selector
        selector_parts = ['type("HOST")']
        if host_name:
            selector_parts.append(f'entityName("{host_name}")')
        if host_group:
            selector_parts.append(f'fromRelationships.isInstanceOf(type("HOST_GROUP"),entityName("{host_group}"))')
        entity_selector = ",".join(selector_parts)

        # Query hosts
        hosts = await self._get_entities(entity_selector)

        if not hosts:
            return AdapterResult(
                success=True,
                data={"hosts_found": 0, "host_name": host_name},
                evidence_snippet=f"No hosts found matching '{host_name}'",
            )

        # Query problems for these hosts
        host_ids = [h.get("entityId", "") for h in hosts]
        problems = await self._get_problems_for_entities(host_ids)

        # Build evidence
        host_summaries = []
        for h in hosts[:10]:
            host_summaries.append({
                "entity_id": h.get("entityId", ""),
                "display_name": h.get("displayName", ""),
                "properties": h.get("properties", {}),
            })

        evidence_lines = [f"VM Health Check | Host filter: {host_name or 'all'}"]
        evidence_lines.append(f"  Hosts found: {len(hosts)}")
        for hs in host_summaries[:5]:
            evidence_lines.append(f"  - {hs['display_name']} ({hs['entity_id']})")

        if problems:
            evidence_lines.append(f"  Active problems: {len(problems)}")
            for p in problems[:5]:
                evidence_lines.append(
                    f"    [{p.get('severityLevel', '?')}] {p.get('title', 'Unknown problem')}"
                )
        else:
            evidence_lines.append("  Active problems: 0 (healthy)")

        metrics.increment("dynatrace.vm_health_checks")
        return AdapterResult(
            success=True,
            data={
                "hosts_found": len(hosts),
                "hosts": host_summaries,
                "problems_count": len(problems),
                "problems": problems[:5],
                "healthy": len(problems) == 0,
            },
            raw_output=json.dumps({"hosts": host_summaries, "problems": problems[:5]}),
            evidence_snippet="\n".join(evidence_lines),
        )

    async def _query_metrics(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Query Dynatrace Metrics API for CPU, memory, etc."""
        metric_selector = parameters.get(
            "metric_selector",
            "builtin:host.cpu.usage,builtin:host.mem.usage",
        )
        entity_selector = parameters.get("entity_selector", 'type("HOST")')
        time_range = parameters.get("time_range", "now-1h")

        url = f"{self._base_url}/api/v2/metrics/query"
        params: Dict[str, str] = {
            "metricSelector": metric_selector,
            "entitySelector": entity_selector,
            "from": time_range,
            "resolution": "1h",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=self._headers,
                params=params,
                ssl=self._settings.verify_ssl,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        results = data.get("result", [])
        evidence_lines = [f"Dynatrace Metrics | Selector: {metric_selector}"]

        metric_data: list = []
        for metric in results:
            metric_id = metric.get("metricId", "")
            for series in metric.get("data", []):
                dimensions = series.get("dimensionMap", {})
                values = series.get("values", [])
                latest_value = None
                for v in reversed(values):
                    if v is not None:
                        latest_value = v
                        break

                entry = {
                    "metric_id": metric_id,
                    "dimensions": dimensions,
                    "latest_value": latest_value,
                }
                metric_data.append(entry)

                host_label = dimensions.get("dt.entity.host", "unknown")
                if latest_value is not None:
                    evidence_lines.append(
                        f"  {metric_id} [{host_label}]: {latest_value:.1f}"
                    )

        metrics.increment("dynatrace.metric_queries")
        return AdapterResult(
            success=True,
            data={"metric_count": len(metric_data), "metrics": metric_data},
            raw_output=json.dumps(metric_data),
            evidence_snippet="\n".join(evidence_lines),
        )

    async def _check_problems(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Check active Dynatrace problems."""
        entity_selector = parameters.get("entity_selector", "")
        time_range = parameters.get("time_range", "now-2h")

        url = f"{self._base_url}/api/v2/problems"
        params: Dict[str, str] = {"from": time_range, "status": "OPEN"}
        if entity_selector:
            params["entitySelector"] = entity_selector

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=self._headers,
                params=params,
                ssl=self._settings.verify_ssl,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        problems = data.get("problems", [])
        evidence_lines = [f"Dynatrace Problems | Open: {len(problems)}"]
        for p in problems[:10]:
            evidence_lines.append(
                f"  [{p.get('severityLevel', '?')}] {p.get('title', '?')} "
                f"(impact: {p.get('impactLevel', '?')})"
            )

        metrics.increment("dynatrace.problem_checks")
        return AdapterResult(
            success=True,
            data={"problem_count": len(problems), "problems": problems[:10]},
            raw_output=json.dumps(problems[:10]),
            evidence_snippet="\n".join(evidence_lines),
        )

    # ── Internal helpers ──────────────────────────────────────────────

    async def _get_entities(
        self, entity_selector: str, page_size: int = 50
    ) -> list:
        """Fetch entities from the Dynatrace Entities API."""
        url = f"{self._base_url}/api/v2/entities"
        params = {"entitySelector": entity_selector, "pageSize": str(page_size)}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=self._headers,
                params=params,
                ssl=self._settings.verify_ssl,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("entities", [])

    async def _get_problems_for_entities(
        self,
        entity_ids: list,
        time_range: str = "now-2h",
    ) -> list:
        """Fetch open problems affecting the given entities."""
        if not entity_ids:
            return []

        entity_selector = ",".join(
            f'entityId("{eid}")' for eid in entity_ids[:20]
        )

        url = f"{self._base_url}/api/v2/problems"
        params: Dict[str, str] = {
            "entitySelector": entity_selector,
            "from": time_range,
            "status": "OPEN",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=self._headers,
                params=params,
                ssl=self._settings.verify_ssl,
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("problems", [])
