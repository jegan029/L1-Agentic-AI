"""SOP matching engine: selects the best SOP for a given incident."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from src.l1_agent.models.incident import Incident
from src.l1_agent.models.sop import SOP
from src.l1_agent.utils.logging import get_logger

logger = get_logger("sop_matcher")

try:
    from src.l1_agent.engine.resolution_memory import ResolutionMemory as _ResolutionMemory
except ImportError:
    _ResolutionMemory = None  # type: ignore[assignment,misc]


# Share of the score each signal carries when every signal can be compared.
WEIGHTS = {"keyword": 0.50, "ci": 0.20, "category": 0.15, "group": 0.15}

# Distinct keyword hits that count as full keyword evidence. An SOP's keywords
# are alternative phrasings of one problem, so a ticket is not expected to
# contain all of them.
KEYWORD_HITS_FOR_FULL_SCORE = 3

_TITLE_STOPWORDS = frozenset(
    {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
)


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _TITLE_STOPWORDS}


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

    A signal is scored only when both sides carry it: the ticket has a value and
    the SOP declares what it applies to. A signal that cannot be compared is left
    out and the remaining weights are rescaled, so a ticket with no CI is not
    scored as if its CI were wrong. A value that is present and different still
    counts against the match.

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

        Scoring weights (see WEIGHTS), rescaled over the comparable signals:
          - Keyword match in the ticket text:  0.50
          - CI / service match:                0.20
          - Category match:                    0.15
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
            signals = {
                "keyword": self._keyword_score(short_desc_lower, desc_lower, sop),
                "ci": self._ci_score(incident, sop),
                "category": self._category_score(incident, sop),
                "group": self._group_score(incident, sop),
            }
            compared = {name: value for name, value in signals.items() if value is not None}
            weight = sum(WEIGHTS[name] for name in compared)
            score = (
                sum(WEIGHTS[name] * value for name, value in compared.items()) / weight
                if weight
                else 0.0
            )
            reasons = [f"{name}={value:.2f}" for name, value in compared.items() if value > 0]
            skipped = [name for name in signals if name not in compared]
            if skipped:
                reasons.append(f"not compared: {'/'.join(skipped)}")

            # Apply historical success-rate boost when memory is available
            if self._memory and sop.sop_id:
                boost = self._memory.boost_factor(sop.sop_id)
                if boost != 1.0:
                    score = min(score * boost, 1.0)
                    reasons.append(f"memory_boost={boost:.2f}")

            rationale = (
                f"Score {score:.2f}: {', '.join(reasons) if reasons else 'no match signals'}"
            )
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
    def _keyword_score(short_desc: str, desc: str, sop: SOP) -> Optional[float]:
        """Score the ticket text against the SOP's keywords and title.

        Counts distinct keywords found as whole words in either description, plus
        the share of the title's words found in the short description (worth at
        most one keyword). Full score at KEYWORD_HITS_FOR_FULL_SCORE hits, or at
        every keyword when the SOP lists fewer.
        """
        title_words = _tokens(sop.title)
        if not sop.keywords and not title_words:
            return None

        hits = 0.0
        for kw in sop.keywords:
            pattern = kw.lower()
            try:
                bounded = re.compile(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])")
            except re.error:
                bounded = re.compile(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])")
            if bounded.search(short_desc) or bounded.search(desc):
                hits += 1

        if title_words:
            hits += len(title_words & _tokens(short_desc)) / len(title_words)

        needed = min(len(sop.keywords), KEYWORD_HITS_FOR_FULL_SCORE) or 1
        return min(hits / needed, 1.0)

    @staticmethod
    def _category_score(incident: Incident, sop: SOP) -> Optional[float]:
        cat = incident.category.strip().lower()
        subcat = incident.subcategory.strip().lower()
        if not sop.applicable_categories or not (cat or subcat):
            return None
        for ac in sop.applicable_categories:
            if ac.lower() in (cat, subcat):
                return 1.0
        return 0.0

    @staticmethod
    def _ci_score(incident: Incident, sop: SOP) -> Optional[float]:
        ci = incident.cmdb_ci.strip().lower()
        if not sop.applicable_services or not ci:
            return None
        for svc in sop.applicable_services:
            if svc.lower() == ci or svc.lower() in ci:
                return 1.0
        return 0.0

    @staticmethod
    def _group_score(incident: Incident, sop: SOP) -> Optional[float]:
        group = incident.assignment_group.strip().lower()
        if not sop.applicable_assignment_groups or not group:
            return None
        for ag in sop.applicable_assignment_groups:
            if ag.lower() == group:
                return 1.0
        return 0.0
