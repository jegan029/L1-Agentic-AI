"""The Splunk search adapter: guard, then one backend, then one result shape.

Whatever the backend, a search is guarded first, and its rows are turned
into the same `AdapterResult`. A search the guard refuses, or that Splunk
refuses as not permitted or not valid, comes back marked `escalate`: the
engine does not try it again and hands the run to a person.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.adapters.splunk.backend import (
    Backend,
    SplunkError,
    clean_rows,
    evidence_snippet,
    result_data,
    short,
)
from src.l1_agent.adapters.splunk.guard import GuardConfig, GuardedSearch, GuardRejected, guard
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("splunk_adapter")


class SplunkSearchAdapter(BaseAdapter):
    def __init__(self, backend: Backend, guard_config: GuardConfig) -> None:
        self._backend = backend
        self._guard = guard_config

    @property
    def adapter_name(self) -> str:
        # The same whichever backend is configured; the backend is in the audit record.
        return "Splunk"

    @property
    def backend_name(self) -> str:
        return self._backend.name

    async def health(self) -> tuple:
        """(reachable and usable, reason when not)."""
        return await self._backend.health()

    async def health_check(self) -> bool:
        ok, reason = await self.health()
        if not ok:
            logger.warning("Splunk health check failed (%s): %s", self._backend.name, reason)
        return ok

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        """Run a search.

        Parameters:
            query (str): SPL, beginning with `search` or `index=`
            time_range: {"earliest": "-1h", "latest": "now"}, or a string such as "now-1h"
            max_results (int): at most the connection's limit
        """
        started = time.perf_counter()
        try:
            guarded = guard(
                parameters.get("query"),
                parameters.get("time_range"),
                parameters.get("max_results"),
                self._guard,
            )
        except GuardRejected as exc:
            metrics.increment("splunk.searches_rejected")
            return self._failed(started, None, "rejected", f"Search refused by the guard: {exc}")

        try:
            rows = clean_rows(await self._backend.search(guarded), guarded.max_results)
        except SplunkError as exc:
            metrics.increment("splunk.searches_failed")
            return self._failed(started, guarded, exc.outcome, short(exc), final=exc.final)
        except Exception as exc:
            metrics.increment("splunk.searches_failed")
            return self._failed(
                started, guarded, "error", f"Splunk search failed ({type(exc).__name__})", False
            )

        metrics.increment("splunk.searches_completed")
        snippet = evidence_snippet(rows)
        return AdapterResult(
            success=True,
            data=result_data(rows),
            raw_output=snippet,
            evidence_snippet=snippet,
            audit=self._audit(started, guarded, "ok", len(rows)),
        )

    def _failed(
        self,
        started: float,
        guarded: Optional[GuardedSearch],
        outcome: str,
        error: str,
        final: bool = True,
    ) -> AdapterResult:
        return AdapterResult(
            success=False,
            error=error,
            escalate=final,
            audit=self._audit(started, guarded, outcome, 0, error),
        )

    def _audit(
        self,
        started: float,
        guarded: Optional[GuardedSearch],
        outcome: str,
        result_count: int,
        error: str = "",
    ) -> Dict[str, Any]:
        """What was asked and what came of it. Never event text and never a credential."""
        record: Dict[str, Any] = {
            "backend": self._backend.name,
            "spl": guarded.spl if guarded else "",
            "earliest": guarded.earliest if guarded else "",
            "latest": guarded.latest if guarded else "",
            "time_range_clamped": bool(guarded and guarded.clamped),
            "result_count": result_count,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "outcome": outcome,
        }
        log = logger.info if outcome == "ok" else logger.warning
        log(
            "Splunk search backend=%s outcome=%s results=%d duration_ms=%.1f "
            "earliest=%s latest=%s spl=%s%s",
            record["backend"],
            outcome,
            result_count,
            record["duration_ms"],
            record["earliest"],
            record["latest"],
            record["spl"] or "(refused before it was built)",
            f" reason={error}" if error else "",
        )
        return record

    async def close(self) -> None:
        await self._backend.close()
