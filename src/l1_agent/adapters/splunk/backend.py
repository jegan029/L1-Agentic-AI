"""What the two Splunk backends share: errors, TLS, and the shape of a result.

A backend is given a `GuardedSearch` and returns rows or raises one of the
errors below. It never sees an unguarded query and never builds a result
itself, so the agent behaves the same whichever backend is configured.
"""

from __future__ import annotations

import os
import ssl
from typing import Any, Dict, List, Protocol, Tuple, Union

from src.l1_agent.adapters.splunk.guard import GuardedSearch
from src.l1_agent.config.settings import ConfigError

RESULTS_KEPT = 10
EVIDENCE_LINES = 5
MAX_VALUE = 500
MAX_ERROR = 300


class SplunkError(Exception):
    """A search that did not return rows. `outcome` is what the audit record says."""

    outcome = "error"
    # True: trying again cannot help, and the run is handed to a person.
    final = True


class AccessDenied(SplunkError):
    """401 or 403: the credential is refused or lacks the permission."""

    outcome = "access_denied"


class QueryRefused(SplunkError):
    """Splunk would not run the search as written."""

    outcome = "refused_by_splunk"


class Misconfigured(SplunkError):
    """The backend cannot work as configured, for example the search tool is not offered."""

    outcome = "misconfigured"


class Unavailable(SplunkError):
    """A timeout, a 5xx answer or no connection. The engine may try again."""

    outcome = "unavailable"
    final = False


class Backend(Protocol):
    name: str

    async def search(self, search: GuardedSearch) -> List[Dict[str, Any]]: ...

    async def health(self) -> Tuple[bool, str]: ...

    async def close(self) -> None: ...


def bearer_token(value: str) -> str:
    """The token, whether or not it was stored with its `Bearer ` prefix."""
    value = (value or "").strip()
    return value[7:].strip() if value[:7].lower() == "bearer " else value


def tls_context(verify_ssl: bool, ca_bundle: str = "") -> Union[ssl.SSLContext, bool]:
    """False when verification is off (demo mode only), otherwise a verifying context."""
    if not verify_ssl:
        return False
    if ca_bundle and not os.path.isfile(ca_bundle):
        raise ConfigError(f"The Splunk CA bundle '{ca_bundle}' is not a file")
    return ssl.create_default_context(cafile=ca_bundle or None)


def clean_rows(rows: Any, limit: int) -> List[Dict[str, str]]:
    """Rows as both backends return them: text values, without Splunk's internal fields."""
    cleaned: List[Dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kept: Dict[str, str] = {}
        for key, value in row.items():
            if not isinstance(key, str) or (key.startswith("_") and key not in ("_raw", "_time")):
                continue
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            text = str(value)
            kept[key] = text.strip()[:MAX_VALUE] if key == "_raw" else text[:MAX_VALUE]
        cleaned.append(kept)
        if len(cleaned) >= limit:
            break
    return cleaned


def evidence_snippet(rows: List[Dict[str, str]]) -> str:
    if not rows:
        return "Splunk search returned 0 results."
    lines = [
        f"  [{i + 1}] {(row.get('_raw') or str(row))[:200]}"
        for i, row in enumerate(rows[:EVIDENCE_LINES])
    ]
    return f"Splunk search returned {len(rows)} result(s):\n" + "\n".join(lines)


def result_data(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """The shape the executor already expects from a search."""
    return {"result_count": len(rows), "results": rows[:RESULTS_KEPT]}


def short(text: Any, secret: str = "") -> str:
    """Error text that is safe to keep: bounded, on one line, and without the token."""
    cleaned = " ".join(str(text).split())
    if secret:
        cleaned = cleaned.replace(secret, "<redacted>")
    return cleaned[:MAX_ERROR]
