"""What an SOP source has to provide (ADR 0005, knowledge family).

A source lists the articles that changed since a cursor and can fetch one.
Nothing a source returns is executed: ingestion turns each article into a
draft that a person reviews.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple

MAX_ARTICLE_CHARS = 200_000


@dataclass(frozen=True)
class Article:
    id: str  # stable within the source
    title: str
    body: str
    revision: str  # changes whenever the content changes
    updated_at: str = ""
    url: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)


class KnowledgeSource(Protocol):
    @property
    def source_id(self) -> str:
        """Identifies the source in drafts and in the audit log, e.g. 'servicenow_kb'."""

    async def changed_since(self, cursor: str) -> Tuple[List[Article], str]:
        """Articles added or changed after `cursor`, and the cursor to pass next time.

        An empty cursor means everything.
        """

    async def fetch(self, article_id: str) -> Optional[Article]: ...

    async def close(self) -> None: ...
