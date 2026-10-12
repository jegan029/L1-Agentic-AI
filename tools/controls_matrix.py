"""Check that the controls matrix points at things that exist and tests that pass.

    python -m tools.controls_matrix                       files and names only
    python -m tools.controls_matrix --junit results.xml   and the outcome of each test named
    python -m tools.controls_matrix --junit results.xml --strict
    python -m tools.controls_matrix --run                 run the named tests, then check them

For every row of docs/controls-matrix.md whose status begins "Implemented":

  - each path in its Code and Test cells exists;
  - each name beside a path (a function, class, constraint, test) is in one of the
    files that cell names;
  - a `CI job` named in its Test cell is a job in a workflow under .github/workflows;
  - with test results: each test the row names ran, and none failed. With --strict a
    skipped test is a failure too (CI has PostgreSQL, SAML and helm, so nothing is
    skipped there for want of them).

A row marked Partial, Not built or anything else is not checked: the matrix says in
words what those rows lack. This tool does not judge whether a control is adequate. It
stops the matrix claiming "implemented" for something that is absent or failing.

How a cell is read. Text in backticks is a path if its first segment is a file or
directory at the repository root; a directory is written with a trailing slash. Other
backticked text is a name, unless it contains a space (a command, quoted for the reader).
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "controls-matrix.md"
WORKFLOWS = ROOT / ".github" / "workflows"
_ROW = re.compile(r"^\|\s*([A-Z]{2}-\d{2})\s*\|(.*)\|\s*$")
_TICK = re.compile(r"`([^`]+)`")
_CI_JOB = re.compile(r"CI jobs?\s+((?:`[^`]+`(?:\s*(?:,|and)\s*)?)+)")
_JOB_ID = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$", re.MULTILINE)


@dataclass
class Row:
    id: str
    control: str
    code: str
    test: str
    status: str

    @property
    def implemented(self) -> bool:
        return self.status.strip().lower().startswith("implemented")


@dataclass
class Selection:
    """Tests a row names: a whole file, or one class, function or method in it."""

    row: str
    path: str
    name: str = ""
    class_name: str = ""  # set when `name` is a method

    @property
    def module(self) -> str:
        return self.path[: -len(".py")].replace("/", ".")

    def label(self) -> str:
        return f"{self.path}::{self.name}" if self.name else self.path


@dataclass
class Report:
    rows: List[Row] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    selections: List[Selection] = field(default_factory=list)


def parse(text: str) -> List[Row]:
    """The rows of the control catalogue: six-column table rows that start with a control id."""
    rows: List[Row] = []
    for line in text.splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        cells = [c.strip() for c in match.group(2).split("|")]
        if len(cells) != 5:
            continue  # another table that happens to start with a control id
        control, _maps_to, code, test, status = cells
        rows.append(Row(match.group(1), control, code, test, status))
    return rows


def _is_path(token: str, root: Path) -> bool:
    if " " in token or token.startswith("/") or "::" in token:
        return False  # a command, a URL path, or a test's node id
    first = token.split("/", 1)[0]
    return bool(first) and (root / first).exists() and ("/" in token or (root / token).exists())


def _tokens(cell: str, root: Path) -> Tuple[List[str], List[str]]:
    """(paths, names) written in backticks in a cell. Commands, which contain a space, are prose."""
    paths, names = [], []
    for token in _TICK.findall(cell):
        token = token.strip()
        if _is_path(token, root):
            paths.append(token)
        elif " " not in token:
            names.append(token.split("::")[-1])  # of a node id, the test's own name
    return paths, names


def _defined(path: Path) -> Dict[str, str]:
    """Every class, function and method a Python file defines: name -> enclosing class or ''."""
    found: Dict[str, str] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = ""
        elif isinstance(node, ast.ClassDef):
            found[node.name] = ""
            for inner in node.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.setdefault(inner.name, node.name)
    return found


def _ci_jobs(root: Path) -> Set[str]:
    jobs: Set[str] = set()
    workflows = root / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")) if workflows.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        _, _, after = text.partition("\njobs:")
        jobs.update(_JOB_ID.findall(after))
    return jobs


def check_static(rows: Sequence[Row], root: Path = ROOT) -> Report:
    """Everything that can be checked without running a test."""
    report = Report(rows=list(rows))
    jobs = _ci_jobs(root)
    seen: Set[str] = set()
    for row in rows:
        if row.id in seen:
            report.problems.append(f"{row.id}: appears twice")
        seen.add(row.id)
        if not row.implemented:
            continue
        for label, cell in (("Code", row.code), ("Test", row.test)):
            ci_names: Set[str] = set()
            for group in _CI_JOB.findall(cell):
                ci_names.update(_TICK.findall(group))
            paths, names = _tokens(cell, root)
            if not paths and not ci_names:
                report.problems.append(f"{row.id}: the {label} cell names no file and no CI job")
                continue
            texts: Dict[str, str] = {}
            for path in paths:
                target = root / path
                if not target.exists():
                    report.problems.append(
                        f"{row.id}: {label} names `{path}`, which does not exist"
                    )
                elif target.is_file():
                    texts[path] = target.read_text(encoding="utf-8", errors="replace")
                else:
                    for inner in sorted(target.rglob("*")):
                        if inner.is_file() and inner.stat().st_size < 2_000_000:
                            texts[inner.relative_to(root).as_posix()] = inner.read_text(
                                encoding="utf-8", errors="replace"
                            )
            for name in ci_names:
                if name not in jobs:
                    report.problems.append(f"{row.id}: no CI job is called `{name}`")
            for name in names:
                if name in ci_names:
                    continue
                if not any(name in text for text in texts.values()):
                    report.problems.append(
                        f"{row.id}: {label} names `{name}`, which is in none of the files beside it"
                    )
        report.selections += _selections(row, root, report.problems)
    return report


def _selections(row: Row, root: Path, problems: List[str]) -> List[Selection]:
    """The tests a row's Test cell names: each test file, narrowed by the names after it."""
    found: List[Selection] = []
    pieces = re.split(r"(`[^`]+`)", row.test)
    current: Optional[str] = None
    narrowed: Dict[str, List[Selection]] = {}
    order: List[str] = []
    for piece in pieces:
        if not piece.startswith("`"):
            continue
        token = piece.strip("`").strip()
        if _is_path(token, root):
            is_test = token.endswith(".py") and "/test_" in f"/{token}" and (root / token).is_file()
            current = token if is_test else None
            if is_test and token not in narrowed:
                narrowed[token] = []
                order.append(token)
            continue
        if current is None or " " in token:
            continue
        defined = _defined(root / current)
        name = token.split("::")[-1]
        if name in defined:
            narrowed[current].append(Selection(row.id, current, name, defined[name]))
        elif name.startswith(("Test", "test_")):
            problems.append(f"{row.id}: `{current}` defines nothing called `{name}`")
    for path in order:
        found += narrowed[path] or [Selection(row.id, path)]
    return found


