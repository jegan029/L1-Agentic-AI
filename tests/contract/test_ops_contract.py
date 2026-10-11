"""The seven operations connectors against recorded answers (Phase 3 exit criterion 2).

HTTP connectors talk to a replay server; command-line and terminal
connectors are given recorded command output and screens. Every fixture was
written from vendor documentation, not captured from a live system, so these
tests fix the connector's side of the contract only.
"""

from __future__ import annotations

from typing import List

import pytest

from src.l1_agent.adapters.autosys_adapter import AutosysAdapter
from src.l1_agent.adapters.mainframe_adapter import MainframeAdapter
from src.l1_agent.adapters.webui_scraper_adapter import WebUIScraperAdapter
from src.l1_agent.adapters.windows_share_adapter import WindowsShareAdapter
from src.l1_agent.connectors.ops.autosys import AutosysConnector
from src.l1_agent.connectors.ops.browser import BrowserConnector
from src.l1_agent.connectors.ops.dynatrace import DynatraceConnector
from src.l1_agent.connectors.ops.fileshare import FileShareConnector
from src.l1_agent.connectors.ops.ibm_mq import IbmMqConnector
from src.l1_agent.connectors.ops.mainframe import MainframeConnector
from src.l1_agent.connectors.ops.splunk import SplunkConnector
from src.l1_agent.connectors.sdk.base import Refused, StepContext
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, FileBackend, SecretResolver
from tests.contract.replay import FIXTURES, Replay, load

CTX = StepContext(None, "run-1", "step-1")


def secrets_in(tmp_path, **values: str):
    """Credential files and the references to them, as a deployment would mount them."""
    refs = {}
    for slot, value in values.items():
        path = tmp_path / slot
        path.write_text(value, encoding="utf-8")
        refs[slot] = f"file:{path.as_posix()}"
    resolver = SecretResolver({"file": FileBackend()}, ttl_seconds=3600)
    return ConnectionSecrets(resolver, refs), resolver


