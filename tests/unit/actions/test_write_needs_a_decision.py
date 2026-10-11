"""No write without a policy decision (Phase 4 exit criterion 2).

Three layers:
  1. every write capability in the registry is refused at the connector's
     entry point unless it is given a grant, and the grant is checked;
  2. a grant is only good for the one call it was minted for, once, briefly,
     and only if this process's authority minted it;
  3. a search of the source tree finds the mint, and the hooks around it,
     called only from the places the design names.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Set

import pytest

from src.l1_agent.actions.grants import GrantAuthority
from src.l1_agent.adapters.base import AdapterResult
from src.l1_agent.connectors.catalog import connectors_for_adapters, default_registry
from src.l1_agent.connectors.sdk.base import (
    Connector,
    Health,
    HealthStatus,
    Refused,
    StepContext,
    WriteNotAuthorized,
)
from src.l1_agent.connectors.sdk.grant import Grant, call_digest
from src.l1_agent.connectors.sdk.manifest import Kind
from src.l1_agent.demo import build_mock_adapters

SRC = Path(__file__).resolve().parents[3] / "src" / "l1_agent"
CTX = StepContext("tenant-1", "run-1", "step-1")
FORCE_START = "autosys.job.force_start"
PARAMS = {"job_name": "NIGHTLY_LOAD"}


class DirectVerifier:
    """A connector's view of the policy engine: an authority and nothing else."""

    def __init__(self, authority: GrantAuthority) -> None:
        self._authority = authority

    async def redeem(self, grant, **call) -> None:
        self._authority.redeem(grant, **call)


def mint(authority: GrantAuthority, **changes) -> Grant:
    values = dict(
        decision_id="decision-1",
        tenant_id="tenant-1",
        run_id="run-1",
        step_id="step-1",
        capability_id=FORCE_START,
        call_digest=call_digest(FORCE_START, PARAMS),
    )
    values.update(changes)
    return authority.mint(**values)


def write_capabilities() -> List[str]:
    registry = default_registry()
    return [c for c in registry.capability_ids() if registry.capability(c).kind is Kind.WRITE]


def sample_parameters(capability_id: str) -> Dict[str, object]:
    """Parameters that satisfy a capability's schema, so a refusal is about the grant."""
    schema = default_registry().capability(capability_id).parameters
    made: Dict[str, object] = {}
    for name in schema.get("required", []):
        spec = schema["properties"][name]
        made[name] = spec.get("enum", ["X_1"])[0] if spec.get("type") == "string" else 1
    return made


# ── 1. The entry point ────────────────────────────────────────────────


