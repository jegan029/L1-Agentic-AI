"""The Splunk connector: its settings, its two backends, and demo mode with Splunk live."""

from __future__ import annotations

import logging

import pytest

from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.adapters.mock_adapters import MockSplunkAdapter
from src.l1_agent.adapters.splunk.mcp_backend import McpBackend
from src.l1_agent.adapters.splunk.rest_backend import RestBackend
from src.l1_agent.config.settings import (
    AgentSettings,
    ConfigError,
    SecretsSettings,
    Settings,
    SplunkSettings,
)
from src.l1_agent.connectors.catalog import connectors_for_adapters
from src.l1_agent.connectors.hub import (
    check_live_connectors,
    demo_connectors,
    environment_connections,
)
from src.l1_agent.connectors.ops.splunk import SplunkConnector
from src.l1_agent.connectors.sdk.base import Health, HealthStatus, Refused, StepContext
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, EnvBackend, SecretResolver
from src.l1_agent.demo import build_mock_adapters
from src.l1_agent.runner import protocol
from src.l1_agent.utils.secrets import _SECRET_MAPPINGS

CTX = StepContext(None, "run-1", "step-1")
MCP_URL = "https://splunk.example:8089/services/mcp"
SPLUNK_ENV = [
    "SPLUNK_BACKEND", "SPLUNK_MCP_URL", "SPLUNK_MCP_SEARCH_TOOL", "SPLUNK_BASE_URL",
    "SPLUNK_TOKEN", "SPLUNK_MCP_AUTH", "SPLUNK_CA_BUNDLE", "SPLUNK_VERIFY_SSL",
    "SPLUNK_ALLOWED_INDEXES", "SPLUNK_MAX_LOOKBACK", "SPLUNK_MAX_RESULTS",
    "SPLUNK_TIMEOUT_SECONDS", "SPLUNK_SEARCH_TIMEOUT", "LIVE_ADAPTERS", "AGENT_DEMO_MODE",
]  # fmt: skip


def secrets(**values: str) -> ConnectionSecrets:
    class Fixed:
        def read(self, locator: str) -> str:
            return values[locator]

    return ConnectionSecrets(
        SecretResolver({"env": Fixed()}, ttl_seconds=3600), {slot: f"env:{slot}" for slot in values}
    )