# ── Test results ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Case:
    classname: str
    name: str
    outcome: str  # passed | failed | skipped


def read_junit(path: Path) -> List[Case]:
    cases: List[Case] = []
    # The file is the one pytest wrote a moment ago in the same job, not input from outside.
    for case in ElementTree.parse(path).getroot().iter("testcase"):  # nosec B314
        tags = {child.tag for child in case}
        outcome = (
            "failed"
            if tags & {"failure", "error"}
            else "skipped"
            if "skipped" in tags
            else "passed"
        )
        cases.append(Case(case.get("classname", ""), case.get("name", ""), outcome))
    return cases


def _matches(selection: Selection, case: Case) -> bool:
    module, bare = selection.module, case.name.split("[", 1)[0]
    if not selection.name:
        return case.classname == module or case.classname.startswith(module + ".")
    if selection.class_name:
        return case.classname == f"{module}.{selection.class_name}" and bare == selection.name
    if selection.name.startswith("Test"):
        return case.classname == f"{module}.{selection.name}"
    return case.classname == module and bare == selection.name


def check_results(
    selections: Sequence[Selection], cases: Sequence[Case], *, strict: bool
) -> List[str]:
    problems: List[str] = []
    for selection in selections:
        ran = [c for c in cases if _matches(selection, c)]
        where = f"{selection.row}: {selection.label()}"
        if not ran:
            problems.append(f"{where} did not run")
            continue
        failed = [c for c in ran if c.outcome == "failed"]
        skipped = [c for c in ran if c.outcome == "skipped"]
        if failed:
            problems.append(f"{where} has {len(failed)} failing test(s), e.g. {failed[0].name}")
        elif strict and skipped:
            problems.append(f"{where} has {len(skipped)} skipped test(s), e.g. {skipped[0].name}")
        elif len(skipped) == len(ran):
            problems.append(f"{where}: every test was skipped, so nothing was shown")
    return problems


def _run_tests(selections: Sequence[Selection], root: Path) -> Path:
    files = sorted({s.path for s in selections})
    out = Path(tempfile.mkdtemp(prefix="controls-")) / "results.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={out}",
            *files,
        ],
        cwd=root,
        check=False,
    )
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="controls_matrix", description=__doc__.splitlines()[0])
    parser.add_argument("--junit", help="JUnit XML from the test run to check against")
    parser.add_argument("--run", action="store_true", help="run the named tests first")
    parser.add_argument("--strict", action="store_true", help="a skipped test is a failure")
    parser.add_argument("--matrix", default=str(MATRIX))
    args = parser.parse_args(argv)

    rows = parse(Path(args.matrix).read_text(encoding="utf-8"))
    report = check_static(rows)
    implemented = [r for r in rows if r.implemented]
    problems = list(report.problems)
    junit = Path(args.junit) if args.junit else None
    if args.run:
        junit = _run_tests(report.selections, ROOT)
    if junit is not None:
        if not junit.is_file():
            problems.append(f"{junit} does not exist: the tests did not produce results")
        else:
            problems += check_results(report.selections, read_junit(junit), strict=args.strict)

    by_status: Dict[str, int] = {}
    for row in rows:
        word = re.split(r"[\s(:;,.]", row.status.strip(), maxsplit=1)[0] or "(none)"
        by_status[word] = by_status.get(word, 0) + 1
    print(
        f"{len(rows)} controls: "
        + ", ".join(f"{count} {word.lower()}" for word, count in sorted(by_status.items()))
    )
    print(
        f"{len(implemented)} marked implemented name {len(report.selections)} test selection(s)"
        + ("" if junit else "; test results were not checked (give --junit or --run)")
    )
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("OK" if not problems else f"FAILED: {len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
