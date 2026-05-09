"""In-memory incident execution history store with JSONL persistence."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.l1_agent.models.evidence import ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.utils.logging import get_logger

logger = get_logger("incident_history")

_HISTORY_FILE = Path("data/incident_history.jsonl")
_MAX_RECORDS = 1000


class IncidentHistoryStore:
    """Stores completed incident execution records.

    Keeps the last 1000 records in memory and persists each record to
    data/incident_history.jsonl so history survives restarts.
    """

    def __init__(self, path: Path = _HISTORY_FILE) -> None:
        self._path = path
        self._records: deque = deque(maxlen=_MAX_RECORDS)
        self._lock = asyncio.Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        self._records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            logger.info("Loaded %d history records from disk", len(self._records))
        except Exception as exc:
            logger.error("Failed to load incident history: %s", exc)

    async def record(self, incident: Incident, summary: ExecutionSummary) -> None:
        entry = {
            "incident_number": summary.incident_number,
            "short_description": incident.short_description,
            "category": incident.category,
            "subcategory": getattr(incident, "subcategory", ""),
            "assignment_group": incident.assignment_group,
            "priority": incident.priority,
            "cmdb_ci": incident.cmdb_ci,
            "sop_id": summary.sop_id,
            "sop_title": summary.sop_title,
            "outcome": summary.outcome.value,
            "escalation_reason": summary.escalation_reason,
            "duration_ms": summary.total_duration_ms,
            "started_at": summary.started_at,
            "completed_at": summary.completed_at or datetime.now(timezone.utc).isoformat(),
            "step_count": len(summary.step_results),
            "step_results": [r.to_audit_dict() for r in summary.step_results],
        }
        async with self._lock:
            self._records.append(entry)
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as exc:
                logger.error("Failed to persist history record: %s", exc)

    def query(
        self,
        page: int = 1,
        per_page: int = 20,
        search: str = "",
        outcome: str = "",
        priority: Optional[int] = None,
    ) -> Tuple[int, List[dict]]:
        records = list(reversed(self._records))  # newest first

        if search:
            s = search.lower()
            records = [
                r for r in records
                if s in r.get("incident_number", "").lower()
                or s in r.get("short_description", "").lower()
                or s in r.get("sop_title", "").lower()
            ]

        if outcome:
            records = [r for r in records if r.get("outcome", "") == outcome.lower()]

        if priority is not None:
            records = [r for r in records if r.get("priority") == priority]

        total = len(records)
        start = (page - 1) * per_page
        items = records[start: start + per_page]

        # Strip step_results from list view for smaller payloads
        return total, [{k: v for k, v in r.items() if k != "step_results"} for r in items]

    def get_by_number(self, number: str) -> Optional[dict]:
        for r in reversed(self._records):
            if r.get("incident_number") == number:
                return r
        return None

    def trend_7day(self) -> List[Dict]:
        today = datetime.now(timezone.utc).date()
        buckets: Dict[str, Dict] = {}
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            buckets[d] = {"date": d, "received": 0, "resolved": 0, "escalated": 0}

        for r in self._records:
            started = r.get("started_at", "")
            if not started:
                continue
            try:
                day = started[:10]
            except Exception:
                continue
            if day not in buckets:
                continue
            buckets[day]["received"] += 1
            outcome = r.get("outcome", "")
            if outcome == "resolved":
                buckets[day]["resolved"] += 1
            elif outcome in ("escalated", "partial", "failed"):
                buckets[day]["escalated"] += 1

        return list(buckets.values())

    @property
    def all_records(self) -> List[dict]:
        return list(self._records)