@pytest.fixture
def env(monkeypatch):
    """A clean environment for Settings.from_env: no .env file and no Splunk variables."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for name in SPLUNK_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestConnectionSettings:
    def test_the_backend_follows_the_settings(self):
        backend = SplunkConnector._backend_of
        assert backend({"base_url": "https://s:8089"}) == "rest"
        assert backend({"mcp_url": MCP_URL}) == "mcp"
        assert backend({"mcp_url": MCP_URL, "base_url": "https://s:8089"}) == "rest"
        assert (
            backend({"backend": "mcp", "mcp_url": MCP_URL, "base_url": "https://s:8089"}) == "mcp"
        )

    @pytest.mark.parametrize(
        "config, message",
        [
            ({}, "base_url"),
            ({"backend": "mcp"}, "mcp_url"),
            ({"backend": "mcp", "base_url": "https://s:8089"}, "mcp_url"),
            ({"backend": "rest", "mcp_url": MCP_URL}, "base_url"),
            ({"backend": "carrier-pigeon", "base_url": "https://s"}, "must be one of"),
            ({"base_url": "https://s", "allowed_indexes": ["app_*"]}, "without wildcards"),
            ({"base_url": "https://s", "allowed_indexes": ["*"]}, "without wildcards"),
            ({"base_url": "https://s", "allowed_indexes": [""]}, "index names"),
            ({"base_url": "https://s", "allowed_indexes": [5]}, "index names"),
            ({"base_url": "https://s", "max_lookback": "forever"}, "max_lookback"),
            ({"base_url": "https://s", "max_results": 0}, "below the minimum"),
            ({"base_url": "https://s", "max_results": 100000}, "above the maximum"),
            ({"base_url": "https://s", "mcp_search_tool": "a tool; rm"}, "not allowed"),
            ({"base_url": "https://s", "token": "abc"}, "not allowed"),
        ],
    )
    def test_a_connection_that_cannot_work_is_refused_when_it_is_read(self, config, message):
        with pytest.raises(ConfigError, match=message):
            SplunkConnector(config)

    def test_tls_verification_is_only_switched_off_in_demo_mode(self):
        config = {"base_url": "https://s:8089", "verify_ssl": False}
        with pytest.raises(ConfigError, match="only allowed in demo mode"):
            SplunkConnector(config)
        SplunkConnector(config, demo_mode=True)

    async def test_each_backend_is_built_with_its_own_credential(self):
        rest = SplunkConnector({"base_url": "https://s:8089"}, secrets(token="rest-token"))
        mcp = SplunkConnector({"mcp_url": MCP_URL}, secrets(mcp_token="Bearer mcp-token"))
        built_rest, built_mcp = await rest._adapter(), await mcp._adapter()
        assert (
            isinstance(built_rest._backend, RestBackend)
            and built_rest._backend._token == "rest-token"
        )
        assert (
            isinstance(built_mcp._backend, McpBackend) and built_mcp._backend._token == "mcp-token"
        )
        assert built_mcp._backend._tool == "splunk_run_query"
        # Whichever backend, the step trace names the same tool.
        assert rest.tool_name == mcp.tool_name == "Splunk"
        await rest.close()
        await mcp.close()

    async def test_the_mcp_backend_without_its_token_is_a_failed_call_not_a_crash(self):
        connector = SplunkConnector({"mcp_url": MCP_URL, "allowed_indexes": ["app_logs"]})
        result = await connector.invoke("splunk.search", {"query": "index=app_logs"}, CTX)
        assert not result.success and "credential unavailable" in result.error
        health = await connector.health()
        assert health.status.value == "unavailable" and "mcp_token" in health.reason

    async def test_an_empty_allow_list_refuses_every_search_before_the_guard(self):
        connector = SplunkConnector({"mcp_url": MCP_URL}, secrets(mcp_token="t"))
        with pytest.raises(Refused, match="allow list 'allowed_indexes' is empty"):
            await connector.invoke("splunk.search", {"query": "index=app_logs"}, CTX)

    async def test_health_reports_why_a_backend_is_unusable(self):
        connector = SplunkConnector({"mcp_url": MCP_URL}, secrets(mcp_token="t"))
        adapter = await connector._adapter()

        async def missing_tool():
            return False, "the MCP server does not offer the search tool 'splunk_run_query'"

        adapter._backend.health = missing_tool
        health = await connector.health()
        assert health.status.value == "unavailable" and "splunk_run_query" in health.reason
        (check,) = await connector.test_connection()
        assert not check.ok and "splunk_run_query" in check.detail

    def test_the_manifest_declares_both_credentials_and_the_allow_list(self):
        manifest = SplunkConnector.manifest
        assert [s.name for s in manifest.secrets] == ["token", "mcp_token"]
        (search,) = manifest.capabilities
        assert search.kind.value == "read" and search.tier.value == "T0"
        assert [(b.parameter, b.config_key) for b in search.allow_lists] == [
            ("query", "allowed_indexes")
        ]


class TestEnvironment:
    def test_defaults(self, env):
        splunk = Settings.from_env().splunk
        assert (splunk.backend, splunk.mcp_search_tool) == ("", "splunk_run_query")
        assert (splunk.max_lookback, splunk.max_results, splunk.allowed_indexes) == ("24h", 100, ())
        assert splunk.verify_ssl is True and splunk.search_timeout_seconds == 120

    def test_every_setting_is_read(self, env):
        env.setenv("SPLUNK_BACKEND", "MCP")
        env.setenv("SPLUNK_MCP_URL", MCP_URL)
        env.setenv("SPLUNK_MCP_SEARCH_TOOL", "run_query")
        env.setenv("SPLUNK_CA_BUNDLE", "/etc/ssl/splunk-ca.pem")
        env.setenv("SPLUNK_ALLOWED_INDEXES", "batch_logs, app_logs")
        env.setenv("SPLUNK_MAX_LOOKBACK", "7d")
        env.setenv("SPLUNK_MAX_RESULTS", "50")
        env.setenv("SPLUNK_TIMEOUT_SECONDS", "45")
        env.setenv("SPLUNK_MCP_AUTH", "Bearer abc")
        settings = Settings.from_env()
        connection = environment_connections(settings)["splunk"]
        assert connection.config == {
            "backend": "mcp",
            "mcp_url": MCP_URL,
            "mcp_search_tool": "run_query",
            "ca_bundle": "/etc/ssl/splunk-ca.pem",
            "verify_ssl": True,
            "search_timeout_seconds": 45,
            "allowed_indexes": ["batch_logs", "app_logs"],
            "max_lookback": "7d",
            "max_results": 50,
        }
        # A reference to the variable, never its value.
        assert connection.secret_refs == {"mcp_token": "env:SPLUNK_MCP_AUTH"}
        assert "abc" not in repr(settings)
        SplunkConnector.check_config(connection.config)

    def test_the_older_timeout_name_still_works(self, env):
        env.setenv("SPLUNK_SEARCH_TIMEOUT", "30")
        assert Settings.from_env().splunk.search_timeout_seconds == 30

    def test_an_unknown_backend_stops_start_up(self, env):
        env.setenv("SPLUNK_BACKEND", "odbc")
        with pytest.raises(ConfigError, match="SPLUNK_BACKEND"):
            Settings.from_env()

    def test_tls_off_stops_start_up_outside_demo_mode(self, env):
        env.setenv("SPLUNK_VERIFY_SSL", "false")
        with pytest.raises(ConfigError, match="SPLUNK_VERIFY_SSL=false is only allowed"):
            Settings.from_env()
        env.setenv("AGENT_DEMO_MODE", "true")
        assert Settings.from_env().splunk.verify_ssl is False

    def test_the_mock_backend_makes_no_connection(self):
        settings = Settings(splunk=SplunkSettings(backend="mock", mcp_url=MCP_URL))
        assert "splunk" not in environment_connections(settings)

    def test_the_mcp_token_follows_the_secrets_backend(self, monkeypatch):
        monkeypatch.delenv("SPLUNK_MCP_AUTH", raising=False)
        splunk = SplunkSettings(mcp_url=MCP_URL)
        plain = environment_connections(Settings(splunk=splunk))["splunk"]
        vault = environment_connections(
            Settings(splunk=splunk, secrets=SecretsSettings(backend="vault"))
        )["splunk"]
        explicit = environment_connections(
            Settings(splunk=splunk, secret_refs={"SPLUNK_MCP_AUTH": "file:/run/secrets/mcp"})
        )["splunk"]
        assert plain.secret_refs == {}
        assert vault.secret_refs["mcp_token"] == "vault:splunk/mcp_auth"
        assert explicit.secret_refs == {"mcp_token": "file:/run/secrets/mcp"}
        assert {"env_key": "SPLUNK_MCP_AUTH", "vault_key": "splunk/mcp_auth"} in _SECRET_MAPPINGS


class TestDemoModeWithSplunkLive:
    def mocks(self):
        return connectors_for_adapters(build_mock_adapters())

    def settings(self, **splunk) -> Settings:
        return Settings(
            agent=AgentSettings(demo_mode=True, live_adapters=("splunk",)),
            splunk=SplunkSettings(**splunk),
        )

    def test_live_adapters_is_for_demo_mode_and_names_splunk_only(self, env):
        env.setenv("LIVE_ADAPTERS", "splunk")
        with pytest.raises(ConfigError, match="LIVE_ADAPTERS is for demo mode"):
            Settings.from_env()
        env.setenv("AGENT_DEMO_MODE", "true")
        assert Settings.from_env().agent.live_adapters == ("splunk",)
        env.setenv("LIVE_ADAPTERS", "splunk,autosys")
        with pytest.raises(ConfigError, match="LIVE_ADAPTERS may name: splunk"):
            Settings.from_env()

    def test_without_it_demo_mode_is_all_mocks(self):
        mocks = self.mocks()
        assert demo_connectors(Settings(agent=AgentSettings(demo_mode=True)), None, mocks) is mocks
        assert isinstance(mocks.connector("splunk")._fixed, MockSplunkAdapter)

    def test_only_splunk_is_swapped_for_the_real_connector(self):
        mocks = self.mocks()
        settings = self.settings(
            mcp_url=MCP_URL, allowed_indexes=("batch_logs", "app_logs"), verify_ssl=False
        )
        hybrid = demo_connectors(settings, SecretResolver({"env": EnvBackend()}), mocks)
        live = hybrid.connector("splunk")
        assert live._fixed is None and live.demo_mode is True
        assert live.config["allowed_indexes"] == ["batch_logs", "app_logs"]
        others = set(mocks.connectors()) - {"splunk"}
        assert others and all(hybrid.connector(c) is mocks.connector(c) for c in others)

    def test_a_live_splunk_that_is_not_configured_stops_start_up(self):
        # Falling back to the mock would show canned events as if they were real.
        for splunk in ({}, {"backend": "mock", "mcp_url": MCP_URL}):
            with pytest.raises(ConfigError, match="LIVE_ADAPTERS names 'splunk'"):
                demo_connectors(self.settings(**splunk), SecretResolver({}), self.mocks())

    def test_a_live_splunk_with_a_broken_setting_stops_start_up(self):
        with pytest.raises(ConfigError, match="max_lookback"):
            demo_connectors(
                self.settings(mcp_url=MCP_URL, max_lookback="soon"),
                SecretResolver({}),
                self.mocks(),
            )


class TestStartUpHealth:
    def hybrid(self):
        settings = Settings(
            agent=AgentSettings(demo_mode=True, live_adapters=("splunk",)),
            splunk=SplunkSettings(mcp_url=MCP_URL, allowed_indexes=("app_logs",)),
        )
        mocks = connectors_for_adapters(build_mock_adapters())
        return settings, demo_connectors(settings, SecretResolver({}), mocks)

    async def test_an_unusable_live_connector_is_reported_and_start_up_goes_on(self, caplog):
        settings, connectors = self.hybrid()  # no token was given
        with caplog.at_level(logging.ERROR, logger="l1_agent"):
            unhealthy = await check_live_connectors(settings, connectors)
        assert "mcp_token" in unhealthy["splunk"]
        assert any("Live connector splunk is UNHEALTHY" in r.getMessage() for r in caplog.records)

    async def test_a_missing_search_tool_is_reported_at_start_up(self):
        settings, connectors = self.hybrid()
        splunk = connectors.connector("splunk")

        async def health():
            return Health(HealthStatus.UNAVAILABLE, "the MCP server does not offer the search tool")

        splunk.health = health
        assert "search tool" in (await check_live_connectors(settings, connectors))["splunk"]

    async def test_a_healthy_one_reports_nothing(self):
        settings, connectors = self.hybrid()

        async def health():
            return Health(HealthStatus.HEALTHY)

        connectors.connector("splunk").health = health
        assert await check_live_connectors(settings, connectors) == {}


class TestOverTheRunnerProtocol:
    def test_a_refusal_and_its_audit_record_cross_the_wire(self):
        sent = AdapterResult(
            success=False,
            error="Search refused by the guard: index '_internal' is not on the allow list",
            escalate=True,
            audit={"backend": "mcp", "spl": "", "result_count": 0, "outcome": "rejected"},
        )
        back = protocol.result_from_wire(protocol.result_to_wire(sent))
        assert back == sent

    def test_the_flag_cannot_ride_on_a_success(self):
        back = protocol.result_from_wire({"success": True, "data": {}, "escalate": True})
        assert back.success and back.escalate is False

    def test_only_true_sets_the_flag(self):
        for value in ("true", 1, "yes", None, [True]):
            assert (
                protocol.result_from_wire({"success": False, "escalate": value}).escalate is False
            )

    def test_the_audit_record_is_bounded_to_short_scalars(self):
        junk = {
            "backend": "rest",
            "spl": "x" * 100_000,
            "nested": {"token": "secret"},
            "listed": ["secret"],
            5: "not a key",
            **{f"k{i}": i for i in range(100)},
        }
        back = protocol.result_from_wire({"success": True, "audit": junk})
        assert len(back.audit) <= protocol.MAX_AUDIT_KEYS
        assert len(back.audit["spl"]) == protocol.MAX_AUDIT_VALUE
        assert "nested" not in back.audit and "listed" not in back.audit
        assert protocol.result_from_wire({"success": True, "audit": "text"}).audit == {}
