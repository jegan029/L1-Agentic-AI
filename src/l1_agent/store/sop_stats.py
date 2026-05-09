"""Per-SOP aggregated statistics computed from incident history."""

from __future__ import annotations

from typing import Dict, List


def compute_sop_stats(records: List[dict]) -> List[dict]:
    """Aggregate per-SOP counters from a list of history records."""
    agg: Dict[str, dict] = {}

    for r in records:
        sop_id = r.get("sop_id") or ""
        if not sop_id:
            continue

        if sop_id not in agg:
            agg[sop_id] = {
                "sop_id": sop_id,
                "sop_title": r.get("sop_title", ""),
                "total_runs": 0,
                "resolved_count": 0,
                "escalated_count": 0,
                "failed_count": 0,
                "total_duration_ms": 0.0,
            }

        entry = agg[sop_id]
        entry["total_runs"] += 1
        outcome = r.get("outcome", "")
        if outcome == "resolved":
            entry["resolved_count"] += 1
        elif outcome == "escalated":
            entry["escalated_count"] += 1
        elif outcome in ("failed", "partial"):
            entry["failed_count"] += 1
        entry["total_duration_ms"] += r.get("duration_ms", 0.0)

    result = []
    for entry in agg.values():
        total = entry["total_runs"]
        resolved = entry["resolved_count"]
        avg_ms = entry["total_duration_ms"] / total if total else 0.0
        result.append({
            "sop_id": entry["sop_id"],
            "sop_title": entry["sop_title"],
            "total_runs": total,
            "resolved_count": resolved,
            "escalated_count": entry["escalated_count"],
            "failed_count": entry["failed_count"],
            "success_rate_pct": round(resolved / total * 100, 1) if total else 0.0,
            "avg_duration_ms": round(avg_ms, 1),
        })

    return sorted(result, key=lambda x: x["total_runs"], reverse=True)
