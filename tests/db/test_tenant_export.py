"""Leaving with your data: the export, and checking it without the product's database."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from src.l1_agent import cli
from src.l1_agent.actions import store as action_store
from src.l1_agent.audit.log import Actor, events_for
from src.l1_agent.auth import store as auth_store
from src.l1_agent.connectors import hub as connections
from src.l1_agent.demo import DEMO_INCIDENTS
from src.l1_agent.models.incident import Incident
from src.l1_agent.service.export import (
    MANIFEST,
    NOT_EXPORTED,
    ExportError,
    export_tenant,
    verify_export,
)
from tests.api.support import seed_runner
from tests.conftest import make_service

ADMIN = Actor("user", "https://idp.test|admin-1")
INTAKE = Actor.system("test-intake")
SECRET_VALUE = "hunter2-not-a-reference"


async def populated(backend, label: str, incident_index: int) -> uuid.UUID:
    tenant = await backend.new_tenant(label)
    service = make_service(backend.db)
    await service.ingest(tenant, Incident.from_servicenow(DEMO_INCIDENTS[incident_index]), INTAKE)
    await service.work_once([tenant])
    await connections.set_connection(
        backend.db,
        tenant,
        ADMIN,
        "splunk",
        {"base_url": f"https://{label}-splunk.example"},
        {"token": "vault:splunk/token"},
    )
    await action_store.set_policy(backend.db, tenant, ADMIN, autonomy="shadow")
    await auth_store.set_role_mapping(
        backend.db, tenant, f"{label}-ops", "operator", ADMIN, grant=True
    )
    await auth_store.create_api_token(backend.db, tenant, "t", ["incidents.read"], 1, ADMIN)
    await seed_runner(backend.db, tenant, f"{label}-runner")
    return tenant


def lines(directory: Path, name: str) -> list:
    text = (directory / name).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class TestExport:
    async def test_it_writes_the_tenants_records_and_nothing_of_another_tenant(
        self, backend, tmp_path
    ):
        mine = await populated(backend, "mine", 0)
        other = await populated(backend, "other", 1)
        out = tmp_path / "export"
        manifest = await export_tenant(backend.db, mine, "mine", out, ADMIN)

        assert manifest["tenant_id"] == str(mine) and manifest["format"] == 1
        assert json.loads((out / MANIFEST).read_text(encoding="utf-8")) == manifest
        for name, info in manifest["files"].items():
            rows = lines(out, name)
            assert len(rows) == info["rows"], name
            assert all(r["tenant_id"] == str(mine) for r in rows), name
        everything = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
        assert str(other) not in everything and "other-splunk.example" not in everything

        assert [r["number"] for r in lines(out, "runs.jsonl")] == [DEMO_INCIDENTS[0]["number"]]
        assert lines(out, "runs.jsonl")[0]["outcome"] in ("resolved", "escalated", "needs_human")
        assert lines(out, "run_steps.jsonl"), "the steps of the run are there"
        assert lines(out, "policies.jsonl")[0]["autonomy"] == "shadow"
        assert lines(out, "role_mappings.jsonl")[0]["idp_group"] == "mine-ops"
        assert lines(out, "runners.jsonl")[0]["name"] == "mine-runner"
        splunk = lines(out, "connections.jsonl")[0]
        assert splunk["config"] == {"base_url": "https://mine-splunk.example"}
        assert splunk["secret_refs"] == {"token": "vault:splunk/token"}, "a reference, as stored"

    async def test_nothing_that_authenticates_is_written(self, backend, tmp_path):
        tenant = await populated(backend, "auth", 0)
        out = tmp_path / "export"
        manifest = await export_tenant(backend.db, tenant, "auth", out, ADMIN)
        written = {name.removesuffix(".jsonl") for name in manifest["files"]}
        assert written.isdisjoint(NOT_EXPORTED)
        assert {"api_tokens", "sessions", "users"} <= set(manifest["not_exported"])
        everything = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
        assert "l1t_" not in everything, "no API token"
        assert "token_hash" not in everything and "csrf_token" not in everything

    async def test_the_audit_log_in_the_export_is_the_log_and_the_export_is_itself_audited(
        self, backend, tmp_path
    ):
        tenant = await populated(backend, "audit", 0)
        before = await events_for(backend.db, tenant, limit=5000)
        out = tmp_path / "export"
        await export_tenant(backend.db, tenant, "audit", out, ADMIN)
        exported = lines(out, "audit_events.jsonl")
        assert [e["seq"] for e in exported] == [e["seq"] for e in before]
        assert [e["hash"] for e in exported] == [e["hash"] for e in before]
        after = await events_for(backend.db, tenant, limit=5000)
        assert after[-1]["event_type"] == "tenant.exported"
        assert after[-1]["payload"]["files"]["audit_events.jsonl"] == len(before)

    async def test_an_export_is_never_written_over_another(self, backend, tmp_path):
        tenant = await populated(backend, "twice", 0)
        out = tmp_path / "export"
        await export_tenant(backend.db, tenant, "twice", out, ADMIN)
        with pytest.raises(ExportError, match="not empty"):
            await export_tenant(backend.db, tenant, "twice", out, ADMIN)


class TestVerifyingAnExportFromItsFilesAlone:
    @pytest.fixture
    async def export(self, backend, tmp_path) -> Path:
        tenant = await populated(backend, "check", 0)
        out = tmp_path / "export"
        await export_tenant(backend.db, tenant, "check", out, ADMIN)
        return out

    def test_an_untouched_export_verifies(self, export):
        assert verify_export(export) == []
        assert cli.main(["export", "verify", "--dir", str(export)]) == 0

    def test_a_changed_audit_event_is_found_even_if_the_manifest_is_fixed_up_too(self, export):
        import hashlib

        log = export / "audit_events.jsonl"
        rows = lines(export, "audit_events.jsonl")
        rows[1]["event_type"] = "something.else"
        text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=True) + "\n" for r in rows)
        log.write_text(text, encoding="utf-8", newline="\n")
        problems = verify_export(export)
        assert "audit_events.jsonl does not match the manifest" in problems
        assert any("content does not match its hash" in p for p in problems)

        # Someone who can also rewrite the manifest still cannot make the chain agree.
        manifest = json.loads((export / MANIFEST).read_text(encoding="utf-8"))
        manifest["files"]["audit_events.jsonl"]["sha256"] = hashlib.sha256(
            log.read_bytes()
        ).hexdigest()
        (export / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        problems = verify_export(export)
        assert problems and all("audit event" in p for p in problems)
        assert cli.main(["export", "verify", "--dir", str(export)]) == 2

    def test_a_removed_event_and_a_removed_file_are_found(self, export):
        log = export / "audit_events.jsonl"
        kept = log.read_text(encoding="utf-8").splitlines(keepends=True)
        log.write_text("".join(kept[:1] + kept[2:]), encoding="utf-8", newline="\n")
        problems = verify_export(export)
        assert any("expected event 2, found 3" in p for p in problems)
        assert any("does not follow the event before it" in p for p in problems)

        (export / "runs.jsonl").unlink()
        assert "runs.jsonl is missing" in verify_export(export)

    def test_a_directory_that_is_not_an_export_says_so(self, tmp_path):
        assert verify_export(tmp_path) == ["the manifest cannot be read: FileNotFoundError"]
        (tmp_path / MANIFEST).write_text("{}", encoding="utf-8")
        assert verify_export(tmp_path) == ["the manifest cannot be read: KeyError"]
