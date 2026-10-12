"""The safety layer every Splunk search passes through before a backend sees it.

SPL can write (`collect`, `outputlookup`, `delete`), read other systems
(`rest`, `dbxquery`) and run stored searches the caller never sees (macros,
`savedsearch`, subsearches). The guard accepts a small read-only subset:

- the search begins with `search` or `index=`;
- it names at least one index, every named index is on the allow list, and
  none is a wildcard, negated or hidden inside parentheses;
- every command after a pipe is on `ALLOWED_COMMANDS`;
- there is no subsearch, no macro and no time modifier written in the query;
- the time range is clamped to the longest lookback allowed, and
  `| head <limit>` is always appended.

This is pattern matching over a tokenised query, not an SPL parser. It is
written to refuse what it does not understand; the Splunk role of the search
user is the control that actually limits what can be read.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

ALLOWED_COMMANDS = frozenset(
    {
        "search",
        "where",
        "eval",
        "stats",
        "table",
        "fields",
        "sort",
        "head",
        "tail",
        "rex",
        "dedup",
        "timechart",
        "top",
        "rare",
    }
)
MAX_QUERY_LENGTH = 4000
MAX_SEGMENTS = 20

# Written in a search, any of these overrides the time range the guard clamps.
_TIME_MODIFIERS = frozenset(
    {"earliest", "latest", "_index_earliest", "_index_latest", "timeformat"}
    | {
        f"{edge}{unit}"
        for edge in ("start", "end", "")
        for unit in ("daysago", "hoursago", "minutesago", "monthsago")
    }
    | {"starttime", "endtime", "starttimeu", "endtimeu"}
)
_INDEX_NAME = re.compile(r"[A-Za-z0-9_\-]+")
_OPERATORS = re.compile(r"(!=|==|::|<=|>=|=|<|>)")

_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
    # Upper bounds, so that a clamp is never too generous.
    "mon": 31 * 86400,
    "q": 92 * 86400,
    "y": 366 * 86400,
}
_UNIT_NAMES = {
    "s": ("seconds", "second", "secs", "sec", "s"),
    "m": ("minutes", "minute", "mins", "min", "m"),
    "h": ("hours", "hour", "hrs", "hr", "h"),
    "d": ("days", "day", "d"),
    "w": ("weeks", "week", "w"),
    "mon": ("months", "month", "mon"),
    "q": ("quarters", "quarter", "qtrs", "qtr", "q"),
    "y": ("years", "year", "yrs", "yr", "y"),
}
_UNIT_OF = {name: unit for unit, names in _UNIT_NAMES.items() for name in names}
_UNIT = "|".join(sorted(_UNIT_OF, key=len, reverse=True))
_OFFSET = re.compile(rf"-(\d{{1,9}})({_UNIT})")
_RELATIVE = re.compile(rf"((?:-\d{{1,9}}(?:{_UNIT}))*)(?:@({_UNIT}|w[0-7]))?")
_DURATION = re.compile(rf"(\d{{1,9}})({_UNIT})")


class GuardRejected(ValueError):
    """The search is not one the agent may run. Nothing was sent to Splunk."""


@dataclass(frozen=True)
class GuardConfig:
    allowed_indexes: Tuple[str, ...] = ()
    max_lookback_seconds: int = 86400
    max_results: int = 100


@dataclass(frozen=True)
class GuardedSearch:
    """What a backend is given: the final SPL and its bounds."""

    spl: str
    earliest: str
    latest: str
    max_results: int
    indexes: Tuple[str, ...]
    clamped: bool = False


def parse_duration(text: str) -> int:
    """`24h`, `90m`, `7d` as seconds. Raises ValueError."""
    match = _DURATION.fullmatch((text or "").strip().lower())
    if not match or int(match.group(1)) <= 0:
        raise ValueError(f"'{text}' is not a duration such as 24h")
    return int(match.group(1)) * _UNIT_SECONDS[_UNIT_OF[match.group(2)]]


# ── Time range ────────────────────────────────────────────────────────


def _seconds_back(value: Any, now: float) -> Tuple[str, int]:
    """A time bound as (text for Splunk, how far back it can reach in seconds)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise GuardRejected("the time range must be relative times such as -4h and now")
    text = str(value).strip().lower()
    if len(text) > 40:
        raise GuardRejected("the time range is too long")
    if text.isdigit():
        # Epoch seconds.
        back = int(now) - int(text)
        if back < 0:
            raise GuardRejected("the time range is in the future")
        return text, back
    if text.startswith("now"):
        text = text[3:]
    if text == "":
        return "now", 0
    match = _RELATIVE.fullmatch(text)
    if not match:
        raise GuardRejected(f"the time '{value}' is not a relative time such as -4h or now")
    back = sum(int(n) * _UNIT_SECONDS[_UNIT_OF[u]] for n, u in _OFFSET.findall(match.group(1)))
    snap = match.group(2)
    if snap:
        # Snapping rounds down, so the bound can reach back by up to one more unit.
        back += _UNIT_SECONDS["w" if snap.startswith("w") else _UNIT_OF[snap]]
    return text, back


