"""The Splunk guard: what may be searched, and what is refused before Splunk sees it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.l1_agent.adapters.splunk.guard import (
    ALLOWED_COMMANDS,
    MAX_QUERY_LENGTH,
    GuardConfig,
    GuardRejected,
    guard,
    normalise_time_range,
    parse_duration,
)

CONFIG = GuardConfig(allowed_indexes=("app_logs", "batch_logs"), max_lookback_seconds=86400)
NOW = 1_800_000_000.0
SAMPLE_SOPS = Path(__file__).resolve().parents[3] / "data" / "sample_sops"


def run(query, time_range=None, max_results=None, config=CONFIG):
    return guard(query, time_range, max_results, config, now=NOW)


def rejected(query, time_range=None, max_results=None, config=CONFIG) -> str:
    with pytest.raises(GuardRejected) as caught:
        run(query, time_range, max_results, config)
    return str(caught.value)


class TestAllowed:
    def test_a_search_that_begins_with_index_gets_the_search_command_and_a_limit(self):
        result = run("index=app_logs ERROR")
        assert result.spl == "search index=app_logs ERROR | head 100"
        assert result.indexes == ("app_logs",)

    def test_a_search_that_begins_with_search_is_kept_as_written(self):
        assert run("search index=app_logs ERROR").spl == "search index=app_logs ERROR | head 100"

    @pytest.mark.parametrize(
        "query",
        [
            'index=batch_logs source=autosys "BATCH_PAYMENT_PROCESS" (FAILURE OR ERROR)',
            'index=app_logs sourcetype=log4j host=app-server-* "PaymentService" (ERROR OR FATAL)',
            'index=app_logs host=app-server-prod01 ("OutOfMemoryError" OR "OOM Killer")',
            "index=app_logs OR index=batch_logs ERROR",
            "index=app_logs ERROR OR FATAL",
            "search index IN (app_logs, batch_logs) ERROR",
            'index="app_logs" ERROR',
            "index=app_logs NOT DEBUG",
            "index=app_logs | stats count by host | sort -count | head 5",
            'index=app_logs | rex "code=(?<code>\\d+)" | where code > 400 | table _time, code',
            "index=app_logs | eval size=len(_raw) | dedup host | fields host, size",
            "index=app_logs | timechart span=5m count | tail 3",
            "index=app_logs | top limit=5 host | rare host",
            "index=app_logs | search index=batch_logs",
            'index=app_logs "a | delete" "[x]" "`m`"',
            'index=app_logs "say \\"hi\\" | delete"',
            'index=app_logs "earliest=-30d is only text here"',
        ],
    )
    def test_read_only_searches_pass(self, query):
        assert run(query).spl.endswith(" | head 100")

    def test_every_search_step_in_the_sample_sops_passes(self):
        seen = 0
        for path in sorted(SAMPLE_SOPS.glob("*.json")):
            for step in json.loads(path.read_text(encoding="utf-8"))["steps"]:
                if step["step_type"] != "SPLUNK_SEARCH":
                    continue
                query = step["parameters"]["query"].replace("${job_name}", "BATCH_PAYMENT_PROCESS")
                run(query, step["parameters"].get("time_range"))
                seen += 1
        assert seen >= 3

    def test_the_limit_is_the_smaller_of_the_step_and_the_setting(self):
        assert run("index=app_logs", max_results=5).max_results == 5
        assert run("index=app_logs", max_results=5000).spl.endswith("| head 100")

    def test_duplicate_indexes_are_reported_once(self):
        assert run("index=app_logs OR index=app_logs").indexes == ("app_logs",)


class TestHowASearchMustBegin:
    @pytest.mark.parametrize(
        "query",
        [
            "| tstats count where index=app_logs",
            "| rest /services/server/info",
            "| inputlookup users.csv",
            "ERROR index=app_logs",
            "stats count",
            "savedsearch nightly",
            "Search index=app_logs",
            "searchindex=app_logs",
            "",
            "   ",
        ],
    )
    def test_anything_else_is_refused(self, query):
        rejected(query)

    def test_a_search_that_is_not_text_is_refused(self):
        for query in (None, 5, ["index=app_logs"], {"q": "index=app_logs"}):
            rejected(query)


class TestIndexes:
    def test_a_search_must_name_an_index(self):
        assert "at least one index" in rejected("search ERROR")

    def test_an_index_off_the_allow_list_is_refused(self):
        assert "'_internal' is not on the allow list" in rejected("index=_internal")
        assert "not on the allow list" in rejected("index=app_logs OR index=main")
        assert "not on the allow list" in rejected("search index IN (app_logs, secrets)")
        assert "not on the allow list" in rejected("index=app_logs | search index=main")

    def test_an_empty_allow_list_denies_everything(self):
        assert "allow list" in rejected("index=app_logs", config=GuardConfig())

    @pytest.mark.parametrize(
        "query",
        [
            "index=*",
            "index=app*",
            "index=app_log?",
            'index="*"',
            "index=_*",
            "search index IN (app_logs, *)",
            "index=app_logs OR index=*",
        ],
    )
    def test_wildcard_indexes_are_refused(self, query):
        assert "wildcard" in rejected(query)

    @pytest.mark.parametrize(
        "query",
        [
            "index!=app_logs",
            "index=app_logs index!=batch_logs",
            "index::app_logs",
            "index==app_logs",
            "index>app_logs",
            "index=app_logs NOT index=batch_logs",
            "INDEX=app_logs",
            "index=app_logs Index=main",
            "index= | stats count",
            "index=",
            "index IN app_logs",
            "index IN ()",
            "index IN (app_logs",
            "index=app/logs",
            "index=app_logs;main",
            'index="app_logs main"',
        ],
    )
    def test_an_index_named_any_other_way_is_refused(self, query):
        rejected(query)

    @pytest.mark.parametrize(
        "query",
        [
            # OR binds tighter than the implied AND: each of these would also
            # search the role's default indexes for the other operand.
            "index=app_logs OR host=secret-host",
            "index=app_logs OR ERROR",
            "ERROR OR index=app_logs",
            "search host=a OR index=app_logs",
            "index=app_logs OR (index=batch_logs)",
            "(index=app_logs OR host=secret-host)",
            "(index=app_logs) ERROR",
            "index=app_logs (index=batch_logs OR sourcetype=x)",
            "index=app_logs OR",
        ],
    )
    def test_an_index_that_does_not_limit_the_whole_search_is_refused(self, query):
        rejected(query)


class TestCommands:
    REJECTED = [
        "delete",
        "collect index=app_logs",
        "outputlookup x.csv",
        "outputcsv x",
        "sendemail to=a@example.com",
        "script python x",
        "run x",
        "rest /services/authentication/users",
        'map search="search index=_audit"',
        "join host",
        "append",
        "appendcols",
        "union",
        "multisearch",
        "inputlookup users.csv",
        "inputcsv x",
        "lookup users user",
        "savedsearch nightly",
        "loadjob 1.2",
        "makeresults",
        "tstats count",
        "mstats avg(x)",
        "metadata type=hosts",
        "datamodel",
        "from datamodel:x",
        "pivot x y",
        'dbxquery query="select 1"',
        "dbinspect index=*",
        "eventcount index=*",
        "sendalert x",
        "summaryindex",
        "sichart count",
        "tscollect",
        "mcollect index=app_logs",
        "meventcollect",
        "foreach * ",
        "transaction host",
        "streamstats count",
        "eventstats count",
        "chart count by host",
        "rename host as h",
        'regex _raw="x"',
        "spath",
        "fillnull",
        "reverse",
        "abstract",
        "localop",
        "noop",
        "nosuchcmd",
        "DELETE",
        "Delete",
        '"delete"',
        "(delete)",
        "=delete",
    ]

    @pytest.mark.parametrize("command", REJECTED)
    def test_a_command_off_the_allow_list_is_refused(self, command):
        assert "not allowed" in rejected(f"index=app_logs | {command}")

    def test_the_allow_list_is_the_reviewed_one(self):
        assert ALLOWED_COMMANDS == {
            "search", "where", "eval", "stats", "table", "fields", "sort",
            "head", "tail", "rex", "dedup", "timechart", "top", "rare",
        }  # fmt: skip

    @pytest.mark.parametrize("command", sorted(ALLOWED_COMMANDS - {"search"}))
    def test_each_allowed_command_is_accepted_after_a_pipe(self, command):
        run(f"index=app_logs | {command} host")

    def test_a_refused_command_is_found_wherever_it_stands(self):
        assert "not allowed" in rejected("index=app_logs | stats count | delete")
        assert "not allowed" in rejected("index=app_logs|delete")
        assert "not allowed" in rejected("index=app_logs |\n delete")
        assert "not allowed" in rejected("index=app_logs |\tcollect index=app_logs")

    def test_an_empty_command_is_refused(self):
        for query in (
            "index=app_logs |",
            "index=app_logs || stats count",
            "index=app_logs | | head 1",
        ):
            assert "empty command" in rejected(query)

    @pytest.mark.parametrize(
        "query",
        [
            "index=app_logs [search index=_internal | fields host]",
            "index=app_logs | where host in [1]",
            "index=app_logs ]",
            "index=app_logs | eval x=[",
        ],
    )
    def test_subsearches_are_refused(self, query):
        assert "subsearch" in rejected(query)

    @pytest.mark.parametrize(
        "query",
        ["index=app_logs `my_macro`", "`all_indexes` index=app_logs", "index=app_logs ```note```"],
    )
    def test_macros_are_refused(self, query):
        assert "macro" in rejected(query) or "begin with" in rejected(query)


class TestQuoting:
    def test_an_unclosed_quote_is_refused(self):
        assert "unclosed quote" in rejected('index=app_logs "ERROR')
        assert "unclosed quote" in rejected('index=app_logs "ERROR\\"')

    def test_a_quote_that_closes_early_exposes_the_command_after_it(self):
        # What a value carrying a quote would do if it were spliced into a quoted term.
        assert "not allowed" in rejected('index=app_logs "X" | delete ""')

    def test_an_escaped_backslash_ends_the_string_where_splunk_ends_it(self):
        assert "not allowed" in rejected('index=app_logs "a\\\\" | delete')
        run('index=app_logs "a\\\\" | stats count')

    def test_single_quotes_do_not_hide_a_pipe(self):
        assert "not allowed" in rejected("index=app_logs 'a | delete'")

    def test_a_quoted_word_is_not_an_index_or_a_command(self):
        assert "at least one index" in rejected('search "index=app_logs"')
        assert run('index=app_logs "index=_internal" "OR"').indexes == ("app_logs",)

    def test_control_characters_are_refused(self):
        for bad in ("\x00", "\x1b", "\x08", "\x7f"):
            assert "control characters" in rejected(f"index=app_logs {bad}| delete")

    def test_unbalanced_parentheses_are_refused(self):
        assert "parentheses" in rejected("index=app_logs (ERROR")
        assert "parentheses" in rejected("index=app_logs ERROR)")


class TestLongInputs:
    def test_a_search_over_the_limit_is_refused(self):
        assert "longer than" in rejected("index=app_logs " + "A" * MAX_QUERY_LENGTH)

    def test_a_search_at_the_limit_is_checked_quickly(self):
        query = "index=app_logs " + ("(a OR b) " * 440)
        assert MAX_QUERY_LENGTH - 40 < len(query) <= MAX_QUERY_LENGTH
        run(query)

    def test_many_pipes_are_refused(self):
        assert "too many commands" in rejected("index=app_logs" + " | head 1" * 50)

    def test_a_huge_input_is_refused_without_being_scanned(self):
        assert "longer than" in rejected('index=app_logs "' + "\\" * 5_000_000)

    def test_a_long_time_value_is_refused(self):
        assert "too long" in rejected("index=app_logs", {"earliest": "-1h" * 100})


class TestTimeRange:
    def test_nothing_given_is_the_longest_lookback(self):
        result = run("index=app_logs")
        assert (result.earliest, result.latest, result.clamped) == ("-86400s", "now", False)

    def test_the_dict_shape_is_kept_when_inside_the_lookback(self):
        result = run("index=app_logs", {"earliest": "-4h", "latest": "now"})
        assert (result.earliest, result.latest, result.clamped) == ("-4h", "now", False)

    @pytest.mark.parametrize("text", ["now-1h", "-1h", " NOW-1H "])
    def test_the_string_shape_is_read_as_the_earliest_time(self, text):
        result = run("index=app_logs", text)
        assert (result.earliest, result.latest) == ("-1h", "now")

    @pytest.mark.parametrize("earliest", ["-7d", "-25h", "-2000m", "-1mon", "-1y", "-1d@d", "0", 1])
    def test_a_longer_lookback_is_clamped(self, earliest):
        result = run("index=app_logs", {"earliest": earliest})
        assert (result.earliest, result.clamped) == ("-86400s", True)

    @pytest.mark.parametrize("earliest", ["-24h", "-1d", "-90m", "-30s", "-4h@h", "-1h-30m", "@h"])
    def test_a_lookback_inside_the_limit_is_not_changed(self, earliest):
        result = run("index=app_logs", {"earliest": earliest})
        assert (result.earliest, result.clamped) == (earliest, False)

    def test_epoch_seconds_inside_the_lookback_are_kept(self):
        result = run("index=app_logs", {"earliest": str(int(NOW) - 600), "latest": int(NOW) - 60})
        assert (result.earliest, result.latest) == (str(int(NOW) - 600), str(int(NOW) - 60))

    @pytest.mark.parametrize(
        "time_range",
        [
            {"earliest": "-1h", "latest": "-2h"},
            {"earliest": "-1h", "latest": "-30d"},
            {"latest": "-2d"},
            {"earliest": "+1h"},
            {"earliest": "-1h", "latest": "+1d"},
            {"earliest": str(int(NOW) + 600)},
            {"earliest": "yesterday"},
            {"earliest": "2026-01-01T00:00:00"},
            {"earliest": "-1h | delete"},
            {"earliest": "-1h", "index_earliest": "-90d"},
            {"earliest": ["-1h"]},
            {"earliest": True},
            {"earliest": -3600},
            ["-1h", "now"],
            42,
        ],
    )
    def test_a_time_range_the_guard_cannot_bound_is_refused(self, time_range):
        rejected("index=app_logs", time_range)

    @pytest.mark.parametrize(
        "term",
        [
            "earliest=-30d",
            "latest=-1d",
            "earliest = -30d",
            'earliest="-30d"',
            "EARLIEST=-30d",
            "_index_earliest=-30d",
            "_index_latest=now",
            "starttime=01/01/2020:00:00:00",
            "endtime=01/01/2021:00:00:00",
            "startdaysago=90",
            "daysago=90",
            "hoursago=900",
            "timeformat=%s",
        ],
    )
    def test_a_time_written_in_the_search_is_refused(self, term):
        assert "give the time range instead" in rejected(f"index=app_logs {term}")
        assert "give the time range instead" in rejected(f"index=app_logs | search {term}")

    def test_parse_duration(self):
        assert parse_duration("24h") == 86400
        assert parse_duration(" 90M ") == 5400
        assert parse_duration("7d") == 604800
        for bad in ("", "0h", "-1h", "24", "h", "1h | delete", "1.5h"):
            with pytest.raises(ValueError):
                parse_duration(bad)

    def test_normalise_uses_the_clock_when_none_is_given(self):
        assert normalise_time_range({"earliest": "-1h"}, 86400) == ("-1h", "now", False)


class TestMaxResults:
    @pytest.mark.parametrize("value", [0, -1, "10", 1.5, True, [5]])
    def test_a_limit_that_is_not_a_positive_whole_number_is_refused(self, value):
        assert "max_results" in rejected("index=app_logs", max_results=value)