class TestEveryWriteCapability:
    def test_there_is_something_to_check(self):
        assert FORCE_START in write_capabilities()

    @pytest.mark.parametrize("capability_id", write_capabilities())
    async def test_it_is_refused_without_a_grant_and_with_anything_that_is_not_one(
        self, capability_id
    ):
        adapters = build_mock_adapters()
        connectors = connectors_for_adapters(adapters)
        parameters = sample_parameters(capability_id)
        assert connectors.refusals(capability_id, parameters) == [], "the call itself is valid"
        authority = GrantAuthority()

        # No verifier: the connector has not been attached to a policy engine at all.
        for grant in (None, object(), "grant", mint(authority, capability_id=capability_id)):
            with pytest.raises(WriteNotAuthorized):
                await connectors.invoke(capability_id, parameters, CTX, grant)

        connectors.authorise_writes(DirectVerifier(authority))
        forged = Grant("d", "tenant-1", "run-1", "step-1", capability_id, "x" * 64, 9e12, "n", "s")
        stranger = mint(
            GrantAuthority(),
            capability_id=capability_id,
            call_digest=call_digest(capability_id, parameters),
        )
        for grant in (None, object(), {"signature": "x"}, forged, stranger):
            with pytest.raises(WriteNotAuthorized):
                await connectors.invoke(capability_id, parameters, CTX, grant)
        assert adapters["autosys"].force_starts == [], "nothing reached the target"

    async def test_with_a_grant_for_exactly_this_call_the_write_is_made_once(self):
        adapters = build_mock_adapters()
        connectors = connectors_for_adapters(adapters)
        authority = GrantAuthority()
        connectors.authorise_writes(DirectVerifier(authority))
        grant = mint(authority)

        result = await connectors.invoke(FORCE_START, PARAMS, CTX, grant)
        assert result.success and adapters["autosys"].force_starts == ["NIGHTLY_LOAD"]
        with pytest.raises(WriteNotAuthorized, match="already been used"):
            await connectors.invoke(FORCE_START, PARAMS, CTX, grant)
        assert adapters["autosys"].force_starts == ["NIGHTLY_LOAD"]

    async def test_a_write_needs_a_step_context_like_any_call(self):
        connectors = connectors_for_adapters(build_mock_adapters())
        with pytest.raises(PermissionError):
            await connectors.invoke(FORCE_START, PARAMS, None, object())

    async def test_a_refused_call_does_not_use_the_grant_up(self):
        adapters = build_mock_adapters()
        connectors = connectors_for_adapters(
            adapters, {"autosys": {"force_start_jobs": ["NIGHTLY_*"]}}
        )
        authority = GrantAuthority()
        connectors.authorise_writes(DirectVerifier(authority))
        other = {"job_name": "PAYROLL_POST"}
        grant = mint(authority, call_digest=call_digest(FORCE_START, other))
        with pytest.raises(Refused, match="not on allow list"):
            await connectors.invoke(FORCE_START, other, CTX, grant)
        assert adapters["autosys"].force_starts == []

    async def test_an_empty_allow_list_refuses_every_target(self):
        connectors = connectors_for_adapters(
            build_mock_adapters(), {"autosys": {"force_start_jobs": []}}
        )
        authority = GrantAuthority()
        connectors.authorise_writes(DirectVerifier(authority))
        with pytest.raises(Refused, match="is empty"):
            await connectors.invoke(FORCE_START, PARAMS, CTX, mint(authority))

    async def test_a_dry_run_is_refused_where_the_capability_has_none(self):
        connectors = connectors_for_adapters(build_mock_adapters())
        with pytest.raises(Refused, match="no dry run"):
            await connectors.invoke(FORCE_START, PARAMS, CTX, dry_run=True)


# ── 2. The grant ──────────────────────────────────────────────────────


class TestGrant:
    CALL = dict(
        tenant_id="tenant-1",
        run_id="run-1",
        step_id="step-1",
        capability_id=FORCE_START,
        digest=call_digest(FORCE_START, PARAMS),
    )

    def test_a_grant_is_good_for_its_own_call_once(self):
        authority = GrantAuthority()
        grant = mint(authority)
        authority.redeem(grant, **self.CALL)
        with pytest.raises(WriteNotAuthorized, match="already been used"):
            authority.redeem(grant, **self.CALL)

    @pytest.mark.parametrize(
        "change, why",
        [
            (dict(tenant_id="tenant-2"), "different step"),
            (dict(run_id="run-2"), "different step"),
            (dict(step_id="step-2"), "different step"),
            (dict(capability_id="autosys.job.delete"), "different step"),
            (dict(digest=call_digest(FORCE_START, {"job_name": "PAYROLL_POST"})), "parameters"),
            (dict(digest=call_digest(FORCE_START, {**PARAMS, "extra": 1})), "parameters"),
        ],
    )
    def test_it_is_not_good_for_any_other_call(self, change, why):
        authority = GrantAuthority()
        with pytest.raises(WriteNotAuthorized, match=why):
            authority.redeem(mint(authority), **{**self.CALL, **change})

    def test_changing_any_field_of_a_grant_breaks_its_signature(self):
        authority = GrantAuthority()
        grant = mint(authority)
        for field, value in (
            ("decision_id", "decision-2"),
            ("tenant_id", "tenant-2"),
            ("run_id", "run-2"),
            ("step_id", "step-2"),
            ("capability_id", "autosys.job.delete"),
            ("call_digest", "0" * 64),
            ("expires_at", grant.expires_at + 3600),
            ("nonce", "other"),
            ("signature", "0" * 64),
        ):
            with pytest.raises(WriteNotAuthorized, match="not issued by the policy engine"):
                authority.redeem(replace(grant, **{field: value}), **self.CALL)

    def test_a_grant_expires(self):
        now = [1000.0]
        authority = GrantAuthority(ttl_seconds=60, clock=lambda: now[0])
        grant = mint(authority)
        now[0] += 60
        with pytest.raises(WriteNotAuthorized, match="expired"):
            authority.redeem(grant, **self.CALL)

    def test_a_grant_from_another_authority_is_refused(self):
        with pytest.raises(WriteNotAuthorized, match="not issued by the policy engine"):
            GrantAuthority().redeem(mint(GrantAuthority()), **self.CALL)

    def test_a_grant_is_not_minted_without_a_decision(self):
        with pytest.raises(ValueError):
            mint(GrantAuthority(), decision_id="")

    def test_the_key_is_not_reachable_from_a_grant(self):
        authority = GrantAuthority()
        grant = mint(authority)
        assert authority._key.hex() not in repr(grant)
        assert len(authority._key) == 32 and authority._key != GrantAuthority()._key


