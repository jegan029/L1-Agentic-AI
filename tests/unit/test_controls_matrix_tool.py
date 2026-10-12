"""The check that keeps the controls matrix honest (Phase 6 exit criterion 2).

Two things are tested: that the real matrix passes the checks that need no test run,
and that the tool fails when a row says "implemented" and is not, using small matrices
written here against a small pretend repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import controls_matrix as cm
from tools.controls_matrix import Case, Row, Selection

HEADER = "| Id | Control | Maps to | Code | Test | Status |\n|---|---|---|---|---|---|\n"


def row(code: str, test: str, status: str = "Implemented (6)", row_id: str = "ZZ-01") -> str:
    return f"| {row_id} | A control | SOC 2 | {code} | {test} | {status} |\n"


@pytest.fixture
def repo(tmp_path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "thing.py").write_text(
        "LIMIT = 3\n\ndef guard():\n    return LIMIT\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_thing.py").write_text(
        "def test_alone():\n    pass\n\n\nclass TestGuard:\n    def test_holds(self):\n        pass\n",
        encoding="utf-8",
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "name: CI\non: push\njobs:\n  lint:\n    runs-on: x\n  deps:\n    runs-on: x\n",
        encoding="utf-8",
    )
    return tmp_path


def problems(repo: Path, text: str) -> list:
    return cm.check_static(cm.parse(HEADER + text), repo).problems


class TestTheRealMatrix:
    def test_every_implemented_row_names_files_and_names_that_exist(self):
        rows = cm.parse(cm.MATRIX.read_text(encoding="utf-8"))
        report = cm.check_static(rows)
        assert report.problems == []
        implemented = [r for r in rows if r.implemented]
        assert len(rows) >= 50 and len(implemented) >= 40
        assert len(report.selections) >= 60, "implemented rows name tests to run"

    def test_no_row_is_left_as_planned(self):
        """After Phase 6 every row says what exists: implemented, partial, or not built."""
        rows = cm.parse(cm.MATRIX.read_text(encoding="utf-8"))
        words = {r.id: r.status.split()[0].rstrip(":;,.(") for r in rows}
        assert set(words.values()) <= {"Implemented", "Partial", "Not"}, words
        assert not [r.id for r in rows if "planned" in r.status.lower()]

    def test_ids_are_unique_and_every_row_has_a_status(self):
        rows = cm.parse(cm.MATRIX.read_text(encoding="utf-8"))
        assert len({r.id for r in rows}) == len(rows)
        assert all(r.status.strip() for r in rows)


class TestWhatItCatches:
    def test_a_well_formed_row_passes(self, repo):
        text = row(
            "`src/thing.py` (`guard`, `LIMIT`)",
            "`tests/test_thing.py` (`TestGuard`), CI job `deps`",
        )
        assert problems(repo, text) == []

    def test_a_file_that_does_not_exist(self, repo):
        found = problems(repo, row("`src/gone.py`", "`tests/test_thing.py`"))
        assert found == ["ZZ-01: Code names `src/gone.py`, which does not exist"]
        found = problems(repo, row("`src/thing.py`", "`tests/test_gone.py`"))
        assert found == ["ZZ-01: Test names `tests/test_gone.py`, which does not exist"]

    def test_a_name_that_is_not_in_the_file_beside_it(self, repo):
        found = problems(repo, row("`src/thing.py` (`removed_guard`)", "`tests/test_thing.py`"))
        assert found == [
            "ZZ-01: Code names `removed_guard`, which is in none of the files beside it"
        ]

    def test_a_test_class_or_function_that_was_renamed_or_removed(self, repo):
        found = problems(repo, row("`src/thing.py`", "`tests/test_thing.py` (`TestGone`)"))
        assert "ZZ-01: `tests/test_thing.py` defines nothing called `TestGone`" in found

    def test_a_ci_job_that_does_not_exist(self, repo):
        found = problems(repo, row("`src/thing.py`", "CI job `sbom`"))
        assert found == ["ZZ-01: no CI job is called `sbom`"]
        assert problems(repo, row("`src/thing.py`", "CI jobs `lint` and `deps`")) == []

    def test_a_cell_that_names_nothing(self, repo):
        found = problems(repo, row("see the code", "`tests/test_thing.py`"))
        assert found == ["ZZ-01: the Code cell names no file and no CI job"]
        found = problems(repo, row("`src/thing.py`", "manual review"))
        assert found == ["ZZ-01: the Test cell names no file and no CI job"]

    def test_rows_that_do_not_claim_to_be_implemented_are_left_alone(self, repo):
        for status in ("Partial (6): half of it", "Not built", "Planned (7)"):
            assert problems(repo, row("`src/gone.py`", "`tests/test_gone.py`", status)) == []

    def test_a_duplicate_id(self, repo):
        text = row("`src/thing.py`", "`tests/test_thing.py`") * 2
        assert "ZZ-01: appears twice" in problems(repo, text)

    def test_a_command_in_backticks_is_prose_and_a_directory_is_a_path(self, repo):
        text = row("`src/` (`guard`)", "`tests/test_thing.py`, run with `python -m pytest tests`")
        assert problems(repo, text) == []

    def test_other_tables_that_start_with_a_control_id_are_ignored(self, repo):
        rows = cm.parse(
            HEADER + row("`src/thing.py`", "`tests/test_thing.py`") + "| ZZ-01 | a | b |\n"
        )
        assert [r.id for r in rows] == ["ZZ-01"]


class TestWhichTestsARowNames:
    def selections(self, repo: Path, test_cell: str) -> list:
        report = cm.check_static(cm.parse(HEADER + row("`src/thing.py`", test_cell)), repo)
        return [(s.path, s.name, s.class_name) for s in report.selections]

    def test_a_file_alone_means_every_test_in_it(self, repo):
        assert self.selections(repo, "`tests/test_thing.py`") == [("tests/test_thing.py", "", "")]

    def test_names_after_a_file_narrow_it(self, repo):
        found = self.selections(
            repo, "`tests/test_thing.py` (`TestGuard`, `test_alone`, `test_holds`)"
        )
        assert found == [
            ("tests/test_thing.py", "TestGuard", ""),
            ("tests/test_thing.py", "test_alone", ""),
            ("tests/test_thing.py", "test_holds", "TestGuard"),
        ]

    def test_a_node_id_is_read_by_its_last_part(self, repo):
        found = self.selections(repo, "`tests/test_thing.py` (`tests/test_thing.py::TestGuard`)")
        assert ("tests/test_thing.py", "TestGuard", "") in found


class TestResults:
    FILE = Selection("ZZ-01", "tests/test_thing.py")
    CLASS = Selection("ZZ-01", "tests/test_thing.py", "TestGuard")
    FUNCTION = Selection("ZZ-01", "tests/test_thing.py", "test_alone")
    METHOD = Selection("ZZ-01", "tests/test_thing.py", "test_holds", "TestGuard")
    PASSED = [
        Case("tests.test_thing", "test_alone", "passed"),
        Case("tests.test_thing.TestGuard", "test_holds[sqlite]", "passed"),
        Case("tests.test_thing.TestGuard", "test_holds[postgres]", "passed"),
        Case("tests.test_other", "test_alone", "failed"),
    ]

    def check(self, selections, cases, strict=False) -> list:
        return cm.check_results(selections, cases, strict=strict)

    def test_everything_named_ran_and_passed(self):
        every = [self.FILE, self.CLASS, self.FUNCTION, self.METHOD]
        assert self.check(every, self.PASSED, strict=True) == []

    def test_a_failing_test_fails_the_row_that_names_it(self):
        cases = [
            Case("tests.test_thing.TestGuard", "test_holds[postgres]", "failed")
        ] + self.PASSED[:2]
        assert self.check([self.FUNCTION], cases) == []
        for selection in (self.FILE, self.CLASS, self.METHOD):
            found = self.check([selection], cases)
            assert len(found) == 1 and "1 failing test(s)" in found[0], selection

    def test_a_test_that_did_not_run_is_not_a_pass(self):
        found = self.check([self.CLASS], [Case("tests.test_thing", "test_alone", "passed")])
        assert found == ["ZZ-01: tests/test_thing.py::TestGuard did not run"]
        assert self.check([self.FILE], []) == ["ZZ-01: tests/test_thing.py did not run"]

    def test_a_skip_is_tolerated_only_when_not_strict_and_never_when_nothing_ran(self):
        mixed = [
            Case("tests.test_thing.TestGuard", "test_holds[sqlite]", "passed"),
            Case("tests.test_thing.TestGuard", "test_holds[postgres]", "skipped"),
        ]
        assert self.check([self.CLASS], mixed) == []
        assert "1 skipped test(s)" in self.check([self.CLASS], mixed, strict=True)[0]
        only_skipped = [Case("tests.test_thing.TestGuard", "test_holds[postgres]", "skipped")]
        assert "every test was skipped" in self.check([self.CLASS], only_skipped)[0]

    def test_results_are_read_from_junit_xml(self, tmp_path):
        xml = tmp_path / "results.xml"
        xml.write_text(
            '<testsuites><testsuite name="pytest">'
            '<testcase classname="tests.test_thing" name="test_alone" time="0.1"/>'
            '<testcase classname="tests.test_thing.TestGuard" name="test_holds"><failure>x</failure></testcase>'
            '<testcase classname="tests.test_thing" name="test_other"><skipped/></testcase>'
            '<testcase classname="tests.test_thing" name="test_broken"><error>x</error></testcase>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        assert [c.outcome for c in cm.read_junit(xml)] == ["passed", "failed", "skipped", "failed"]


class TestCommand:
    def write(self, tmp_path: Path, text: str) -> str:
        path = tmp_path / "matrix.md"
        path.write_text(HEADER + text, encoding="utf-8")
        return str(path)

    def test_exit_status_follows_the_problems(self, tmp_path, capsys):
        good = self.write(
            tmp_path, row("`tools/controls_matrix.py` (`check_static`)", "CI job `tests`")
        )
        assert cm.main(["--matrix", good]) == 0
        assert "OK" in capsys.readouterr().out
        bad = self.write(tmp_path, row("`tools/not_there.py`", "CI job `tests`"))
        assert cm.main(["--matrix", bad]) == 1
        out = capsys.readouterr().out
        assert "PROBLEM: ZZ-01" in out and "FAILED: 1 problem(s)" in out

    def test_missing_results_are_a_failure_not_a_pass(self, tmp_path, capsys):
        good = self.write(
            tmp_path,
            row(
                "`tools/controls_matrix.py`",
                "`tests/unit/test_controls_matrix_tool.py` (`TestCommand`)",
            ),
        )
        assert cm.main(["--matrix", good, "--junit", str(tmp_path / "none.xml")]) == 1
        assert "did not produce results" in capsys.readouterr().out

    def test_a_row_is_an_object_that_knows_whether_it_claims_to_be_implemented(self):
        assert Row("A", "", "", "", "Implemented (2): yes").implemented
        assert Row("A", "", "", "", " implemented").implemented
        assert not Row("A", "", "", "", "Partial (2)").implemented
        assert not Row("A", "", "", "", "Not built").implemented
