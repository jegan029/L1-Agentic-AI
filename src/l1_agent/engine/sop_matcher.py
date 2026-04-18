"""SOP matching engine: selects the best SOP for a given incident."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, List, Optional, Tuple

from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.logging import get_logger

logger = get_logger("sop_matcher")

try:
    from src.l1_agent.engine.resolution_memory import ResolutionMemory as _ResolutionMemory
except ImportError:
    _ResolutionMemory = None  # type: ignore[assignment,misc]


@dataclass
class MatchResult:
    """Result of SOP matching."""

    sop: Optional[SOP]
    confidence: float
    rationale: str
    alternatives: List[Tuple[SOP, float]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.alternatives is None:
            self.alternatives = []


class SOPMatcher:
    """Matches incidents to SOPs using keyword/regex, CI, category, and assignment group.

    When a ResolutionMemory instance is provided, historical success rates are used
    to boost or penalise scores by up to ±10%.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.6,
        memory: "Optional[_ResolutionMemory]" = None,
    ) -> None:
        self._threshold = confidence_threshold
        self._memory = memory

    def match(self, incident: Incident, sops: List[SOP]) -> MatchResult:
        """Find the best matching SOP for the given incident.

        Scoring weights:
          - Keyword match in short_description: 0.50
          - Category match:                    0.15
          - CI / service match:                0.20
          - Assignment group match:            0.15
        """
        if not sops:
            return MatchResult(
                sop=None,
                confidence=0.0,
                rationale="No SOPs available in the knowledge base.",
            )

        scored: List[Tuple[SOP, float, str]] = []
        short_desc_lower = incident.short_description.lower()
        desc_lower = incident.description.lower()

        for sop in sops:
            score = 0.0
            reasons: List[str] = []

            # ── Keyword matching (weight 0.50) ──
            keyword_score = self._keyword_score(short_desc_lower, desc_lower, sop)
            score += keyword_score * 0.50
            if keyword_score > 0:
                reasons.append(f"keyword={keyword_score:.2f}")

            # ── Category matching (weight 0.15) ──
            cat_score = self._category_score(incident, sop)
            score += cat_score * 0.15
            if cat_score > 0:
                reasons.append(f"category={cat_score:.2f}")

            # ── CI / service matching (weight 0.20) ──
            ci_score = self._ci_score(incident, sop)
            score += ci_score * 0.20
            if ci_score > 0:
                reasons.append(f"ci={ci_score:.2f}")

            # ── Assignment group matching (weight 0.15) ──
            group_score = self._group_score(incident, sop)
            score += group_score * 0.15
            if group_score > 0:
                reasons.append(f"group={group_score:.2f}")

            # Apply historical success-rate boost when memory is available
            if self._memory and sop.sop_id:
                boost = self._memory.boost_factor(sop.sop_id)
                if boost != 1.0:
                    score = min(score * boost, 1.0)
                    reasons.append(f"memory_boost={boost:.2f}")

            rationale = f"Score {score:.2f}: {', '.join(reasons) if reasons else 'no match signals'}"
            scored.append((sop, score, rationale))

        scored.sort(key=lambda x: x[1], reverse=True)
        best_sop, best_score, best_rationale = scored[0]

        alternatives = [(s, sc) for s, sc, _ in scored[1:4] if sc > 0]

        if best_score < self._threshold:
            return MatchResult(
                sop=best_sop,
                confidence=best_score,
                rationale=f"Low confidence ({best_score:.2f} < {self._threshold}). Best: {best_rationale}",
                alternatives=alternatives,
            )

        return MatchResult(
            sop=best_sop,
            confidence=best_score,
            rationale=best_rationale,
            alternatives=alternatives,
        )

    # ── Scoring helpers ───────────────────────────────────────────────

    @staticmethod
    def _keyword_score(short_desc: str, desc: str, sop: SOP) -> float:
        """Score keyword matches against the SOP's keywords and title."""
        if not sop.keywords and not sop.title:
            return 0.0

        matches = 0
        total = max(len(sop.keywords), 1)

        # Check SOP title words in the incident short_description
        title_words = set(sop.title.lower().split())
        title_matches = sum(1 for w in title_words if w in short_desc)
        if title_words:
            matches += title_matches / len(title_words)

        # Check each keyword via regex or substring
        for kw in sop.keywords:
            pattern = kw.lower()
            try:
                if re.search(pattern, short_desc) or re.search(pattern, desc):
                    matches += 1
            except re.error:
                if pattern in short_desc or pattern in desc:
                    matches += 1

        return min(matches / total, 1.0)

    @staticmethod
    def _category_score(incident: Incident, sop: SOP) -> float:
        if not sop.applicable_categories:
            return 0.0
        cat = incident.category.lower()
        subcat = incident.subcategory.lower()
        for ac in sop.applicable_categories:
            if ac.lower() in (cat, subcat):
                return 1.0
        return 0.0

    @staticmethod
    def _ci_score(incident: Incident, sop: SOP) -> float:
        if not sop.applicable_services:
            return 0.0
        ci = incident.cmdb_ci.lower()
        for svc in sop.applicable_services:
            if svc.lower() == ci or svc.lower() in ci:
                return 1.0
        return 0.0

    @staticmethod
    def _group_score(incident: Incident, sop: SOP) -> float:
        if not sop.applicable_assignment_groups:
            return 0.0
        group = incident.assignment_group.lower()
        for ag in sop.applicable_assignment_groups:
            if ag.lower() == group:
                return 1.0
        return 0.0
