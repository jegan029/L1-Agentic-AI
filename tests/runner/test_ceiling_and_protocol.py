"""The runner's own limits, and the shapes that travel between the two planes."""

from __future__ import annotations

import json
import uuid

import pytest

from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.config.settings import ConfigError, RunnerSettings, Settings
from src.l1_agent.connectors.catalog import default_registry
from src.l1_agent.connectors.sdk.base import CheckResult
from src.l1_agent.runner import protocol
from src.l1_agent.runner.ceiling import Ceiling, LocalConnection

REGISTRY = default_registry()
FORCE_START = REGISTRY.capability("autosys.job.force_start")
JOB_STATUS = REGISTRY.capability("autosys.job.status")
SPLUNK = REGISTRY.capability("splunk.search")
JOB = {"job_name": "NIGHTLY_LOAD"}
TARGETS = {"autosys": {"force_start_jobs": ("NIGHTLY_*",)}}


class TestCeiling:
    def test_by_default_a_runner_reads_and_writes_nothing(self):
        ceiling = Ceiling()
        assert ceiling.max_tier == "T0"
        assert ceiling.refuse(JOB_STATUS, "autosys", JOB, {}) is None
        assert "goes no higher than T0" in ceiling.refuse(FORCE_START, "autosys", JOB, {})

    def test_raising_the_tier_is_not_enough_for_a_write_without_a_local_target_list(self):
        refused = Ceiling(max_tier="T1").refuse(FORCE_START, "autosys", JOB, {})
        assert "no list of its own" in refused and "force_start_jobs" in refused
        # An empty list of its own allows nothing either.
        empty = Ceiling(max_tier="T1", targets={"autosys": {"force_start_jobs": ()}})
        assert "not on this runner's own list" in empty.refuse(FORCE_START, "autosys", JOB, {})

    def test_a_write_is_made_only_to_a_target_on_the_runners_own_list(self):
        ceiling = Ceiling(max_tier="T1", targets=TARGETS)
        assert ceiling.refuse(FORCE_START, "autosys", JOB, {}) is None
        refused = ceiling.refuse(FORCE_START, "autosys", {"job_name": "PAYROLL_POST"}, {})
        assert "not on this runner's own list" in refused

    def test_a_local_list_also_narrows_reads_and_its_absence_does_not(self):
        narrowed = Ceiling(targets={"autosys": {"force_start_jobs": ("NIGHTLY_*",)}})
        assert narrowed.refuse(JOB_STATUS, "autosys", {"job_name": "ANY_JOB"}, {}) is None
        fileshare = REGISTRY.capability("fileshare.file.read")
        binding = fileshare.allow_lists[0]
        only = Ceiling(targets={"fileshare": {binding.config_key: ("\\\\fs1\\ops\\*",)}})
        inside = {binding.parameter: "\\\\fs1\\ops\\run.log"}
        outside = {binding.parameter: "\\\\fs2\\hr\\pay.csv"}
        assert only.refuse(fileshare, "fileshare", inside, {}) is None
        assert "not on this runner's own list" in only.refuse(fileshare, "fileshare", outside, {})

    def test_the_capability_list_is_a_closed_list(self):
        ceiling = Ceiling(capabilities=("autosys.*",))
        assert ceiling.refuse(JOB_STATUS, "autosys", JOB, {}) is None
        assert "not a capability this runner carries out" in ceiling.refuse(
            SPLUNK, "splunk", {"query": "x"}, {}
        )

    def test_the_kill_switch_file_stops_writes_and_not_reads(self, tmp_path):
        switch = tmp_path / "stop"
        ceiling = Ceiling(max_tier="T2", targets=TARGETS, kill_switch_file=str(switch))
        assert ceiling.refuse(FORCE_START, "autosys", JOB, {}) is None
        switch.write_text("")
        assert ceiling.kill_switch_on() and ceiling.describe()["kill_switch"] is True
        assert ceiling.refuse(FORCE_START, "autosys", JOB, {}) == "the runner's kill switch is on"
        assert ceiling.refuse(JOB_STATUS, "autosys", JOB, {}) is None

    def test_a_credential_reference_must_point_where_the_runner_reads_credentials(self):
        ceiling = Ceiling(secret_ref_prefixes=("file:/run/secrets/connectors/", "vault:l1/"))
        ok = {"token": "file:/run/secrets/connectors/splunk-token"}
        assert ceiling.refuse(SPLUNK, "splunk", {"query": "x"}, ok) is None
        assert (
            ceiling.refuse(SPLUNK, "splunk", {"query": "x"}, {"token": "vault:l1/splunk/token"})
            is None
        )
        for reference in (
            "file:/etc/shadow",
            "env:RUNNER_PRIVATE_KEY",
            "file:/run/secrets/connectors/../runner/key",
            "vault:other/splunk",
        ):
            refused = ceiling.refuse(SPLUNK, "splunk", {"query": "x"}, {"token": reference})
            assert "RUNNER_SECRET_REF_PREFIXES" in refused, reference
            assert reference not in refused, "the reference itself is not echoed back"
        # With no prefixes at all, no reference from the control plane is followed.
        assert "RUNNER_SECRET_REF_PREFIXES" in Ceiling().refuse_test("splunk", ok)

    def test_a_connection_held_by_the_runner_replaces_the_control_planes(self):
        local = LocalConnection({"base_url": "https://splunk.internal"}, {"token": "env:X"})
        ceiling = Ceiling(connections={"splunk": local})
        # Whatever the control plane sent is not used, so it is not what gets checked.
        assert (
            ceiling.refuse(SPLUNK, "splunk", {"query": "x"}, {"token": "file:/etc/shadow"}) is None
        )
        assert ceiling.describe()["local_connections"] == ["splunk"]

    def test_it_is_read_from_files_only_the_operator_can_write(self, tmp_path):
        targets = tmp_path / "targets.json"
        targets.write_text(json.dumps({"autosys": {"force_start_jobs": ["NIGHTLY_*"]}}))
        connections = tmp_path / "connections.json"
        connections.write_text(
            json.dumps({"autosys": {"config": {"cli_path": "/opt/autorep"}, "secret_refs": {}}})
        )
        ceiling = Ceiling.from_settings(
            RunnerSettings(
                max_tier="T1",
                capabilities=("autosys.*",),
                targets_file=str(targets),
                connections_file=str(connections),
                secret_ref_prefixes=("file:/run/secrets/",),
            )
        )
        assert ceiling.refuse(FORCE_START, "autosys", JOB, {}) is None
        assert ceiling.connections["autosys"].config == {"cli_path": "/opt/autorep"}
        assert ceiling.describe()["local_targets"] == {"autosys": ["force_start_jobs"]}

    @pytest.mark.parametrize(
        "content", ["not json", "[]", '{"autosys": []}'], ids=["garbage", "list", "wrong shape"]
    )
    def test_an_unreadable_file_stops_the_runner_starting(self, tmp_path, content):
        path = tmp_path / "targets.json"
        path.write_text(content)
        with pytest.raises(ConfigError, match="RUNNER_TARGETS_FILE"):
            Ceiling.from_settings(RunnerSettings(targets_file=str(path)))
        with pytest.raises(ConfigError, match="RUNNER_TARGETS_FILE"):
            Ceiling.from_settings(RunnerSettings(targets_file=str(tmp_path / "missing.json")))

    def test_settings_refuse_an_unknown_tier_mode_or_mocks_outside_demo(self, monkeypatch):
        for name, value, why in (
            ("RUNNER_MAX_TIER", "T3", "RUNNER_MAX_TIER"),
            ("RUNNER_MODE", "sideways", "RUNNER_MODE"),
            ("RUNNER_MOCK_CONNECTORS", "true", "AGENT_DEMO_MODE"),
            ("RUNNER_VERIFY_SSL", "false", "AGENT_DEMO_MODE"),
        ):
            monkeypatch.setenv("AGENT_DEMO_MODE", "false")
            monkeypatch.setenv(name, value)
            with pytest.raises(ConfigError, match=why):
                Settings.from_env()
            monkeypatch.delenv(name)
        monkeypatch.setenv("RUNNER_MAX_TIER", "t1")
        monkeypatch.setenv("RUNNER_SECRET_REF_PREFIXES", "file:/a/, vault:b/")
        made = Settings.from_env().runner
        assert made.max_tier == "T1" and made.mode == "embedded"
        assert made.secret_ref_prefixes == ("file:/a/", "vault:b/")


