"""Redaction of text before it is sent to a model provider (ADR 0009).

Everything the gateway sends passes through a `Redactor`. Secrets and personal
data are replaced by placeholders such as ``[[EMAIL_1]]``. The same value gets
the same placeholder for the whole run, so the text stays readable, and the
map from placeholder to value never leaves the process.

This is pattern matching. It finds the shapes listed here and nothing else: a
person's name in running text, or a secret with no recognisable shape and low
entropy, is not found. `evals/redaction.json` is the corpus it is tested with.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Dict, Iterable, List, Optional, Tuple

SECRET = "SECRET"
EMAIL = "EMAIL"
PHONE = "PHONE"
CARD = "CARD"
IBAN = "IBAN"
NATIONAL_ID = "ID"
IP = "IP"
PERSON = "PERSON"

# Kinds that may be put back into text written to the ticket the data came from.
# A secret is never put back.
PERSONAL = frozenset({EMAIL, PHONE, PERSON, IP})

_PLACEHOLDER = re.compile(r"\[\[([A-Z]+)_(\d+)\]\]")
_SENSITIVE_KEY = (
    r"password|passwd|pwd|passphrase|secret|client_secret|token|access_token|refresh_token"
    r"|api[_-]?key|apikey|authorization|credential|private_key"
)

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=\-]{8,})")
_KEY_VALUE = re.compile(
    r"(?i)\b(" + _SENSITIVE_KEY + r")\b(\"?\s*(?:=|:|\bis\b)\s*\"?)(?!\[\[)([^\s\"',;&<>]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+:[^/\s@]+)(@)")
_TOKEN_SHAPES = re.compile(
    r"\bsk-[A-Za-z0-9_\-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_CARD = re.compile(r"(?<![\w.])(?:\d[ \-]?){12,18}\d(?![\w.])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}")
_SSN = re.compile(r"(?<![\w\-])\d{3}-\d{2}-\d{4}(?![\w\-])")
_PHONE = re.compile(
    r"(?<![\w.])\+\d[\d ().\-]{7,17}\d(?!\w)"
    r"|(?<![\w.\-])\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}(?![\w\-])"
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{24,}(?![A-Za-z0-9+/=_\-])")
_HEX = re.compile(r"[0-9a-fA-F]{32,}")
MIN_ENTROPY_BITS = 3.5


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _is_card(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return 13 <= len(digits) <= 19 and len(set(digits)) > 1 and _luhn(digits)


def _is_iban(text: str) -> bool:
    compact = text.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    moved = compact[4:] + compact[:4]
    number = "".join(str(int(c, 36)) for c in moved)
    return int(number) % 97 == 1


def _iban_length(text: str) -> int:
    """How much of `text`, from its start, is a valid IBAN. Zero when none of it is.

    The candidate may run on into the words that follow, so shorter and shorter
    prefixes are tried until one passes the check digits.
    """
    for end in range(len(text), 14, -1):
        prefix = text[:end]
        if prefix.endswith(" ") or (end < len(text) and text[end].isalnum()):
            continue
        if _is_iban(prefix):
            return end
    return 0


def _is_ip(text: str) -> bool:
    return all(int(part) <= 255 for part in text.split("."))


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def _looks_random(text: str) -> bool:
    """A long token that is more likely a credential than a name."""
    if _HEX.fullmatch(text):
        return True
    classes = (
        any(c.islower() for c in text),
        any(c.isupper() for c in text),
        any(c.isdigit() for c in text),
    )
    return all(classes) and _entropy(text) >= MIN_ENTROPY_BITS


class Redactor:
    """Replaces secrets and personal data with placeholders, consistently for one run."""

    def __init__(self, known_personal: Iterable[str] = ()) -> None:
        self._originals: Dict[str, str] = {}  # placeholder -> value
        self._placeholders: Dict[Tuple[str, str], str] = {}
        self._counts: Dict[str, int] = {}
        names = sorted({n.strip() for n in known_personal if n and len(n.strip()) >= 3}, key=len)
        self._names: Optional[re.Pattern] = (
            re.compile(
                r"(?i)(?<!\w)(?:" + "|".join(re.escape(n) for n in reversed(names)) + r")(?!\w)"
            )
            if names
            else None
        )

    @property
    def found(self) -> Dict[str, int]:
        """How many distinct values of each kind were replaced. No values."""
        return dict(self._counts)

    def _placeholder(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._placeholders:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            name = f"[[{kind}_{self._counts[kind]}]]"
            self._placeholders[key] = name
            self._originals[name] = value
        return self._placeholders[key]

    def _swap(
        self,
        text: str,
        pattern: re.Pattern,
        kind: str,
        *,
        group: int = 0,
        accept: Optional[Callable[[str], bool]] = None,
    ) -> str:
        def replace(match: re.Match) -> str:
            value = match.group(group)
            if accept is not None and not accept(value):
                return match.group(0)
            whole = match.group(0)
            start = match.start(group) - match.start(0)
            return whole[:start] + self._placeholder(kind, value) + whole[start + len(value) :]

        return pattern.sub(replace, text)

    def _iban(self, match: re.Match) -> str:
        found = match.group(0)
        length = _iban_length(found)
        if not length:
            return found
        return self._placeholder(IBAN, found[:length]) + found[length:]

    def redact(self, text: object) -> str:
        """The text with every recognised secret and personal data item replaced."""
        out = str(text if text is not None else "")
        if not out:
            return out
        # Text that already looks like a placeholder must not be mistaken for one later.
        out = out.replace("[[", "[ [").replace("]]", "] ]")
        out = self._swap(out, _PRIVATE_KEY, SECRET)
        out = self._swap(out, _AUTH_SCHEME, SECRET, group=3)
        out = self._swap(out, _KEY_VALUE, SECRET, group=3)
        out = self._swap(out, _URL_CREDENTIALS, SECRET, group=2)
        out = self._swap(out, _TOKEN_SHAPES, SECRET)
        out = self._swap(out, _EMAIL, EMAIL)
        out = _IBAN.sub(self._iban, out)
        out = self._swap(out, _CARD, CARD, accept=_is_card)
        out = self._swap(out, _SSN, NATIONAL_ID)
        out = self._swap(out, _PHONE, PHONE)
        out = self._swap(out, _IPV4, IP, accept=_is_ip)
        if self._names is not None:
            out = self._swap(out, self._names, PERSON)
        return self._swap(out, _LONG_TOKEN, SECRET, accept=_looks_random)

    def restore(self, text: str, kinds: Iterable[str] = PERSONAL) -> str:
        """Put values of the given kinds back. Unknown placeholders are left as they are."""
        allowed = frozenset(kinds)

        def replace(match: re.Match) -> str:
            if match.group(1) not in allowed:
                return match.group(0)
            return self._originals.get(match.group(0), match.group(0))

        return _PLACEHOLDER.sub(replace, text)

    def values(self) -> List[str]:
        """Every value that was replaced. For tests; never log or send this."""
        return list(self._originals.values())
