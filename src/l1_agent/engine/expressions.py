"""Typed expression language for DECISION rules and VERIFY assertions.

Deliberately small and never evaluated with eval(). Grammar:

    expr    := or
    or      := and ("or" and)*
    and     := unary ("and" unary)*
    unary   := "not" unary | compare
    compare := operand (OP operand)?        OP: == != < <= > >= in "not in"
    operand := literal | path | "(" expr ")"
    literal := number | "string" | 'string' | true | false | null | [literal, ...]
    path    := NAME ("." NAME | "[" (string | integer) "]")*

Paths resolve against a context dict, for example ``result.current_depth``,
``steps["step-2"].jobs[0].status`` or ``all_success``. A missing path, a
type mismatch or a non-boolean result raises ExpressionError; callers treat
that as "cannot continue", never as true.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Tuple

MAX_EXPRESSION_LENGTH = 500


class ExpressionError(ValueError):
    """The expression is malformed or could not be evaluated."""


_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<number>-?\d+(?:\.\d+)?)
      | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<op>==|!=|<=|>=|<|>)
      | (?P<punct>[()\[\].,])
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)
_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}
_Token = Tuple[str, Any]


def _tokenize(text: str) -> List[_Token]:
    if not isinstance(text, str) or not text.strip():
        raise ExpressionError("expression is empty")
    if len(text) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError("expression is too long")
    tokens: List[_Token] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ExpressionError(
                f"unexpected character at position {pos}: {text[pos : pos + 10]!r}"
            )
        pos = m.end()
        if m.group("number") is not None:
            raw = m.group("number")
            tokens.append(("lit", float(raw) if "." in raw else int(raw)))
        elif m.group("string") is not None:
            body = m.group("string")[1:-1]
            tokens.append(("lit", re.sub(r"\\(.)", r"\1", body)))
        elif m.group("op") is not None:
            tokens.append(("op", m.group("op")))
        elif m.group("punct") is not None:
            tokens.append(("punct", m.group("punct")))
        else:
            name = m.group("name")
            tokens.append(("kw" if name in _KEYWORDS else "name", name))
    return tokens


class _Parser:
    def __init__(self, tokens: List[_Token]) -> None:
        self._t = tokens
        self._i = 0

    def _peek(self) -> _Token:
        return self._t[self._i] if self._i < len(self._t) else ("end", None)

    def _next(self) -> _Token:
        tok = self._peek()
        self._i += 1
        return tok

    def _accept(self, kind: str, value: Any) -> bool:
        if self._peek() == (kind, value):
            self._i += 1
            return True
        return False

    def _expect(self, kind: str, value: Any) -> None:
        if not self._accept(kind, value):
            raise ExpressionError(f"expected {value!r} but found {self._peek()[1]!r}")

    def parse(self) -> tuple:
        node = self._or()
        if self._peek()[0] != "end":
            raise ExpressionError(f"unexpected token {self._peek()[1]!r}")
        return node

    def _or(self) -> tuple:
        node = self._and()
        while self._accept("kw", "or"):
            node = ("or", node, self._and())
        return node

    def _and(self) -> tuple:
        node = self._unary()
        while self._accept("kw", "and"):
            node = ("and", node, self._unary())
        return node

    def _unary(self) -> tuple:
        if self._accept("kw", "not"):
            return ("not", self._unary())
        return self._compare()

    def _compare(self) -> tuple:
        left = self._operand()
        kind, value = self._peek()
        if kind == "op":
            self._next()
            return ("cmp", value, left, self._operand())
        if (kind, value) == ("kw", "in"):
            self._next()
            return ("cmp", "in", left, self._operand())
        if (kind, value) == ("kw", "not"):
            self._next()
            self._expect("kw", "in")
            return ("cmp", "not in", left, self._operand())
        return left

    def _operand(self) -> tuple:
        kind, value = self._next()
        if kind == "lit":
            return ("lit", value)
        if kind == "kw" and value in ("true", "false", "null"):
            return ("lit", {"true": True, "false": False, "null": None}[value])
        if (kind, value) == ("punct", "("):
            node = self._or()
            self._expect("punct", ")")
            return node
        if (kind, value) == ("punct", "["):
            items: List[Any] = []
            if not self._accept("punct", "]"):
                while True:
                    k, v = self._next()
                    if k == "lit":
                        items.append(v)
                    elif k == "kw" and v in ("true", "false", "null"):
                        items.append({"true": True, "false": False, "null": None}[v])
                    else:
                        raise ExpressionError("list literals may contain only literals")
                    if self._accept("punct", "]"):
                        break
                    self._expect("punct", ",")
            return ("lit", items)
        if kind == "name":
            parts: List[Any] = [value]
            while True:
                if self._accept("punct", "."):
                    k, v = self._next()
                    if k not in ("name", "kw"):
                        raise ExpressionError("expected a name after '.'")
                    parts.append(v)
                elif self._accept("punct", "["):
                    k, v = self._next()
                    if k != "lit" or isinstance(v, (float, bool)) or not isinstance(v, (str, int)):
                        raise ExpressionError("index must be a string or an integer")
                    parts.append(v)
                    self._expect("punct", "]")
                else:
                    break
            return ("path", tuple(parts))
        raise ExpressionError(f"unexpected token {value!r}")


def parse(text: str) -> tuple:
    """Parse an expression into an AST. Raises ExpressionError if malformed."""
    return _Parser(_tokenize(text)).parse()


def root_names(text: str) -> List[str]:
    """Return the first segment of every path used, for static validation."""
    names: List[str] = []

    def walk(node: tuple) -> None:
        if node[0] == "path":
            names.append(node[1][0])
        elif node[0] in ("or", "and"):
            walk(node[1])
            walk(node[2])
        elif node[0] == "not":
            walk(node[1])
        elif node[0] == "cmp":
            walk(node[2])
            walk(node[3])

    walk(parse(text))
    return names


def _resolve(path: tuple, context: Mapping[str, Any]) -> Any:
    current: Any = context
    walked: List[str] = []
    for part in path:
        walked.append(str(part))
        if isinstance(current, Mapping):
            if part not in current:
                raise ExpressionError(f"'{'.'.join(walked)}' is not present")
            current = current[part]
        elif isinstance(current, (list, tuple)) and isinstance(part, int):
            if not -len(current) <= part < len(current):
                raise ExpressionError(f"index out of range at '{'.'.join(walked)}'")
            current = current[part]
        else:
            raise ExpressionError(f"cannot read '{part}' at '{'.'.join(walked)}'")
    return current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ExpressionError(f"{what} must be true or false, got {type(value).__name__}")
    return value


def _eval(node: tuple, context: Mapping[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        return _resolve(node[1], context)
    if kind == "not":
        return not _as_bool(_eval(node[1], context), "operand of 'not'")
    if kind == "and":
        return _as_bool(_eval(node[1], context), "operand of 'and'") and _as_bool(
            _eval(node[2], context), "operand of 'and'"
        )
    if kind == "or":
        return _as_bool(_eval(node[1], context), "operand of 'or'") or _as_bool(
            _eval(node[2], context), "operand of 'or'"
        )
    if kind == "cmp":
        op, left, right = node[1], _eval(node[2], context), _eval(node[3], context)
        if op in ("in", "not in"):
            if not isinstance(right, (list, tuple, str)):
                raise ExpressionError("right side of 'in' must be a list or a string")
            if isinstance(right, str) and not isinstance(left, str):
                raise ExpressionError("left side of 'in' must be a string for a string search")
            found = left in right
            return found if op == "in" else not found
        if op in ("==", "!="):
            comparable = (
                left is None
                or right is None
                or (_is_number(left) and _is_number(right))
                or type(left) is type(right)
            )
            if not comparable:
                # Comparing unlike types is almost always an authoring mistake.
                raise ExpressionError(
                    f"cannot compare {type(left).__name__} with {type(right).__name__}"
                )
            return (left == right) if op == "==" else (left != right)
        if not (_is_number(left) and _is_number(right)) and not (
            isinstance(left, str) and isinstance(right, str)
        ):
            raise ExpressionError(
                f"cannot order {type(left).__name__} against {type(right).__name__}"
            )
        return {"<": left < right, "<=": left <= right, ">": left > right, ">=": left >= right}[op]
    raise ExpressionError(f"unknown node {kind!r}")


def evaluate(text: str, context: Mapping[str, Any]) -> bool:
    """Evaluate an expression to a boolean. Anything else is an ExpressionError."""
    return _as_bool(_eval(parse(text), context), "expression result")
