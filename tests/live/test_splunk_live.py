"""Live tests against a Splunk lab. They seed events and search for them.

Skipped unless SPLUNK_LIVE_TESTS=1. Run them on purpose:

    $env:SPLUNK_LIVE_TESTS = "1"
    pytest tests/live -m splunk_live -v

THESE TESTS WRITE EVENTS to the indexes `batch_logs` and `app_logs` through
the HTTP Event Collector. Use a lab, never a Splunk anyone relies on.

Needs SPLUNK_HEC_TOKEN (and SPLUNK_HEC_URL if not https://localhost:8088),
SPLUNK_ALLOWED_INDEXES containing both indexes, and for each backend its own
settings: SPLUNK_MCP_URL with SPLUNK_MCP_AUTH, SPLUNK_BASE_URL with
SPLUNK_TOKEN. A backend that is not configured is skipped, and the summary
says so. AGENT_DEMO_MODE=true is needed for SPLUNK_VERIFY_SSL=false.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest
from dotenv import load_dotenv

from src.l1_agent.config.settings import Settings
from src.l1_agent.connectors.hub import environment_connections
from src.l1_agent.connectors.ops.splunk import SplunkConnector
from src.l1_agent.connectors.sdk.base import StepContext
from src.l1_agent.connectors.sdk.secrets import ConnectionSecrets, SecretResolver
from tests.e2e.test_splunk_hybrid import (
    AUTOSYS_INCIDENT,
    SEARCH_STEP,
    hybrid_settings,
    run_incident,
    step,
)

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

pytestmark = [
    pytest.mark.splunk_live,
    pytest.mark.skipif(
        os.environ.get("SPLUNK_LIVE_TESTS") != "1", reason="set SPLUNK_LIVE_TESTS=1 to run"
    ),
]

CTX = StepContext(None, "live-test", "search")
WAIT_SECONDS = 60
_spec = importlib.util.spec_from_file_location("seed_splunk", ROOT / "scripts" / "seed_splunk.py")
seed_splunk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_splunk)

AUTOSYS_QUERY = 'index=batch_logs source=autosys "BATCH_PAYMENT_PROCESS" (FAILURE OR ERROR)'


def configured(backend: str) -> bool:
    if backend == "mcp":
        return bool(os.environ.get("SPLUNK_MCP_URL") and os.environ.get("SPLUNK_MCP_AUTH"))
    return bool(os.environ.get("SPLUNK_BASE_URL") and os.environ.get("SPLUNK_TOKEN"))


def settings_for(backend: str) -> Settings:
    settings = Settings.from_env()
    return dataclasses.replace(
        settings, splunk=dataclasses.replace(settings.splunk, backend=backend)
    )


def connector_for(backend: str) -> SplunkConnector:
    if not configured(backend):
        pytest.skip(f"the {backend} backend is not configured")
    settings = settings_for(backend)
    connection = environment_connections(settings)["splunk"]
    return SplunkConnector(
        dict(connection.config),
        ConnectionSecrets(SecretResolver.from_settings(settings.secrets), connection.secret_refs),
        demo_mode=settings.agent.demo_mode,
    )


async def search_until(connector: SplunkConnector, query: str, count: int) -> Any:
    """Search until the seeded events are indexed. Returns the last result."""
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        result = await connector.invoke(
            "splunk.search", {"query": query, "time_range": {"earliest": "-4h"}}, CTX
        )
        assert result.success, result.error
        if result.data["result_count"] >= count or time.monotonic() > deadline:
            return result
        await asyncio.sleep(2)


@pytest.fixture(scope="module")
def seeded() -> Dict[str, Any]:
    if not os.environ.get("SPLUNK_HEC_TOKEN"):
        pytest.skip("SPLUNK_HEC_TOKEN is not set, so nothing can be seeded")
    return seed_splunk.seed(["autosys", "healthy"])


@pytest.mark.parametrize("backend", ["mcp", "rest"])
class TestEachBackend:
    async def test_health(self, backend):
        connector = connector_for(backend)
        try:
            health = await connector.health()
            assert health.status.value == "healthy", health.reason
        finally:
            await connector.close()

    async def test_the_sop_s_search_finds_the_seeded_failure(self, backend, seeded):
        connector = connector_for(backend)
        try:
            result = await search_until(
                connector, f'{AUTOSYS_QUERY} "seed_run={seeded["run_id"]}"', 2
            )
        finally:
            await connector.close()
        assert result.data["result_count"] == 2
        rows = result.data["results"]
        assert {row["source"] for row in rows} == {"autosys"}
        assert {row["host"] for row in rows} == {"batch-server-01"}
        assert {row["index"] for row in rows} == {"batch_logs"}
        assert any("exit_code=1" in row["_raw"] for row in rows)
        assert "exit code 1" in result.evidence_snippet
        assert result.audit["backend"] == backend and result.audit["outcome"] == "ok"

    async def test_the_healthy_job_has_no_failure(self, backend, seeded):
        connector = connector_for(backend)
        try:
            # Wait for the healthy events themselves, so that "none" is not "not indexed yet".
            there = await search_until(
                connector,
                f'index=batch_logs source=autosys "{seed_splunk.HEALTHY_JOB}" '
                f'"seed_run={seeded["run_id"]}"',
                2,
            )
            assert there.data["result_count"] == 2
            result = await connector.invoke(
                "splunk.search",
                {
                    "query": f'index=batch_logs source=autosys "{seed_splunk.HEALTHY_JOB}" '
                    f'(FAILURE OR ERROR) "seed_run={seeded["run_id"]}"',
                    "time_range": {"earliest": "-4h"},
                },
                CTX,
            )
        finally:
            await connector.close()
        assert result.success and result.data == {"result_count": 0, "results": []}

    async def test_a_refused_search_is_refused_here_too(self, backend):
        connector = connector_for(backend)
        try:
            result = await connector.invoke("splunk.search", {"query": "index=_internal"}, CTX)
        finally:
            await connector.close()
        assert not result.success and result.escalate and result.audit["outcome"] == "rejected"


async def test_both_backends_return_the_same_shape(seeded):
    if not (configured("mcp") and configured("rest")):
        pytest.skip("needs both backends configured")
    query = f'{AUTOSYS_QUERY} "seed_run={seeded["run_id"]}" | sort _time'
    results = {}
    for backend in ("mcp", "rest"):
        connector = connector_for(backend)
        try:
            results[backend] = await search_until(connector, query, 2)
        finally:
            await connector.close()
    assert results["mcp"].data == results["rest"].data
    assert results["mcp"].evidence_snippet == results["rest"].evidence_snippet


@pytest.mark.parametrize("backend", ["mcp", "rest"])
async def test_the_autosys_incident_end_to_end_with_splunk_live(backend, seeded, tmp_path):
    connector = connector_for(backend)
    try:
        await search_until(connector, f'{AUTOSYS_QUERY} "seed_run={seeded["run_id"]}"', 2)
    finally:
        await connector.close()
    record, notes = await run_incident(
        hybrid_settings(tmp_path, settings_for(backend).splunk), AUTOSYS_INCIDENT
    )
    search = step(record, SEARCH_STEP)
    assert search["status"] == "success" and search["audit"]["backend"] == backend
    assert search["audit"]["result_count"] >= 2
    assert f"seed_run={seeded['run_id']}" in search["evidence_snippet"]
    note = next(n["note"] for n in notes if f"Step {SEARCH_STEP} " in n["note"])
    assert f"seed_run={seeded['run_id']}" in note
    assert record["outcome"] == "escalated"
    assert "still in a failed state" in record["escalation_reason"]


async def test_an_index_off_the_allow_list_escalates_end_to_end(tmp_path):
    backend = "mcp" if configured("mcp") else "rest"
    if not configured(backend):
        pytest.skip("no backend is configured")
    splunk = dataclasses.replace(settings_for(backend).splunk, allowed_indexes=("app_logs",))
    record, _ = await run_incident(hybrid_settings(tmp_path, splunk), AUTOSYS_INCIDENT)
    assert record["outcome"] == "escalated"
    assert "index 'batch_logs' is not on the allow list" in record["escalation_reason"]
    assert step(record, SEARCH_STEP)["audit"]["outcome"] == "rejected"