def normalise_time_range(
    value: Any, max_lookback_seconds: int, now: Optional[float] = None
) -> Tuple[str, str, bool]:
    """(earliest, latest, clamped) from either shape in use.

    A dict with `earliest` and `latest`, or a plain string such as `now-1h`
    that is read as the earliest time. Nothing given means the longest
    lookback allowed.
    """
    now = time.time() if now is None else now
    longest = f"-{max_lookback_seconds}s"
    if value in (None, "", {}):
        return longest, "now", False
    if isinstance(value, str):
        value = {"earliest": value}
    if not isinstance(value, dict):
        raise GuardRejected("the time range must be an object or a relative time")
    unknown = sorted(set(value) - {"earliest", "latest"})
    if unknown:
        raise GuardRejected(f"the time range has an unknown key '{unknown[0]}'")

    clamped = False
    if value.get("earliest") in (None, ""):
        earliest, earliest_back = longest, max_lookback_seconds
    else:
        earliest, earliest_back = _seconds_back(value["earliest"], now)
        if earliest_back > max_lookback_seconds:
            earliest, earliest_back, clamped = longest, max_lookback_seconds, True
    if value.get("latest") in (None, ""):
        latest, latest_back = "now", 0
    else:
        latest, latest_back = _seconds_back(value["latest"], now)
    if latest_back > earliest_back:
        raise GuardRejected("the time range ends before it begins or lies outside the lookback")
    return earliest, latest, clamped


# ── The query ─────────────────────────────────────────────────────────

_Token = Tuple[str, str]  # (kind, text): "w" word, "q" quoted string, "p" punctuation


def _segments(query: str) -> List[List[_Token]]:
    """Split on pipes that are outside double quotes, and tokenise each part."""
    segments: List[List[_Token]] = [[]]
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch == '"':
            j = i + 1
            while j < n and query[j] != '"':
                j += 2 if query[j] == "\\" else 1
            if j >= n:
                raise GuardRejected("the search has an unclosed quote")
            segments[-1].append(("q", query[i + 1 : j]))
            i = j + 1
        elif ch == "|":
            segments.append([])
            i += 1
        elif ch in "[]":
            raise GuardRejected("subsearches in square brackets are not allowed")
        elif ch == "`":
            raise GuardRejected("macros and backtick comments are not allowed")
        elif ch in "(),":
            segments[-1].append(("p", ch))
            i += 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not query[j].isspace() and query[j] not in '|[]`(),"':
                j += 1
            for part in _OPERATORS.split(query[i:j]):
                if part:
                    segments[-1].append(("p" if _OPERATORS.fullmatch(part) else "w", part))
            i = j
    return segments


def _index_name(token: Optional[_Token], allowed: Tuple[str, ...]) -> str:
    if token is None or token[0] == "p":
        raise GuardRejected("index= must be followed by an index name")
    name = token[1]
    if "*" in name or "?" in name:
        raise GuardRejected("wildcard indexes are not allowed")
    if not _INDEX_NAME.fullmatch(name):
        raise GuardRejected("an index name has characters that are not allowed")
    if name not in allowed:
        raise GuardRejected(f"index '{name}' is not on the allow list")
    return name


