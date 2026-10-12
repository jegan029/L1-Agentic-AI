"""Phase 6 exit criterion 1: the whole path, from an empty tenant to a first run.

Onboard a tenant, connect ServiceNow, a knowledge source, Autosys and Splunk,
approve an SOP, set autonomy to shadow, run the scenario. Every step is taken
the way an operator or a user would take it: the command line for the tenant
and the runner's key, the HTTP API (with a different person for each duty)
for everything else, and the real service assembly with a runner attached.

What stands in for the outside world:
  - ServiceNow is `fake_servicenow.py`, an HTTPS server written by hand from
    the Table API documentation. The real ServiceNow connector talks to it.
  - Autosys and Splunk are the mock adapters, held by a real runner. Every
    call to them crosses the runner protocol.
  - The SOP source is a real Git repository on local disk.
  - Sign-in is a session written to the database; no identity provider runs.

The deployment is in demo mode, because only demo mode accepts a certificate
nothing trusts, a local Git path and mock connectors. Nothing is seeded: the
tenant starts empty (`AGENT_DEMO_SEED=false`).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from src.l1_agent import cli
from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.audit import log as audit
from src.l1_agent.config.settings import (
    AgentSettings,
    AuthSettings,
    DatabaseSettings,
    LLMSettings,
    RunnerSettings,
    ServiceNowSettings,
    Settings,
    WorkerSettings,
)
from src.l1_agent.demo import _restart_incident, build_mock_adapters
from src.l1_agent.main import L1AgentService
from src.l1_agent.runner import keys
from src.l1_agent.runner.agent import Runner
from src.l1_agent.service.runs import record_by_number
from tests.actions_support import RESTART_FILE, RESTART_SOP, reconstruct
from tests.api.support import BASE_URL, SECRET, session
from tests.e2e.fake_servicenow import AGENT_SYS_ID, FakeServiceNow

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

JOB = "NIGHTLY_SETTLEMENT_LOAD"
NUMBER = "INC0042001"
SYS_ID = "b" * 32
INTAKE_GROUP = "L1-Batch-Support"


class Counting(BaseAdapter):
    """A mock adapter that remembers each call made to it."""

    def __init__(self, inner: BaseAdapter) -> None:
        self._inner = inner
        self.calls: List[Dict[str, Any]] = []

    async def execute(self, parameters: Dict[str, Any]) -> AdapterResult:
        self.calls.append(dict(parameters))
        return await self._inner.execute(parameters)

    async def health_check(self) -> bool:
        self.calls.append({"health_check": True})
        return await self._inner.health_check()

    @property
    def adapter_name(self) -> str:
        return self._inner.adapter_name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class Person:
    """One signed-in user with their own roles, cookie and CSRF token."""

    def __init__(self, app, cookies: dict, csrf: str) -> None:
        self._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
        self._http.cookies.update(cookies)
        self._headers = {"X-CSRF-Token": csrf}

    async def get(self, path: str, **params: Any) -> httpx.Response:
        return await self._http.get(path, params=params)

    async def send(self, method: str, path: str, body: Optional[dict] = None) -> httpx.Response:
        return await self._http.request(method, path, json=body, headers=self._headers)

    async def close(self) -> None:
        await self._http.aclose()


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def sop_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "sop-repo"
    (repo / "sops").mkdir(parents=True)
    git(repo, "init", "--quiet", "-b", "main")
    shutil.copy(RESTART_FILE, repo / "sops" / "autosys_job_restart.json")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "restart SOP")
    return repo


def steps_of(report: dict) -> Dict[str, bool]:
    return {s["id"]: s["done"] for s in report["steps"]}


@pytest.fixture(params=["sqlite", "postgres"])
async def deployment(request, tmp_path):
    """Settings for an empty tenant on each database, and the tenant's slug."""
    slug = f"e2e-{uuid.uuid4().hex[:8]}"
    if request.param == "sqlite":
        database = DatabaseSettings(
            url=f"sqlite+aiosqlite:///{(tmp_path / 'e2e.db').as_posix()}", tenant_slug=slug
        )
    else:
        urls = request.getfixturevalue("pg_urls")
        database = DatabaseSettings(url=urls.app, migration_url=urls.owner, tenant_slug=slug)
    dispatch_pem, _ = keys.generate()
    grant_pem, _ = keys.generate()
    settings = Settings(
        agent=AgentSettings(
            demo_mode=True, demo_seed=False, webhook_secret=SECRET, max_concurrent_incidents=1
        ),
        llm=LLMSettings(enabled=False),
        database=database,
        # An identity provider is configured, so nobody is the built-in demo user.
        auth=AuthSettings(
            oidc_issuer="https://idp.test",
            oidc_client_id="l1",
            oidc_client_secret="not-used",
            oidc_redirect_uri=f"{BASE_URL}/auth/callback",
        ),
        servicenow=ServiceNowSettings(live_in_demo=True, verify_ssl=False, poll_interval_seconds=0),
        runner=RunnerSettings(mode="remote", dispatch_key=dispatch_pem, grant_key=grant_pem),
        worker=WorkerSettings(poll_interval_seconds=0.02),
    )
    return request.param, settings