@pytest.fixture
def no_wait(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("src.l1_agent.adapters.splunk_adapter.asyncio.sleep", instant)


class TestSplunk:
    def build(self, url, tmp_path, token="sp-one"):
        secrets, _ = secrets_in(tmp_path, token=token)
        return SplunkConnector({"base_url": url}, secrets, demo_mode=True)

    async def test_search_creates_a_job_waits_for_it_and_reads_the_results(self, tmp_path, no_wait):
        async with Replay.from_file("splunk/search_ok.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "splunk.search",
                {
                    "query": "index=app_logs ERROR",
                    "time_range": {"earliest": "-1h", "latest": "now"},
                },
                CTX,
            )
            server.assert_complete()
        assert result.success and result.data["result_count"] == 3
        assert "PaymentService timeout" in result.evidence_snippet

    async def test_no_results_is_a_successful_search(self, tmp_path, no_wait):
        async with Replay.from_file("splunk/search_no_results.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "splunk.search", {"query": "index=app_logs nothing"}, CTX
            )
            server.assert_complete()
        assert result.success and result.data["result_count"] == 0

    async def test_a_rejected_token_is_a_failed_check(self, tmp_path):
        async with Replay.from_file("splunk/unauthorized.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "splunk.search", {"query": "index=app_logs"}, CTX
            )
            server.assert_complete()  # one request: the credential had not changed
        assert not result.success and "401" in result.error
        assert "sp-one" not in result.error

    async def test_a_search_splunk_rejects_is_a_failed_check(self, tmp_path):
        async with Replay.from_file("splunk/bad_search.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "splunk.search", {"query": "| foo"}, CTX
            )
        assert not result.success and "400" in result.error

    async def test_a_rotated_token_is_picked_up_without_a_restart(self, tmp_path, no_wait):
        """Phase 3 exit criterion 5, for a ported adapter."""
        secrets, resolver = secrets_in(tmp_path, token="sp-one")
        async with Replay.from_file("splunk/token_rotated.json") as server:
            splunk = SplunkConnector({"base_url": server.url}, secrets, demo_mode=True)
            await splunk.secret("token")  # the connector has been running with sp-one
            (tmp_path / "token").write_text("sp-two", encoding="utf-8")  # rotated in the backend
            result = await splunk.invoke("splunk.search", {"query": "index=app_logs ERROR"}, CTX)
            server.assert_complete()
        assert result.success and result.data["result_count"] == 1


class TestDynatrace:
    def build(self, url, tmp_path):
        secrets, _ = secrets_in(tmp_path, api_token="dt-one")
        return DynatraceConnector({"base_url": url}, secrets, demo_mode=True)

    async def test_a_host_with_no_open_problem_is_healthy(self, tmp_path):
        async with Replay.from_file("dynatrace/host_healthy.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "dynatrace.host.health", {"action": "vm_health", "host_name": "app-server-01"}, CTX
            )
            server.assert_complete()
        assert result.success and result.data["healthy"] is True
        assert result.data["hosts_found"] == 1

    async def test_hosts_on_later_pages_are_checked_too(self, tmp_path):
        # 22 hosts over two pages. The only open problem is on host 21: reading
        # one page, or asking about the first 20 hosts, would call the group healthy.
        async with Replay.from_file("dynatrace/host_group_paged.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "dynatrace.host.health", {"action": "vm_health", "host_group": "payments"}, CTX
            )
            server.assert_complete()
        assert result.success
        assert result.data["hosts_found"] == 22
        assert result.data["healthy"] is False and result.data["problems_count"] == 1

    async def test_an_api_error_is_a_failed_check_never_a_healthy_host(self, tmp_path):
        async with Replay.from_file("dynatrace/problems_unavailable.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "dynatrace.host.health", {"host_name": "app-server-01"}, CTX
            )
        assert not result.success and "500" in result.error
        assert "healthy" not in result.data

    async def test_metrics_return_the_latest_value(self, tmp_path):
        async with Replay.from_file("dynatrace/metrics.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "dynatrace.metrics.query",
                {
                    "action": "metrics",
                    "metric_selector": "builtin:host.mem.usage",
                    "entity_selector": 'type("HOST"),entityName("app-server-01")',
                },
                CTX,
            )
            server.assert_complete()
        assert result.success and result.data["latest"] == {"builtin:host.mem.usage": 92.5}

    async def test_a_token_without_the_scope_is_a_failed_check(self, tmp_path):
        async with Replay.from_file("dynatrace/token_missing_scope.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "dynatrace.metrics.query", {"action": "metrics"}, CTX
            )
        assert not result.success and "403" in result.error

    async def test_a_host_name_cannot_break_out_of_the_selector(self, tmp_path):
        connector = self.build("http://127.0.0.1:9", tmp_path)
        result = await connector.invoke(
            "dynatrace.host.health", {"host_name": 'x"),type("SERVICE'}, CTX
        )
        assert not result.success and "quotes" in result.error


class TestIbmMq:
    def build(self, url, tmp_path):
        secrets, _ = secrets_in(tmp_path, api_key="ir-one")
        return IbmMqConnector({"base_url": url}, secrets, demo_mode=True)

    async def check(self, fixture, tmp_path, **parameters):
        async with Replay.from_file(f"ibm_mq/{fixture}.json") as server:
            result = await self.build(server.url, tmp_path).invoke(
                "ibm_mq.queue.check", {"queue_manager": "QMPROD01", **parameters}, CTX
            )
            server.assert_complete()
        return result

    async def test_queue_depth(self, tmp_path):
        result = await self.check("depth", tmp_path, queue="ORDER.REQUEST", action="depth")
        assert result.success and result.data["current_depth"] == 12

    async def test_browse_asks_for_a_few_messages_only(self, tmp_path):
        result = await self.check("browse", tmp_path, queue="ORDER.REQUEST", action="browse")
        assert result.success and len(result.data["messages"]) == 1

    async def test_queue_manager_status_without_a_queue(self, tmp_path):
        result = await self.check("manager_status", tmp_path, action="status")
        assert result.success and result.data["status"] == "RUNNING"

    async def test_an_unknown_queue_is_a_failed_check(self, tmp_path):
        result = await self.check("unknown_queue", tmp_path, queue="ORDER.REQUEST", action="depth")
        assert not result.success and "404" in result.error

    async def test_a_rejected_key_is_a_failed_check(self, tmp_path):
        result = await self.check("unauthorized", tmp_path, queue="ORDER.REQUEST", action="depth")
        assert not result.success and "401" in result.error and "ir-one" not in result.error

    async def test_a_name_that_would_change_the_request_path_is_refused(self, tmp_path):
        connector = self.build("http://127.0.0.1:9", tmp_path)
        result = await connector.invoke(
            "ibm_mq.queue.check", {"queue_manager": "QM1/../../admin", "action": "status"}, CTX
        )
        assert not result.success and "Invalid" in result.error


class FakeProcess:
    def __init__(self, stdout: str, returncode: int, stderr: str = "") -> None:
        self._out, self._err, self.returncode = stdout, stderr, returncode

    async def communicate(self):
        return self._out.encode(), self._err.encode()


class TestAutosys:
    @pytest.fixture
    def commands(self, monkeypatch) -> List[tuple]:
        """Record every command line and answer it from a recorded output."""
        seen: List[tuple] = []
        self.answer = FakeProcess("", 0)

        async def fake_exec(*argv, **_kwargs):
            seen.append(argv)
            return self.answer

        monkeypatch.setattr(
            "src.l1_agent.adapters.autosys_adapter.asyncio.create_subprocess_exec", fake_exec
        )
        return seen

    async def test_job_status_reads_the_status_column_of_a_box(self, commands):
        self.answer = FakeProcess(load("autosys/autorep_box.txt"), 0)
        result = await AutosysConnector({}).invoke(
            "autosys.job.status", {"job_name": "BATCH_PAYMENT_PROCESS"}, CTX
        )
        assert commands == [("autorep", "-J", "BATCH_PAYMENT_PROCESS")]
        assert result.success
        # autorep prints Run/Ntry and Pri/Xit after the status; the status is not the last field.
        assert [(j["job_name"], j["status"]) for j in result.data["jobs"]] == [
            ("BATCH_PAYMENT_PROCESS", "FA"),
            ("BATCH_PAYMENT_EXTRACT", "SU"),
            ("BATCH_PAYMENT_LOAD", "FA"),
        ]

    async def test_dependencies_use_job_depends(self, commands):
        self.answer = FakeProcess("", 0)
        await AutosysConnector({}).invoke(
            "autosys.job.status", {"job_name": "BATCH_X", "query_type": "dependencies"}, CTX
        )
        assert commands == [("job_depends", "-J", "BATCH_X")]

    async def test_an_unknown_job_is_a_failed_check(self, commands):
        self.answer = FakeProcess("", 1, load("autosys/autorep_unknown_job.txt"))
        result = await AutosysConnector({}).invoke(
            "autosys.job.status", {"job_name": "NO_SUCH_JOB"}, CTX
        )
        assert not result.success and "CAUAJM_E_50033" in result.error

    async def test_a_job_name_with_shell_characters_never_reaches_a_command(self, commands):
        result = await AutosysConnector({}).invoke(
            "autosys.job.status", {"job_name": "X; sendevent -E FORCE_STARTJOB -J X"}, CTX
        )
        assert not result.success and commands == []

    # ── The force start (ADR 0008). The reads above stay in the read-only adapter. ──

    def writer(self, **config):
        """A connector that will accept a grant minted by the returned function."""
        from src.l1_agent.actions.grants import GrantAuthority
        from src.l1_agent.connectors.sdk.grant import call_digest

        connector = AutosysConnector({"force_start_jobs": ["NIGHTLY_*"], **config})
        authority = GrantAuthority()

        class Verifier:
            async def redeem(self, grant, **call):
                authority.redeem(grant, **call)

        connector.grant_verifier = Verifier()

        def grant(job: str):
            return authority.mint(
                decision_id="d-1",
                tenant_id="None",
                run_id="run-1",
                step_id="step-1",
                capability_id="autosys.job.force_start",
                call_digest=call_digest("autosys.job.force_start", {"job_name": job}),
            )

        return connector, grant

    @pytest.fixture
    def sendevent(self, monkeypatch) -> List[tuple]:
        seen: List[tuple] = []
        self.answer = FakeProcess("", 0)

        async def fake_exec(*argv, **kwargs):
            seen.append((argv, kwargs))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        monkeypatch.setattr(
            "src.l1_agent.connectors.ops.autosys.asyncio.create_subprocess_exec", fake_exec
        )
        return seen

    async def test_force_start_is_an_argument_list_from_configuration(self, sendevent):
        connector, grant = self.writer(sendevent_path="/opt/CA/bin/sendevent")
        job = "NIGHTLY_SETTLEMENT_LOAD"
        result = await connector.invoke(
            "autosys.job.force_start", {"job_name": job}, CTX, grant(job)
        )
        assert result.success and result.data == {"job_name": job, "event": "FORCE_STARTJOB"}
        argv, kwargs = sendevent[0]
        assert argv == ("/opt/CA/bin/sendevent", "-E", "FORCE_STARTJOB", "-J", job)
        assert "shell" not in kwargs and len(sendevent) == 1

    async def test_a_refused_event_is_a_failed_write_with_the_schedulers_message(self, sendevent):
        self.answer = FakeProcess("", 1, load("autosys/sendevent_refused.txt"))
        connector, grant = self.writer()
        result = await connector.invoke(
            "autosys.job.force_start", {"job_name": "NIGHTLY_X"}, CTX, grant("NIGHTLY_X")
        )
        assert not result.success and "CAUAJM_E_10281" in result.error
        assert len(sendevent) == 1, "a write is not retried"

    async def test_a_missing_executable_is_a_failed_write(self, sendevent):
        self.answer = FileNotFoundError("sendevent")
        connector, grant = self.writer()
        result = await connector.invoke(
            "autosys.job.force_start", {"job_name": "NIGHTLY_X"}, CTX, grant("NIGHTLY_X")
        )
        assert not result.success and "could not be run" in result.error

    async def test_a_job_off_the_allow_list_never_reaches_a_command(self, sendevent):
        connector, grant = self.writer()
        for job in ("PAYROLL_POST", "nightly_x", "X; rm -rf /", "NIGHTLY_X -E KILLJOB"):
            with pytest.raises(Refused):
                await connector.invoke(
                    "autosys.job.force_start", {"job_name": job}, CTX, grant(job)
                )
        assert sendevent == []

    async def test_a_listed_name_with_unsafe_characters_is_still_not_run(self, sendevent):
        """The allow list is the tenant's; the character check is the connector's own."""
        connector, grant = self.writer(force_start_jobs=["*"])
        capability = connector.manifest.capability("autosys.job.force_start")
        for job in ("X; rm -rf /", "$(reboot)", "A B", "--help -J X", "NIGHTLY_X\n"):
            # Refused by the capability's schema, before the grant is used...
            with pytest.raises(Refused, match="characters that are not allowed"):
                await connector.invoke(
                    "autosys.job.force_start", {"job_name": job}, CTX, grant(job)
                )
            # ...and by the connector itself, were the schema ever loosened.
            result = await connector._execute_write(capability, {"job_name": job})
            assert not result.success and result.error == "Invalid job name"
        assert sendevent == []

    async def test_no_grant_no_command(self, sendevent):
        from src.l1_agent.connectors.sdk.base import WriteNotAuthorized

        connector, grant = self.writer()
        for given in (None, object(), grant("NIGHTLY_OTHER")):
            with pytest.raises(WriteNotAuthorized):
                await connector.invoke(
                    "autosys.job.force_start", {"job_name": "NIGHTLY_X"}, CTX, given
                )
        assert sendevent == []

    def test_the_only_commands_the_adapter_runs_are_reads(self):
        from src.l1_agent.adapters.autosys_adapter import SAFE_COMMANDS

        assert SAFE_COMMANDS == {"autorep", "job_depends"}
        assert not hasattr(AutosysAdapter, "force_start")


class TestFileShare:
    PREFIX = "//fileserver/logs"

    @pytest.fixture
    def share(self, monkeypatch):
        """Serve //fileserver/logs/* from the fixture directory."""

        def to_local(unc: str) -> str:
            return str(FIXTURES / "fileshare" / unc.rsplit("/", 1)[-1])

        monkeypatch.setattr(WindowsShareAdapter, "_unc_to_os_path", staticmethod(to_local))
        return FileShareConnector({"allowed_unc_prefixes": [self.PREFIX]})

    async def test_tail_filtered_by_a_literal_pattern(self, share):
        result = await share.invoke(
            "fileshare.file.read",
            {"unc_path": f"{self.PREFIX}/app.log", "pattern": "ERROR", "last_n_lines": 10},
            CTX,
        )
        assert result.success and result.data["line_count"] == 2
        assert "timeout calling ledger" in result.raw_output and "INFO" not in result.raw_output

    async def test_a_missing_file_is_a_failed_check(self, share):
        result = await share.invoke(
            "fileshare.file.read", {"unc_path": f"{self.PREFIX}/absent.log"}, CTX
        )
        assert not result.success and "not found" in result.error.lower()

    async def test_a_path_outside_the_allowed_prefix_is_refused(self, share):
        for path in ("//fileserver/logs-private/app.log", "//fileserver/logs/../secrets/app.log"):
            result = await share.invoke("fileshare.file.read", {"unc_path": path}, CTX)
            assert not result.success and "not in the allowed" in result.error, path

    async def test_an_empty_allow_list_refuses_before_the_adapter_runs(self):
        with pytest.raises(Refused, match="allow list 'allowed_unc_prefixes' is empty"):
            await FileShareConnector({}).invoke(
                "fileshare.file.read", {"unc_path": f"{self.PREFIX}/app.log"}, CTX
            )


class FakeDriver:
    def __init__(self, page_source: str, title: str) -> None:
        self.page_source, self.title = page_source, title
        self.visited: List[str] = []
        self.closed = False

    def set_page_load_timeout(self, _seconds) -> None:
        return None

    def get(self, url: str) -> None:
        self.visited.append(url)

    def quit(self) -> None:
        self.closed = True


class TestBrowser:
    def build(self, monkeypatch, page: str, title: str):
        driver = FakeDriver(load(f"browser/{page}"), title)
        monkeypatch.setattr(WebUIScraperAdapter, "_create_driver", lambda self: driver)
        return BrowserConnector({"allowed_domains": ["console.example"]}), driver

    async def test_a_page_with_the_expected_text_is_healthy(self, monkeypatch):
        browser, driver = self.build(monkeypatch, "healthy.html", "Payments console")
        result = await browser.invoke(
            "browser.page.check",
            {"url": "https://console.example/status", "expected_text": "all systems operational"},
            CTX,
        )
        assert result.success and result.data["status"] == "healthy"
        assert driver.visited == ["https://console.example/status"] and driver.closed

    async def test_an_error_page_is_a_failed_check(self, monkeypatch):
        browser, driver = self.build(monkeypatch, "bad_gateway.html", "502 Bad Gateway")
        result = await browser.invoke(
            "browser.page.check", {"url": "https://console.example/status"}, CTX
        )
        assert not result.success and "HTTP 502" in result.data["error_indicators"]
        assert driver.closed

    async def test_other_hosts_and_plain_http_are_never_visited(self, monkeypatch):
        browser, driver = self.build(monkeypatch, "healthy.html", "x")
        for url in (
            "https://evil.example/",
            "http://console.example/status",
            "https://console.example.evil.example/",
            "https://user:pw@console.example/",
        ):
            result = await browser.invoke("browser.page.check", {"url": url}, CTX)
            assert not result.success, url
        assert driver.visited == []

    async def test_an_empty_allow_list_refuses_before_the_adapter_runs(self):
        with pytest.raises(Refused):
            await BrowserConnector({}).invoke(
                "browser.page.check", {"url": "https://console.example/"}, CTX
            )


class TestMainframe:
    def build(self, monkeypatch, screen):
        navigations = []

        def recorded(self, navigation):
            navigations.append(navigation)
            return screen

        monkeypatch.setattr(MainframeAdapter, "_connect_navigate_disconnect", recorded)
        connector = MainframeConnector(
            {
                "host": "mf.example",
                "allowed_transactions": ["BEIM"],
                "default_navigation": [{"input": "BEIM", "key": "Enter"}],
            }
        )
        return connector, navigations

    async def test_job_status_is_read_from_the_screen(self, monkeypatch):
        mainframe, navigations = self.build(monkeypatch, load("mainframe/beim_status.txt"))
        result = await mainframe.invoke(
            "mainframe.screen.check", {"action": "async_status", "job_name": "PAYASYNC"}, CTX
        )
        assert result.success and result.data["all_match_expected"] is True
        assert result.data["jobs"][0]["status"] == "inact ok"
        # The keystrokes are the connection's own; the step supplied none.
        assert navigations == [[{"input": "BEIM", "key": "Enter"}]]

    async def test_a_job_in_error_does_not_match(self, monkeypatch):
        mainframe, _ = self.build(monkeypatch, load("mainframe/beim_status.txt"))
        result = await mainframe.invoke(
            "mainframe.screen.check", {"action": "async_status", "job_name": "LEDGASYN"}, CTX
        )
        assert result.success and result.data["all_match_expected"] is False

    async def test_a_failed_connection_is_a_failed_check(self, monkeypatch):
        mainframe, _ = self.build(monkeypatch, None)
        result = await mainframe.invoke(
            "mainframe.screen.check", {"action": "screen_check", "expected_text": "BEIM"}, CTX
        )
        assert not result.success and "Failed to connect" in result.error

    async def test_a_transaction_off_the_allow_list_is_refused(self, monkeypatch):
        mainframe, navigations = self.build(monkeypatch, load("mainframe/beim_status.txt"))
        result = await mainframe.invoke(
            "mainframe.screen.check", {"action": "async_status", "transaction": "CEMT"}, CTX
        )
        assert not result.success and navigations == []

    async def test_keystrokes_cannot_be_passed_as_parameters(self, monkeypatch):
        mainframe, navigations = self.build(monkeypatch, load("mainframe/beim_status.txt"))
        with pytest.raises(Refused, match="screen_navigation"):
            await mainframe.invoke(
                "mainframe.screen.check",
                {"action": "screen_check", "screen_navigation": [{"input": "CEMT SET"}]},
                CTX,
            )
        assert navigations == []