class TestProtocol:
    def test_an_identity_names_the_tenant_and_the_runner(self):
        tenant, runner = uuid.uuid4(), uuid.uuid4()
        assert protocol.parse_identity(protocol.identity(tenant, runner)) == (tenant, runner)
        for text in (
            "",
            "abc",
            f"{tenant.hex}",
            f"{tenant.hex}.zz",
            f"{tenant}.{runner}",
            "x" * 65,
        ):
            assert protocol.parse_identity(text) is None

    def test_a_request_signature_covers_method_path_time_and_body(self):
        base = protocol.request_message("POST", "/runner/v1/claim", "100", b"{}")
        assert base == protocol.request_message("post", "/runner/v1/claim", "100", b"{}")
        for other in (
            protocol.request_message("GET", "/runner/v1/claim", "100", b"{}"),
            protocol.request_message("POST", "/runner/v1/calls/x/result", "100", b"{}"),
            protocol.request_message("POST", "/runner/v1/claim", "101", b"{}"),
            protocol.request_message("POST", "/runner/v1/claim", "100", b"{ }"),
        ):
            assert other != base

    def test_an_envelope_is_one_text_whatever_order_it_was_built_in(self):
        values = dict(
            call_id=uuid.UUID(int=1),
            tenant_id=uuid.UUID(int=2),
            kind=protocol.KIND_INVOKE,
            connector_id="autosys",
            capability_id="autosys.job.status",
            run_id="run-1",
            step_id="step-1",
            purpose="run",
            secret_refs={},
            grant=None,
            issued_at=1.0,
            expires_at=2.0,
        )
        one = protocol.envelope(parameters={"a": 1, "b": 2}, config={"x": 1, "y": 2}, **values)
        two = protocol.envelope(parameters={"b": 2, "a": 1}, config={"y": 2, "x": 1}, **values)
        assert one == two and one.isascii()
        body = json.loads(one)
        assert body["v"] == 1 and body["connection"] == {
            "config": {"x": 1, "y": 2},
            "secret_refs": {},
        }
        with pytest.raises(ValueError):
            protocol.envelope(parameters={}, config={}, **{**values, "kind": "shell"})

    def test_a_result_is_bounded_on_the_way_out_and_on_the_way_in(self):
        big = AdapterResult(
            success=True,
            data={"status": "SU"},
            raw_output="r" * (protocol.MAX_RAW_OUTPUT + 500),
            evidence_snippet="e" * (protocol.MAX_EVIDENCE + 500),
            error="x" * 5000,
        )
        wire = protocol.result_to_wire(big)
        assert len(wire["raw_output"]) == protocol.MAX_RAW_OUTPUT
        assert len(wire["evidence_snippet"]) == protocol.MAX_EVIDENCE
        assert len(wire["error"]) == protocol.MAX_ERROR
        assert len(protocol.canonical(wire)) < 256 * 1024, "it fits in one request"
        back = protocol.result_from_wire(wire)
        assert back.success and back.data == {"status": "SU"}
        with pytest.raises(ValueError, match="too large"):
            protocol.result_to_wire(
                AdapterResult(success=True, data={"k": "v" * protocol.MAX_DATA})
            )

    @pytest.mark.parametrize(
        "junk", [None, "ok", [], {}, {"success": "yes"}, {"success": 1}, {"data": {"status": "SU"}}]
    )
    def test_anything_that_is_not_a_result_is_a_failure_never_a_success(self, junk):
        result = protocol.result_from_wire(junk)
        assert result.success is False and result.error

    def test_data_that_is_not_an_object_is_dropped(self):
        result = protocol.result_from_wire({"success": True, "data": ["SU"], "raw_output": 5})
        assert result.data == {} and result.raw_output == "5"

    def test_checks_are_bounded(self):
        wire = protocol.checks_to_wire([CheckResult("n" * 500, True, "d" * 900)])
        assert len(wire[0]["name"]) == 200 and len(wire[0]["detail"]) == 500