async def test_from_an_empty_tenant_to_a_first_run_in_shadow(deployment, tmp_path, capsys):
    backend, settings = deployment
    slug = settings.database.tenant_slug
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "snow-password").write_text("pw", encoding="utf-8")
    (secrets / "splunk-token").write_text("not-read-by-a-mock", encoding="utf-8")
    prefix = "file:" + secrets.as_posix() + "/"

    snow = await FakeServiceNow(tmp_path).start()
    service: Optional[L1AgentService] = None
    people: List[Person] = []
    background: List[asyncio.Task] = []
    runner: Optional[Runner] = None
    runner_http: Optional[httpx.AsyncClient] = None
    stop_runner = asyncio.Event()
    try:
        # ── 1. Onboard the tenant ─────────────────────────────────────
        if backend == "postgres":
            args = cli._parser().parse_args(["tenant", "create", "--slug", slug, "--name", "E2E"])
            assert await cli._run(args, settings) == 0
        service = L1AgentService(settings)
        ctx = await service.setup()  # on SQLite this creates the demo tenant
        app = await service.create_app()

        async def person(*roles: str) -> Person:
            made = Person(app, *await session(ctx, list(roles)))
            people.append(made)
            return made

        admin = await person("tenant_admin")
        author = await person("sop_author")
        approver = await person("sop_approver")
        auditor = await person("auditor")

        start = (await admin.get("/api/onboarding")).json()
        assert start["complete"] is False and start["runner_mode"] == "remote"
        assert steps_of(start) == {
            "sign_in": False,
            "itsm": False,
            "runner": False,
            "connectors": True,  # Autosys has usable defaults from the environment
            "knowledge": False,
            "sop": False,
            "policy": False,
            "first_run": False,
        }
        # Nothing is taken in until ServiceNow is connected.
        from tests.api.support import PAYLOAD, signed_headers

        early = json.dumps(PAYLOAD).encode()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as anyone:
            refused = await anyone.post(
                "/webhook/incident", content=early, headers=signed_headers(early)
            )
        assert refused.status_code == 503

        mapped = await admin.send("PUT", "/api/role-mappings", {"group": "ops", "role": "operator"})
        assert mapped.status_code == 200

        # ── 2. Connect ServiceNow ─────────────────────────────────────
        connection = {
            "config": {
                "base_url": snow.url,
                "auth": "basic",
                "username": "agent",
                "assignment_group": INTAKE_GROUP,
            },
            "secrets": {"password": prefix + "snow-password"},
        }
        # A credential is a reference. A value is refused before anything is stored.
        literal = {**connection, "secrets": {"password": "pw"}}
        bad = await admin.send("PUT", "/api/connectors/servicenow/connection", literal)
        assert bad.status_code == 400
        # Only an administrator connects things.
        denied = await author.send("PUT", "/api/connectors/servicenow/connection", connection)
        assert denied.status_code == 403
        saved = await admin.send("PUT", "/api/connectors/servicenow/connection", connection)
        assert saved.status_code == 200, saved.text
        tested = (await admin.send("POST", "/api/connectors/servicenow/test")).json()
        assert tested["ok"] is True and len(tested["checks"]) == 2, tested

        # ── 3. Enrol and start the runner ─────────────────────────────
        key_file = tmp_path / "runner.key"
        assert cli._keygen(str(key_file)) == 0
        public = (tmp_path / "runner.key.pub").read_text(encoding="ascii").strip()
        assert public in capsys.readouterr().out
        assert cli._keygen(str(key_file)) == 1, "an existing key is never overwritten"
        enrolled = await admin.send(
            "POST", "/api/runners", {"name": "runner-a", "public_key": public}
        )
        assert enrolled.status_code == 201, enrolled.text
        control_plane = (await admin.get("/api/runners")).json()
        assert control_plane["mode"] == "remote" and control_plane["keys"]["grant"]

        targets = tmp_path / "runner-targets.json"
        targets.write_text(json.dumps({"autosys": {"force_start_jobs": ["NIGHTLY_*"]}}))
        mocks = {name: Counting(adapter) for name, adapter in build_mock_adapters().items()}
        runner_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
        runner = Runner(
            Settings(
                agent=AgentSettings(demo_mode=True),
                runner=RunnerSettings(
                    control_plane_url=BASE_URL,
                    identity=enrolled.json()["identity"],
                    dispatch_public_key=control_plane["keys"]["dispatch"],
                    grant_public_key=control_plane["keys"]["grant"],
                    max_tier="T1",
                    targets_file=str(targets),
                    secret_ref_prefixes=(prefix,),
                    mock_connectors=True,
                ),
            ),
            key_file.read_text(encoding="ascii"),
            http=runner_http,
            adapters=mocks,
        )

        async def work() -> None:
            while not stop_runner.is_set():
                try:
                    await runner.poll_once(wait_seconds=0.5)
                except Exception:
                    await asyncio.sleep(0.05)

        background.append(asyncio.create_task(work()))

        # ── 4. Connect Autosys, Splunk and the SOP source ─────────────
        before = {c["id"]: c for c in (await admin.get("/api/connectors")).json()["items"]}
        assert before["splunk"]["configured"] is False
        assert before["splunk"]["runs_on"] == "runner"
        assert before["servicenow"]["runs_on"] == "control_plane"
        assert before["mainframe"]["preview"] and before["browser"]["preview"]

        splunk = await admin.send(
            "PUT",
            "/api/connectors/splunk/connection",
            {
                "config": {"base_url": "https://splunk.internal.example"},
                "secrets": {"token": prefix + "splunk-token"},
            },
        )
        autosys = await admin.send(
            "PUT",
            "/api/connectors/autosys/connection",
            {"config": {"cli_path": "autorep", "force_start_jobs": ["NIGHTLY_*"]}, "secrets": {}},
        )
        assert splunk.status_code == 200 and autosys.status_code == 200, splunk.text
        for connector in ("splunk", "autosys"):
            result = (await admin.send("POST", f"/api/connectors/{connector}/test")).json()
            assert result["ok"] is True, result
        # The connection tests were made by the runner.
        assert {"health_check": True} in mocks["splunk"].calls
        assert {"health_check": True} in mocks["autosys"].calls

        repo = sop_repository(tmp_path)
        source = await admin.send(
            "PUT",
            "/api/connectors/git_knowledge/connection",
            {
                "config": {
                    "url": str(repo),
                    "ref": "main",
                    "path": "sops",
                    "cache_dir": str(tmp_path / "git-cache"),
                },
                "secrets": {},
            },
        )
        assert source.status_code == 200, source.text
        fetched = (await admin.send("POST", "/api/connectors/git_knowledge/test")).json()
        assert fetched["ok"] is True, fetched

        # ── 5. Bring the SOP in, review it, publish it ────────────────
        synced = (await author.send("POST", "/api/knowledge/sync")).json()["items"]
        assert [(s["source"], s["drafts"], s["errors"]) for s in synced] == [("git", 1, [])]
        drafts = (await author.get("/api/sops", status="draft")).json()["items"]
        assert [d["sop_id"] for d in drafts] == [RESTART_SOP]
        version = drafts[0]["id"]
        # A draft is not something the engine can run, whatever the file said.
        assert (await author.get("/api/sops", status="published")).json()["items"] == []

        submitted = await author.send("POST", f"/api/sop-versions/{version}/submit")
        assert submitted.status_code == 200, submitted.text
        seen = (await approver.get(f"/api/sop-versions/{version}")).json()
        content_hash = submitted.json()["content_hash"]
        own = await author.send(
            "POST", f"/api/sop-versions/{version}/approve", {"content_hash": content_hash}
        )
        assert own.status_code == 403, "an author cannot approve"
        approved = await approver.send(
            "POST", f"/api/sop-versions/{version}/approve", {"content_hash": content_hash}
        )
        assert approved.status_code == 200, (approved.text, seen)
        published = await approver.send("POST", f"/api/sop-versions/{version}/publish")
        assert published.status_code == 200 and published.json()["version"] == 1

        # ── 6. Set autonomy to shadow ─────────────────────────────────
        refused = await approver.send("PUT", "/api/policy", {"autonomy": "automatic"})
        assert refused.status_code == 403, "only an administrator sets how far it may go"
        policy = await admin.send("PUT", "/api/policy", {"autonomy": "shadow"})
        assert policy.status_code == 200 and policy.json()["autonomy"] == "shadow"

        ready = (await admin.get("/api/onboarding")).json()
        assert steps_of(ready) == {**{k: True for k in steps_of(start)}, "first_run": False}

        # ── 7. Run the scenario ───────────────────────────────────────
        snow.add_incident(
            {**_restart_incident(SYS_ID, NUMBER, JOB, "A rerun is usually enough."), "state": "1"}
        )
        background.append(asyncio.create_task(service._poll_loop(ctx)))
        background.extend(service.start_workers(ctx))
        record = None
        for _ in range(600):
            record = await record_by_number(ctx.db, ctx.tenant_id, NUMBER)
            if record and record.get("outcome"):
                break
            await asyncio.sleep(0.05)
        assert record and record["outcome"] == "escalated", record
        assert record["sop_id"] == RESTART_SOP and not record.get("verified_by")

        # What it looked at, through the runner.
        assert {"job_name": JOB, "query_type": "status"} in mocks["autosys"].calls
        assert any(JOB in c.get("query", "") for c in mocks["splunk"].calls)
        # What it changed: nothing. Shadow records what it would have done.
        assert mocks["autosys"].force_starts == []
        actions = (await admin.get("/api/actions")).json()["items"]
        assert [(a["capability"], a["parameters"], a["status"], a["verdict"]) for a in actions] == [
            ("autosys.job.force_start", {"job_name": JOB}, "shadowed", "shadow")
        ]
        assert actions[0]["change_number"] == "", "no change record is opened for a shadow action"

        # What ServiceNow saw: the ticket claimed, notes written, and a hand-over.
        ticket = snow.tables["incident"][SYS_ID]
        assert snow.refused == 0
        assert {"sys_id": SYS_ID, "assigned_to": AGENT_SYS_ID, "state": "2"} in snow.writes
        notes = "\n".join(snow.work_notes[SYS_ID])
        assert RESTART_SOP in notes and "shadow" in notes.lower()
        assert ticket["assignment_group"] != INTAKE_GROUP and ticket["assigned_to"] == ""
        assert ticket["state"] != "6", "an unverified run never resolves the ticket"

        # ── 8. What is on record ──────────────────────────────────────
        done = (await admin.get("/api/onboarding")).json()
        assert done["complete"] is True and done["done"] == done["total"]

        events = await audit.events_for(
            ctx.db, ctx.tenant_id, correlation_id=actions[0]["run_id"], limit=1000
        )
        story = reconstruct(events)
        assert story["incident"] == NUMBER and story["outcome"] == "escalated"
        assert story["sop"][0] == RESTART_SOP and str(story["sop"][1]) == "1"
        assert story["action"]["verdict"] == "shadow"
        assert story["action"]["parameters"] == {"job_name": JOB}
        assert "executed" not in story["action"]
        assert "runner.write_handed_over" not in story["types"]

        verified = (await auditor.get("/api/audit/verify")).json()
        assert verified["ok"] is True and verified["problems"] == []
        types = [e["event_type"] for e in await audit.events_for(ctx.db, ctx.tenant_id, limit=5000)]
        for expected in (
            "connection.updated",
            "connection.tested",
            "runner.enrolled",
            "knowledge.synced",
            "sop.approved",
            "sop.published",
            "policy.changed",
            "incident.received",
            "policy.decided",
            "run.concluded",
        ):
            assert expected in types, expected
        if backend == "postgres":
            assert types[0] == "tenant.created"
        # No credential reached the audit log or the queue: only references did.
        everything = json.dumps(await audit.events_for(ctx.db, ctx.tenant_id, limit=5000))
        assert "not-read-by-a-mock" not in everything and prefix + "splunk-token" in everything
    finally:
        stop_runner.set()
        if service is not None:
            service._stop.set()
        await asyncio.gather(*background, return_exceptions=True)
        if runner is not None:
            await runner.close()
        if runner_http is not None:
            await runner_http.aclose()
        for someone in people:
            await someone.close()
        if service is not None:
            await service.close()
        await snow.close()