def _check_search_terms(tokens: List[_Token], allowed: Tuple[str, ...], *, base: bool) -> List[str]:
    """The indexes a `search` names. Rejects anything that widens or re-times it.

    In the base search an index must stand at the top level, so that it limits
    the whole search: not inside parentheses, not after NOT, and joined by OR
    only to another index. (OR binds tighter than the implied AND, so
    `index=a OR host=b` would search every default index for `host=b`.)
    """
    found: List[str] = []
    depth = 0
    after_index = False  # the previous term was a complete index term
    pending_or = False  # an OR after an index: only another index may follow
    loose_or = False  # an OR after something else: an index may not follow
    i = 0
    while i < len(tokens):
        kind, text = tokens[i]
        lowered = text.lower() if kind == "w" else ""
        following = tokens[i + 1] if i + 1 < len(tokens) else None

        if kind == "w" and lowered in _TIME_MODIFIERS and following and following[0] == "p":
            if following[1] not in "(),":
                raise GuardRejected(
                    f"'{text}' may not be written in the search; give the time range instead"
                )
        if kind == "w" and lowered == "index":
            if text != "index":
                raise GuardRejected("'index' must be written in lower case")
            if depth:
                raise GuardRejected("an index may not be named inside parentheses")
            if i and tokens[i - 1] == ("w", "NOT"):
                raise GuardRejected("an index may not be excluded with NOT")
            if loose_or:
                raise GuardRejected("an index may be joined by OR only to another index")
            pending_or = False
            if following == ("p", "="):
                found.append(_index_name(tokens[i + 2] if i + 2 < len(tokens) else None, allowed))
                i += 3
            elif following and following[0] == "w" and following[1].lower() == "in":
                if i + 2 >= len(tokens) or tokens[i + 2] != ("p", "("):
                    raise GuardRejected("index IN must be followed by a list in parentheses")
                i += 3
                listed = 0
                while i < len(tokens) and tokens[i] != ("p", ")"):
                    if tokens[i] != ("p", ","):
                        found.append(_index_name(tokens[i], allowed))
                        listed += 1
                    i += 1
                if i >= len(tokens) or not listed:
                    raise GuardRejected("index IN must list at least one index")
                i += 1
            else:
                raise GuardRejected("an index must be named as index=<name>")
            after_index = True
            continue

        if pending_or:
            raise GuardRejected("an index may be joined by OR only to another index")
        loose_or = False
        if (kind, text) == ("p", "("):
            depth += 1
        elif (kind, text) == ("p", ")"):
            depth -= 1
            if depth < 0:
                raise GuardRejected("the search has unbalanced parentheses")
        elif base and kind == "w" and text == "OR" and depth == 0:
            pending_or, loose_or = after_index, not after_index
        after_index = False
        i += 1

    if depth:
        raise GuardRejected("the search has unbalanced parentheses")
    if pending_or:
        raise GuardRejected("an index may be joined by OR only to another index")
    return found


def unquoted_placeholders(query: Any) -> List[str]:
    """The `${name}` placeholders in a search that do not stand inside double quotes.

    A value from a ticket goes into a search as a quoted term, never as bare
    SPL. The SOP validator uses this; the guard still checks the rendered search.
    """
    if not isinstance(query, str):
        return []
    found: List[str] = []
    quoted = False
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if quoted and ch == "\\":
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif not quoted and query.startswith("${", i):
            end = query.find("}", i)
            if end != -1:
                found.append(query[i + 2 : end])
        i += 1
    return found


def guard(
    query: Any,
    time_range: Any = None,
    max_results: Any = None,
    config: Optional[GuardConfig] = None,
    *,
    now: Optional[float] = None,
) -> GuardedSearch:
    """Check a search and return what may be sent. Raises GuardRejected."""
    config = config or GuardConfig()
    if not isinstance(query, str) or not query.strip():
        raise GuardRejected("no search was given")
    if len(query) > MAX_QUERY_LENGTH:
        raise GuardRejected(f"the search is longer than {MAX_QUERY_LENGTH} characters")
    if any(ord(c) < 32 and c not in "\t\n\r" for c in query) or "\x7f" in query:
        raise GuardRejected("the search contains control characters")
    if not config.allowed_indexes:
        raise GuardRejected("no index is on the allow list, so no search may be run")
    query = query.strip()

    segments = _segments(query)
    if len(segments) > MAX_SEGMENTS:
        raise GuardRejected("the search has too many commands")
    first = segments[0]
    if first and first[0] == ("w", "search"):
        first = first[1:]
        explicit = True
    elif len(first) > 1 and first[0] == ("w", "index") and first[1] == ("p", "="):
        explicit = False
    else:
        raise GuardRejected("a search must begin with 'search' or 'index='")

    indexes = _check_search_terms(first, config.allowed_indexes, base=True)
    if not indexes:
        raise GuardRejected("a search must name at least one index")
    for segment in segments[1:]:
        if not segment:
            raise GuardRejected("the search has an empty command")
        kind, command = segment[0]
        if kind != "w" or command.lower() not in ALLOWED_COMMANDS:
            shown = command[:40] if kind == "w" else "a quoted string"
            raise GuardRejected(f"the command '{shown}' is not allowed")
        if command.lower() == "search":
            _check_search_terms(segment[1:], config.allowed_indexes, base=False)

    limit = config.max_results
    if max_results is not None:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise GuardRejected("max_results must be a positive whole number")
        limit = min(max_results, limit)
    earliest, latest, clamped = normalise_time_range(time_range, config.max_lookback_seconds, now)
    spl = f"{query if explicit else 'search ' + query} | head {limit}"
    return GuardedSearch(spl, earliest, latest, limit, tuple(dict.fromkeys(indexes)), clamped)
