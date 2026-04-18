"""Resolution memory — persists resolved incident outcomes to improve SOP matching.

Each time an incident is resolved (not escalated), a record is written to a
JSON-lines file.  The SOPMatcher reads these records to boost SOPs that have
historically resolved similar incidents.

Storage format (one JSON object per line):
{
  "incident_number": "INC0001234",
  "sop_id": "SOP-MQ-001",
  "sop_title": "MQ Queue Depth High",
  "outcome": "resolved",
  "confidence_used": 0.82,
  "short_description": "MQ queue depth high on QMPROD01",
  "category": "Middleware",
  "cmdb_ci": "PaymentService",
  "assignment_group": "L1-Middleware-Support",
  "keywords_matched": ["queue depth", "mq"],
  "duration_ms": 4200.0,
  "resolved_at": "2026-04-18T10:00:00+00:00"
}
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional

from src.l1_agent.models.evidence import ExecutionOutcome, ExecutionSummary
from src.l1_agent.models.incident import Incident
from src.l1_agent.utils.logging import get_logger

logger = get_logger("resolution_memory")


class ResolutionMemory:
    """Persist and query resolved-incident history for learning-loop feedback.

    The memory file is a JSON-lines (*.jsonl) file that can be read back
    on startup to seed the SOP match-score boosting map.
    """

    def __init__(self, memory_file: str = "data/resolution_memory.jsonl") -> None:
        self._path = Path(memory_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        # sop_id → list of historical confidence scores
        self._confidence_history: Dict[str, List[float]] = defaultdict(list)
        # sop_id → success count
        self._success_counts: Dict[str, int] = defaultdict(int)
        # sop_id → total count
        self._total_counts: Dict[str, int] = defaultdict(int)
        self._load_existing()

    # ── Write ─────────────────────────────────────────────────────────

    def record(
        self,
        incident: Incident,
        summary: ExecutionSummary,
        confidence_used: float,
        keywords_matched: Optional[List[str]] = None,
    ) -> None:
        """Write a resolution record and update in-memory stats."""
        record = {
            "incident_number": incident.number,
            "sop_id": summary.sop_id,
            "sop_title": summary.sop_title,
            "outcome": summary.outcome.value,
            "confidence_used": confidence_used,
            "short_description": incident.short_description,
            "category": incident.category,
            "cmdb_ci": incident.cmdb_ci,
            "assignment_group": incident.assignment_group,
            "keywords_matched": keywords_matched or [],
            "duration_ms": summary.total_duration_ms,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            except OSError as exc:
                logger.error("Failed to write resolution memory: %s", exc)
                return

            sop_id = summary.sop_id
            if sop_id:
                self._confidence_history[sop_id].append(confidence_used)
                self._total_counts[sop_id] += 1
                if summary.outcome == ExecutionOutcome.RESOLVED:
                    self._success_counts[sop_id] += 1

        logger.info(
            "Resolution recorded: incident=%s sop=%s outcome=%s",
            incident.number,
            summary.sop_id,
            summary.outcome.value,
        )

    # ── Query ─────────────────────────────────────────────────────────

    def success_rate(self, sop_id: str) -> float:
        """Return historical resolution success rate for a given SOP (0.0–1.0)."""
        with self._lock:
            total = self._total_counts.get(sop_id, 0)
            if total == 0:
                return 0.5  # neutral prior when no history
            success = self._success_counts.get(sop_id, 0)
            return success / total

    def average_confidence(self, sop_id: str) -> float:
        """Return mean confidence score when this SOP was selected historically."""
        with self._lock:
            hist = self._confidence_history.get(sop_id, [])
            return sum(hist) / len(hist) if hist else 0.0

    def boost_factor(self, sop_id: str) -> float:
        """Compute a [0.9, 1.1] multiplier based on historical success rate.

        SOPs with >70 % success get up to +10 % boost; <30 % get up to -10 %.
        """
        rate = self.success_rate(sop_id)
        total = self._total_counts.get(sop_id, 0)
        if total < 3:
            return 1.0  # not enough data to adjust
        # Linear interpolation: rate=1.0 → 1.10, rate=0.0 → 0.90
        return 0.90 + (rate * 0.20)

    def stats(self) -> Dict[str, object]:
        """Return a snapshot of all tracked SOPs for observability."""
        with self._lock:
            return {
                sop_id: {
                    "total": self._total_counts[sop_id],
                    "resolved": self._success_counts[sop_id],
                    "success_rate": round(self.success_rate(sop_id), 3),
                    "avg_confidence": round(self.average_confidence(sop_id), 3),
                    "boost_factor": round(self.boost_factor(sop_id), 3),
                }
                for sop_id in self._total_counts
            }

    # ── Startup load ──────────────────────────────────────────────────

    def _load_existing(self) -> None:
        if not self._path.exists():
            return
        loaded = 0
        try:
            with self._path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        sop_id = rec.get("sop_id", "")
                        confidence = float(rec.get("confidence_used", 0.0))
                        outcome = rec.get("outcome", "")
                        if sop_id:
                            self._confidence_history[sop_id].append(confidence)
                            self._total_counts[sop_id] += 1
                            if outcome == ExecutionOutcome.RESOLVED.value:
                                self._success_counts[sop_id] += 1
                            loaded += 1
                    except (json.JSONDecodeError, ValueError):
                        pass
        except OSError as exc:
            logger.warning("Could not load resolution memory: %s", exc)
            return
        logger.info("Loaded %d resolution records from %s", loaded, self._path)