# ── 3. The source tree ────────────────────────────────────────────────


def calls_of(name: str) -> Dict[str, Set[str]]:
    """Every call to a function or method called `name`: file -> the functions it is called in."""
    found: Dict[str, Set[str]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                called = node.func
                label = getattr(called, "attr", None) or getattr(called, "id", None)
                if label == name:
                    found.setdefault(path.relative_to(SRC).as_posix(), set()).add(function.name)
    return found


def assignments_of(attribute: str) -> Set[str]:
    found: Set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and any(
                isinstance(t, ast.Attribute) and t.attr == attribute for t in targets
            ):
                found.add(path.relative_to(SRC).as_posix())
    return found


class TestSourceTree:
    def test_a_grant_is_minted_only_after_the_database_authorised_the_action(self):
        assert calls_of("mint") == {"actions/service.py": {"_authorise", "_authorise_rollback"}}
        source = (SRC / "actions" / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name, must_come_first in (
            ("_authorise", "authorise_execution"),
            ("_authorise_rollback", "transition"),
        ):
            function = next(
                n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name
            )
            lines = {
                getattr(c.func, "attr", ""): c.lineno
                for c in ast.walk(function)
                if isinstance(c, ast.Call)
            }
            assert lines[must_come_first] < lines["mint"], f"{name} mints before it records"

    def test_the_authority_is_built_in_one_place(self):
        assert set(calls_of("GrantAuthority")) == {"actions/service.py"}

    def test_the_database_step_that_authorises_is_called_from_one_place(self):
        assert calls_of("authorise_execution") == {"actions/service.py": {"_authorise"}}

    def test_only_the_engine_attaches_a_verifier_and_only_the_sdk_stores_one(self):
        assert calls_of("authorise_writes") == {"engine/executor.py": {"execute_sop"}}
        assert assignments_of("grant_verifier") == {
            "connectors/sdk/base.py",
            "connectors/sdk/registry.py",
        }

    def test_a_connector_write_is_reached_only_through_the_entry_point(self):
        assert calls_of("_execute_write") == {"connectors/sdk/base.py": {"_invoke_write"}}
        assert calls_of("_invoke_write") == {"connectors/sdk/base.py": {"invoke"}}
        # The mock scheduler's own method, and the real command, are called from one connector.
        assert calls_of("force_start") == {"connectors/ops/autosys.py": {"_execute_write"}}
        assert calls_of("_sendevent") == {"connectors/ops/autosys.py": {"_execute_write"}}

    def test_no_adapter_or_engine_module_runs_sendevent(self):
        offenders = [
            path.relative_to(SRC).as_posix()
            for path in SRC.rglob("*.py")
            if "FORCE_STARTJOB" in path.read_text(encoding="utf-8")
            and path.relative_to(SRC).as_posix()
            not in ("connectors/ops/autosys.py", "adapters/mock_adapters.py")
        ]
        assert offenders == []


class TestAConnectorThatForgetsToImplementAWrite:
    async def test_it_fails_loudly_instead_of_doing_something_else(self):
        from tests.unit.connectors.test_manifest_lint import FORCE_START as DEMO_WRITE
        from tests.unit.connectors.test_manifest_lint import STATUS, manifest

        class Half(Connector):
            async def _execute(self, capability, parameters):
                return AdapterResult(success=True)

            async def health(self):
                return Health(HealthStatus.HEALTHY)

        Half.manifest = manifest(STATUS, DEMO_WRITE)
        connector = Half({"allowed_jobs": ["J"]})
        authority = GrantAuthority()
        connector.grant_verifier = DirectVerifier(authority)
        parameters = {"job_name": "J"}
        grant = mint(
            authority,
            capability_id=DEMO_WRITE.id,
            call_digest=call_digest(DEMO_WRITE.id, parameters),
        )
        with pytest.raises(NotImplementedError):
            await connector.invoke(DEMO_WRITE.id, parameters, CTX, grant)
