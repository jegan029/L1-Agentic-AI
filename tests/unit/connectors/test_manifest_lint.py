"""Manifest lint and the registry (Phase 3 exit criterion 1).

A connector whose write capability lacks a risk tier, a verify operation, a
closed parameter schema, required scopes or an allow list for its target is
not registered, so it cannot ship.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.connectors.catalog import (
    FIRST_PARTY,
    STEP_CAPABILITIES,
    connectors_for_adapters,
    default_registry,
)
from src.l1_agent.connectors.sdk.base import (
    Connector,
    Health,
    HealthStatus,
    Refused,
    StepContext,
    WriteNotAuthorized,
)
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    PreCheck,
    RiskTier,
    Verify,
    lint,
)
from src.l1_agent.connectors.sdk.registry import ManifestError, Registry
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.demo import build_mock_adapters
from src.l1_agent.models.sop import CHECK_STEP_TYPES

CTX = StepContext(None, "run-1", "step-1")

STATUS = Capability(
    id="demo.job.status",
    kind=Kind.READ,
    tier=RiskTier.T0,
    summary="Read a job's status",
    parameters=closed({"job_name": {"type": "string"}}, required=["job_name"]),
)
# A write that declares everything a write must.
FORCE_START = Capability(
    id="demo.job.force_start",
    kind=Kind.WRITE,
    tier=RiskTier.T2,
    summary="Start a job now",
    parameters=closed({"job_name": {"type": "string"}}, required=["job_name"]),
    scopes=("sendevent",),
    allow_lists=(AllowListBinding("job_name", "allowed_jobs", "exact"),),
    target_parameters=("job_name",),
    idempotent=False,
    pre_checks=(PreCheck("demo.job.status", "result.status == 'FA'"),),
    verify=Verify("demo.job.status", "result.status == 'RU'"),
    rollback="none: a started job cannot be un-started",
)


def manifest(*capabilities: Capability) -> Manifest:
    return Manifest(
        id="demo",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="Demo",
        config=closed({"allowed_jobs": {"type": "array"}}),
        capabilities=capabilities,
    )


def problems(capability: Capability) -> list:
    return lint(manifest(STATUS, capability), frozenset({STATUS.id, capability.id}))


class TestShippedConnectors:
    def test_every_first_party_manifest_passes_lint(self):
        registry = default_registry()
        known = frozenset(registry.capability_ids())
        for cls in FIRST_PARTY:
            assert lint(cls.manifest, known) == [], cls.manifest.id

    def test_every_capability_has_a_closed_schema_and_a_tier(self):
        registry = default_registry()
        for capability_id in registry.capability_ids():
            capability = registry.capability(capability_id)
            assert capability.parameters["additionalProperties"] is False, capability_id
            assert capability.tier is not None, capability_id

    def test_the_write_capabilities_are_exactly_the_reviewed_ones(self):
        """A new write is a decision, not a side effect of a change: it has to be added here.

        That none of them can be invoked without a policy decision is proved in
        tests/unit/actions/test_write_needs_a_decision.py.
        """
        registry = default_registry()
        writes = [c for c in registry.capability_ids() if registry.capability(c).kind is Kind.WRITE]
        assert writes == ["autosys.job.force_start"]
        force_start = registry.capability("autosys.job.force_start")
        assert force_start.tier is RiskTier.T1 and not force_start.idempotent
        assert force_start.rollback.startswith("none:") and not force_start.supports_dry_run

    def test_every_check_step_type_names_a_registered_read_capability(self):
        assert set(STEP_CAPABILITIES) == set(CHECK_STEP_TYPES)
        for step_type, capability_id in STEP_CAPABILITIES.items():
            capability = default_registry().capability(capability_id)
            assert capability is not None and capability.kind is Kind.READ, step_type

    def test_manifests_export_as_plain_data(self):
        import json

        for item in default_registry().manifests():
            exported = json.loads(json.dumps(item.to_dict()))
            assert exported["id"] == item.id
            assert "value" not in json.dumps(exported["secrets"]), "slots carry no values"


class TestWriteCapabilityRules:
    def test_a_complete_write_capability_passes(self):
        assert problems(FORCE_START) == []

    @pytest.mark.parametrize(
        "change, expected",
        [
            (dict(tier=None), "needs a risk tier"),
            (dict(tier=RiskTier.T0), "is tier T1 or T2"),
            (dict(verify=None), "needs a verify operation"),
            (dict(verify=Verify("demo.job.status", "")), "needs a verify operation"),
            (dict(verify=Verify("demo.job.absent", "result.ok")), "unknown capability"),
            (dict(scopes=()), "must list the scopes"),
            (dict(allow_lists=()), "has no allow list binding"),
            (dict(target_parameters=()), "must name its target parameters"),
            (dict(rollback=""), "declare a rollback"),
            (dict(rollback="demo.job.absent"), "rollback names unknown capability"),
            (
                dict(pre_checks=(PreCheck("demo.job.absent", "result.ok"),)),
                "pre-check names unknown capability",
            ),
            (dict(pre_checks=("demo.job.status",)), "a pre-check is a capability with"),
            (dict(pre_checks=(PreCheck("demo.job.status", ""),)), "needs an assertion"),
            (dict(pre_checks=(PreCheck("demo.job.status", "result =="),)), "does not parse"),
            (
                dict(pre_checks=(PreCheck("demo.job.status", "incident.priority == 1"),)),
                "unknown name",
            ),
            (dict(verify=Verify("demo.job.status", "result ==")), "does not parse"),
            (
                dict(verify=Verify("demo.job.status", "result.ok", retries=1000)),
                "verify waits at most",
            ),
            (
                dict(parameters={"type": "object", "properties": {"job_name": {}}}),
                "additionalProperties false",
            ),
            (
                dict(
                    parameters={
                        "type": "object",
                        "additionalProperties": True,
                        "properties": {"job_name": {}},
                    }
                ),
                "additionalProperties false",
            ),
        ],
    )
    def test_a_write_missing_any_required_part_fails_lint(self, change, expected):
        found = problems(replace(FORCE_START, **change))
        assert any(expected in p for p in found), found

    def test_a_tier_three_capability_cannot_be_declared(self):
        assert [t.value for t in RiskTier] == ["T0", "T1", "T2"]

    def test_a_read_cannot_claim_a_write_tier(self):
        assert any("tier T0" in p for p in problems(replace(STATUS, tier=RiskTier.T1)))

    def test_an_allow_list_must_be_a_connection_setting(self):
        binding = AllowListBinding("job_name", "not_a_setting", "exact")
        found = problems(replace(FORCE_START, allow_lists=(binding,)))
        assert any("is not a connection setting" in p for p in found)

    def test_capability_ids_belong_to_their_connector(self):
        found = lint(manifest(replace(STATUS, id="other.job.status")))
        assert any("must start with 'demo.'" in p for p in found)


class TestRegistry:
    def cls(self, *capabilities):
        return type("DemoConnector", (), {"manifest": manifest(*capabilities)})

    def test_a_connector_that_fails_lint_is_not_registered(self):
        bad = replace(FORCE_START, verify=None)
        with pytest.raises(ManifestError) as err:
            Registry([self.cls(STATUS, bad)])
        assert any("verify" in p for p in err.value.problems)

    def test_two_connectors_cannot_declare_the_same_capability(self):
        other = type("Other", (), {"manifest": replace(manifest(STATUS), id="demo2")})
        with pytest.raises(ManifestError):
            Registry([self.cls(STATUS), other])

    def test_the_same_connector_cannot_be_registered_twice(self):
        with pytest.raises(ManifestError):
            Registry([self.cls(STATUS), self.cls(STATUS)])

    def test_lookup(self):
        registry = Registry([self.cls(STATUS, FORCE_START)])
        assert registry.owner("demo.job.force_start") == "demo"
        assert registry.capability("demo.job.absent") is None
        assert registry.capability_ids() == ["demo.job.force_start", "demo.job.status"]


class _Demo(Connector):
    manifest = manifest(STATUS, FORCE_START)
    calls: list

    async def _execute(self, capability, parameters):
        self.calls.append((capability.id, parameters))
        return AdapterResult(success=True, data={"status": "SU"})

    async def health(self):
        return Health(HealthStatus.HEALTHY)


class TestEntryPoint:
    def build(self, **config) -> _Demo:
        connector = _Demo(config)
        connector.calls = []
        return connector

    async def test_a_read_needs_a_step_context(self):
        with pytest.raises(PermissionError):
            await self.build().invoke("demo.job.status", {"job_name": "J"}, None)

    async def test_a_write_cannot_be_invoked_at_all_yet(self):
        connector = self.build(allowed_jobs=["J"])
        with pytest.raises(WriteNotAuthorized):
            await connector.invoke("demo.job.force_start", {"job_name": "J"}, CTX)
        with pytest.raises(WriteNotAuthorized):
            await connector.invoke("demo.job.force_start", {"job_name": "J"}, CTX, grant=object())
        assert connector.calls == []

    async def test_parameters_outside_the_schema_never_reach_the_connector(self):
        connector = self.build()
        for parameters in ({}, {"job_name": 5}, {"job_name": "J", "extra": 1}):
            with pytest.raises(Refused):
                await connector.invoke("demo.job.status", parameters, CTX)
        assert connector.calls == []

    async def test_an_unknown_capability_is_not_dispatched(self):
        with pytest.raises(LookupError):
            await self.build().invoke("demo.job.delete", {}, CTX)

    def test_an_empty_allow_list_denies_and_an_exact_list_is_enforced(self):
        capability = FORCE_START
        empty = self.build()
        listed = self.build(allowed_jobs=["BATCH_A"])
        assert "is empty" in empty._allow_list_problems(capability, {"job_name": "BATCH_A"})[0]
        assert listed._allow_list_problems(capability, {"job_name": "BATCH_A"}) == []
        assert (
            "not on allow list"
            in (listed._allow_list_problems(capability, {"job_name": "BATCH_B"})[0])
        )

    async def test_the_mocks_are_reached_only_through_the_same_checks(self):
        connectors = connectors_for_adapters(build_mock_adapters())
        ok = await connectors.invoke("autosys.job.status", {"job_name": "BATCH_X"}, CTX)
        assert ok.success
        with pytest.raises(Refused):
            await connectors.invoke("autosys.job.status", {"job_name": "B", "force": True}, CTX)
        assert connectors.refusals("splunk.absent", {}) == ["Unknown capability 'splunk.absent'"]

    def test_connection_settings_are_checked_against_the_manifest(self):
        from src.l1_agent.config.settings import ConfigError

        with pytest.raises(ConfigError):
            _Demo({"allowed_jobs": "everything"})
        with pytest.raises(ConfigError):
            _Demo({"surprise": True})
