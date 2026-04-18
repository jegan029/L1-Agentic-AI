"""Splunk REST API adapter for log searches."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import aiohttp

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.config.settings import SplunkSettings
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("splunk_adapter")


class SplunkAdapter(BaseAdapter):
    """Adapter for Splunk REST API (/services/search/jobs).

    Workflow:
    1. Create a search job (POST /services/search/jobs)
    2. Poll for job completion
    3. Fetch results
    4. Return summarised evidence
    """

    def __init__(self, settings: SplunkSettings) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

    @property
    def adapter_name(self) -> str:
        return "Splunk"

    async def health_check(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self._base_url}/services/server/info"
                async with session.get(
                    url,
                    headers=self._headers,
                    ssl=self._settings.verify_ssl,
                    params={"output_mode": "json"},
                ) as resp:
                    return resp.status == 200
        except Exception as exc:
            logger.warning("Splunk health check failed: %s", exc)
            return False

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Execute a Splunk search.

        Parameters:
            query (str): SPL search query
            time_range (dict): {"earliest": "-1h", "latest": "now"}
            max_results (int): Max result count (default 100)
        """
        query = parameters.get("query", "")
        time_range = parameters.get("time_range", {})
        max_results = parameters.get("max_results", 100)

        if not query:
            return AdapterResult(success=False, error="No search query provided")

        try:
            job_sid = await self._create_search(query, time_range)
            await self._wait_for_job(job_sid)
            results = await self._get_results(job_sid, max_results)
            evidence = self._summarise_results(results)

            metrics.increment("splunk.searches_completed")
            return AdapterResult(
                success=True,
                data={"result_count": len(results), "results": results[:10]},
                raw_output=str(results[:5]),
                evidence_snippet=evidence,
            )
        except Exception as exc:
            metrics.increment("splunk.searches_failed")
            logger.error("Splunk search failed: %s", exc)
            return AdapterResult(success=False, error=str(exc))

    async def _create_search(
        self, query: str, time_range: Dict[str, str]
    ) -> str:
        """Create a Splunk search job and return the SID."""
        url = f"{self._base_url}/services/search/jobs"
        payload = {
            "search": f"search {query}" if not query.startswith("search ") else query,
            "output_mode": "json",
            "exec_mode": "normal",
        }
        if "earliest" in time_range:
            payload["earliest_time"] = time_range["earliest"]
        if "latest" in time_range:
            payload["latest_time"] = time_range["latest"]

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=self._headers,
                data=payload,
                ssl=self._settings.verify_ssl,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("sid", "")

    async def _wait_for_job(self, sid: str, poll_interval: float = 2.0) -> None:
        """Poll until the Splunk search job completes."""
        import time as _time

        url = f"{self._base_url}/services/search/jobs/{sid}"
        timeout = self._settings.search_timeout_seconds
        start = _time.monotonic()

        async with aiohttp.ClientSession() as session:
            while (_time.monotonic() - start) < timeout:
                async with session.get(
                    url,
                    headers=self._headers,
                    ssl=self._settings.verify_ssl,
                    params={"output_mode": "json"},
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    entry = data.get("entry", [{}])[0] if data.get("entry") else {}
                    content = entry.get("content", {})
                    if content.get("isDone"):
                        return
                await asyncio.sleep(poll_interval)

        raise TimeoutError(f"Splunk search job {sid} timed out after {timeout}s")

    async def _get_results(self, sid: str, max_results: int) -> list:
        """Fetch results from a completed search job."""
        url = f"{self._base_url}/services/search/jobs/{sid}/results"
        params = {"output_mode": "json", "count": str(max_results)}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=self._headers,
                ssl=self._settings.verify_ssl,
                params=params,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("results", [])

    @staticmethod
    def _summarise_results(results: list) -> str:
        count = len(results)
        if count == 0:
            return "Splunk search returned 0 results."
        snippet_lines = []
        for i, row in enumerate(results[:5]):
            raw = row.get("_raw", str(row))
            snippet_lines.append(f"  [{i+1}] {raw[:200]}")
        return f"Splunk search returned {count} result(s):\n" + "\n".join(snippet_lines)
