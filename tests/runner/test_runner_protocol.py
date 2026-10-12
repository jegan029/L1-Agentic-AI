"""The control plane and a runner, joined only by the protocol (ADR 0010).

Every test here runs the real application and the real runner. The control
plane's own connectors are tripwires: a call made there fails the test.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
import pytest
import sqlalchemy as sa

from src.l1_agent.actions import store as action_store
from src.l1_agent.audit.log import events_for
from src.l1_agent.connectors.sdk.base import (
    OutcomeUnknown,
    Refused,
    StepContext,
    WriteNotAuthorized,
)
from src.l1_agent.db.models import runner_calls
from src.l1_agent.runner import keys, protocol
from src.l1_agent.runner import store as runner_store
from src.l1_agent.runner.agent import Runner
from src.l1_agent.runner.ceiling import Ceiling
from src.l1_agent.runner.dispatch import RunnerUnavailable
from tests.actions_support import FAILS_AGAIN, T1_JOB, T2_JOB, make_world
from tests.api.support import audit_types, client, session
from tests.runner_support import ADMIN, RUNNER_TARGETS, Remote, make_remote

STATUS = "autosys.job.status"
FORCE_START = "autosys.job.force_start"


@pytest.fixture
async def remote(backend):
    made = await make_remote(backend)
    yield made
    await made.stop()


async def stored_result(world, incident) -> dict:
    """What the action pipeline recorded as the result: its message and whether it is unknown."""
    action = await world.action(incident)
    row = await action_store.get(world.db, world.tenant, uuid.UUID(action["id"]))
    return row.result or {}


def context(remote: Remote, step: str = "step-1") -> StepContext:
    return StepContext(remote.tenant, "run-1", step)


async def queue_size(remote: Remote) -> int:
    async with remote.db.tenant(remote.tenant) as conn:
        return (
            await conn.execute(
                sa.select(sa.func.count())
                .select_from(runner_calls)
                .where(runner_calls.c.tenant_id == remote.tenant)
            )
        ).scalar_one()


def signed(
    remote: Remote, path: str, payload: dict, *, pem: str = "", ts: int = 0, ident: str = ""
):
    body = protocol.canonical(payload).encode("ascii")
    timestamp = str(ts or int(time.time()))
    signer = keys.Signer(keys.private_seed(pem or remote.keys.runner_pem), protocol.REQUEST_DOMAIN)
    headers = {
        protocol.ID_HEADER: ident or remote.identity,
        protocol.TIMESTAMP_HEADER: timestamp,
        protocol.SIGNATURE_HEADER: signer.sign(
            protocol.request_message("POST", path, timestamp, body)
        ),
    }
    return body, headers


def claim_body() -> dict:
    return {"max": 5, "wait_seconds": 0, "nonce": uuid.uuid4().hex}


class TestAReadGoesThroughTheRunner:
    async def test_the_call_is_made_by_the_runner_and_not_in_the_control_plane(self, remote):
        remote.start()
        connectors = await remote.hub.for_tenant(remote.tenant)
        result = await connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
        assert result.success and result.data["jobs"][0]["status"] == "FA"
        assert remote.untouched(), "nothing was called in the control plane"
        assert await queue_size(remote) == 0, "a call is removed once its answer is read"
        assert connectors.tool_name(STATUS) and connectors.connector("autosys") is not None

    async def test_the_control_plane_still_refuses_a_bad_call_before_it_is_queued(self, remote):
        connectors = await remote.hub.for_tenant(remote.tenant)
        with pytest.raises(Refused):
            await connectors.invoke(STATUS, {"job_name": "A", "shell": "rm -rf /"}, context(remote))
        with pytest.raises(LookupError):
            await connectors.invoke("autosys.job.delete", {}, context(remote))
        with pytest.raises(PermissionError):
            await connectors.invoke(STATUS, {"job_name": "A"}, None)
        with pytest.raises(PermissionError, match="another tenant"):
            await connectors.invoke(
                STATUS, {"job_name": "A"}, StepContext(uuid.uuid4(), "run-1", "step-1")
            )
        with pytest.raises(WriteNotAuthorized):
            await connectors.invoke(FORCE_START, {"job_name": "NIGHTLY_LOAD"}, context(remote))
        assert await queue_size(remote) == 0

    async def test_with_no_runner_a_read_ends_as_unavailable_and_is_withdrawn(self, backend):
        remote = await make_remote(backend, wait_seconds=0.3)
        try:
            connectors = await remote.hub.for_tenant(remote.tenant)
            with pytest.raises(RunnerUnavailable, match="was not made"):
                await connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
            assert await queue_size(remote) == 0
            # A runner that turns up afterwards has nothing to do.
            assert await remote.make_runner().poll_once(wait_seconds=0) == 0
        finally:
            await remote.stop()

    async def test_a_call_whose_caller_went_away_is_withdrawn(self, remote):
        """A worker that is shutting down must not leave a call behind for a runner to make."""
        connectors = await remote.hub.for_tenant(remote.tenant)
        waiting = asyncio.create_task(
            connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
        )
        for _ in range(200):
            if await queue_size(remote):
                break
            await asyncio.sleep(0.01)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        async with remote.db.tenant(remote.tenant) as conn:
            statuses = [r.status for r in await conn.execute(sa.select(runner_calls.c.status))]
        assert statuses == ["cancelled"]
        assert await remote.make_runner().poll_once(wait_seconds=0) == 0

    async def test_a_connection_test_is_made_from_the_runner(self, remote):
        remote.start()
        checks = await remote.hub.test(remote.tenant, "splunk")
        assert [c.ok for c in checks] == [True] and remote.untouched()

    async def test_a_connection_test_says_so_when_no_runner_answers(self, backend):
        remote = await make_remote(backend, wait_seconds=0.3)
        try:
            checks = await remote.hub.test(remote.tenant, "splunk")
            assert [c.name for c in checks] == ["reach the runner"] and not checks[0].ok
        finally:
            await remote.stop()


class TestWhoMayAskForWork:
    async def test_a_request_must_be_signed_by_an_enrolled_runner(self, remote):
        async with client(remote.app) as http:
            path = protocol.CLAIM_PATH
            assert (await http.post(path, json=claim_body())).status_code == 401

            body, headers = signed(remote, path, claim_body())
            assert (await http.post(path, content=body, headers=headers)).status_code == 200

            # The same request again is a replay.
            assert (await http.post(path, content=body, headers=headers)).status_code == 401

            # Signed by a key that was never enrolled.
            body, headers = signed(remote, path, claim_body(), pem=keys.generate()[0])
            assert (await http.post(path, content=body, headers=headers)).status_code == 401

            # A body other than the one that was signed.
            body, headers = signed(remote, path, claim_body())
            other = protocol.canonical({**claim_body(), "max": 10}).encode()
            assert (await http.post(path, content=other, headers=headers)).status_code == 401

            # A signature for one route is not good for another.
            body, headers = signed(remote, protocol.result_path(uuid.uuid4()), claim_body())
            assert (await http.post(path, content=body, headers=headers)).status_code == 401

            # Too old, or from the future.
            for skew in (-protocol.MAX_SKEW_SECONDS - 5, protocol.MAX_SKEW_SECONDS + 5):
                body, headers = signed(remote, path, claim_body(), ts=int(time.time()) + skew)
                assert (await http.post(path, content=body, headers=headers)).status_code == 401

            for ident in ("", "nonsense", protocol.identity(remote.tenant, uuid.uuid4())):
                body, headers = signed(remote, path, claim_body())
                headers[protocol.ID_HEADER] = ident
                assert (await http.post(path, content=body, headers=headers)).status_code == 401

    async def test_a_runner_given_no_identity_is_known_by_its_key(self, remote):
        """What a chart install relies on: the key alone, enrolled before the runner starts."""
        runner = remote.make_runner(identity="")
        assert runner._identity == protocol.key_identity(remote.keys.runner_public)
        remote.start(runner)
        connectors = await remote.hub.for_tenant(remote.tenant)
        result = await connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
        assert result.success and remote.untouched()

        # The key in the header is only a name. Without that key's signature it opens nothing.
        stranger_pem, stranger_public = keys.generate()
        async with client(remote.app) as http:
            for pem, ident in (
                (stranger_pem, protocol.key_identity(remote.keys.runner_public)),
                (stranger_pem, protocol.key_identity(stranger_public)),
                (remote.keys.runner_pem, protocol.key_identity("not a key")),
            ):
                body, headers = signed(remote, protocol.CLAIM_PATH, claim_body(), pem=pem)
                headers[protocol.ID_HEADER] = ident
                denied = await http.post(protocol.CLAIM_PATH, content=body, headers=headers)
                assert denied.status_code == 401, ident

        await runner_store.revoke(remote.db, remote.tenant, ADMIN, remote.runner_id)
        with pytest.raises(RuntimeError, match="401"):
            await remote.make_runner(identity="").poll_once(wait_seconds=0)

    async def test_one_key_is_one_runner(self, remote):
        with pytest.raises(runner_store.RunnerError, match="already enrolled as 'runner-1'"):
            await runner_store.enrol(
                remote.db, remote.tenant, ADMIN, "runner-2", remote.keys.runner_public
            )

    async def test_a_runner_of_another_tenant_gets_nothing(self, remote, backend):
        other_tenant = await backend.new_tenant("other")
        other_id = await runner_store.enrol(
            backend.db, other_tenant, ADMIN, "runner-1", remote.keys.runner_public
        )
        async with client(remote.app) as http:
            body, headers = signed(
                remote,
                protocol.CLAIM_PATH,
                claim_body(),
                ident=protocol.identity(other_tenant, other_id),
            )
            denied = await http.post(protocol.CLAIM_PATH, content=body, headers=headers)
        assert denied.status_code == 401

    async def test_a_revoked_runner_is_refused_from_its_next_request(self, remote):
        runner = remote.make_runner()
        assert await runner.poll_once(wait_seconds=0) == 0
        assert await runner_store.revoke(remote.db, remote.tenant, ADMIN, remote.runner_id)
        with pytest.raises(RuntimeError, match="401"):
            await runner.poll_once(wait_seconds=0)
        assert await runner_store.revoke(remote.db, remote.tenant, ADMIN, remote.runner_id) is False

    async def test_browser_sessions_and_tokens_cannot_use_the_runner_routes(self, remote):
        cookies, csrf = await session(remote.ctx, ["tenant_admin"])
        async with client(remote.app) as http:
            http.cookies.update(cookies)
            denied = await http.post(
                protocol.CLAIM_PATH, json=claim_body(), headers={"X-CSRF-Token": csrf}
            )
        assert denied.status_code == 401

    async def test_a_result_is_taken_only_from_the_runner_that_holds_the_call_and_once(
        self, remote, backend
    ):
        second_pem, second_public = keys.generate()
        second = await runner_store.enrol(
            remote.db, remote.tenant, ADMIN, "runner-2", second_public
        )
        connectors = await remote.hub.for_tenant(remote.tenant)
        waiting = asyncio.create_task(
            connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
        )
        answer = {"outcome": "ok", "result": {"success": True, "data": {"status": "SU"}}}
        try:
            async with client(remote.app) as http:
                for _ in range(100):
                    body, headers = signed(remote, protocol.CLAIM_PATH, claim_body())
                    calls = (
                        await http.post(protocol.CLAIM_PATH, content=body, headers=headers)
                    ).json()["calls"]
                    if calls:
                        break
                    await asyncio.sleep(0.02)
                path = protocol.result_path(calls[0]["id"])

                # Another enrolled runner cannot answer a call it does not hold.
                body, headers = signed(
                    remote,
                    path,
                    {**answer, "nonce": "a"},
                    pem=second_pem,
                    ident=protocol.identity(remote.tenant, second),
                )
                assert (await http.post(path, content=body, headers=headers)).status_code == 409

                for junk in ({"outcome": "great", "result": {}}, {"outcome": "ok", "result": []}):
                    body, headers = signed(remote, path, {**junk, "nonce": uuid.uuid4().hex})
                    assert (await http.post(path, content=body, headers=headers)).status_code == 400

                body, headers = signed(remote, path, {**answer, "nonce": "b"})
                assert (await http.post(path, content=body, headers=headers)).status_code == 200
                body, headers = signed(remote, path, {**answer, "nonce": "c"})
                assert (await http.post(path, content=body, headers=headers)).status_code == 409
            assert (await waiting).data == {"status": "SU"}
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


class TestWhatTheRunnerChecksForItself:
    async def one_call(self, remote: Remote, **invoke) -> dict:
        """Queue one read and take it off the queue as a runner would be handed it."""
        connectors = await remote.hub.for_tenant(remote.tenant)
        task = asyncio.create_task(
            connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
        )
        try:
            async with client(remote.app) as http:
                for _ in range(100):
                    body, headers = signed(remote, protocol.CLAIM_PATH, claim_body())
                    calls = (
                        await http.post(protocol.CLAIM_PATH, content=body, headers=headers)
                    ).json()["calls"]
                    if calls:
                        return calls[0]
                    await asyncio.sleep(0.02)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise AssertionError("no call was queued")

    async def test_a_call_that_was_changed_after_signing_is_not_made(self, remote):
        item = await self.one_call(remote)
        runner = remote.make_runner()
        changed = item["envelope"].replace("NIGHTLY_LOAD", "PAYROLL_POST")
        assert changed != item["envelope"]
        outcome, result = await runner._make({**item, "envelope": changed})
        assert outcome == protocol.REFUSED and "not signed by the control plane" in result["error"]
        outcome, result = await runner._make({**item, "signature": "00" * 64})
        assert outcome == protocol.REFUSED
        for broken in ({"id": item["id"]}, {**item, "envelope": 5}, {**item, "signature": None}):
            assert (await runner._make(broken))[0] == protocol.REFUSED

    async def test_a_call_signed_by_some_other_key_is_not_made(self, remote):
        item = await self.one_call(remote)
        stranger = keys.Signer(keys.private_seed(keys.generate()[0]), protocol.CALL_DOMAIN)
        forged = {**item, "signature": stranger.sign(item["envelope"].encode())}
        outcome, _ = await remote.make_runner()._make(forged)
        assert outcome == protocol.REFUSED

    async def test_a_call_is_made_once_and_not_after_it_expires(self, remote):
        item = await self.one_call(remote)
        runner = remote.make_runner()
        assert (await runner._make(item))[0] == protocol.OK
        outcome, result = await runner._make(item)
        assert outcome == protocol.REFUSED and "already been made" in result["error"]

        late = remote.make_runner()
        late._clock = lambda: time.time() + 3600
        outcome, result = await late._make(item)
        assert outcome == protocol.REFUSED and "expired" in result["error"]

    async def test_a_call_for_another_tenant_or_under_another_id_is_not_made(self, remote):
        item = await self.one_call(remote)
        runner = remote.make_runner()
        outcome, result = await runner._make({**item, "id": str(uuid.uuid4())})
        assert outcome == protocol.REFUSED and "does not match" in result["error"]
        elsewhere = remote.make_runner(identity=protocol.identity(uuid.uuid4(), remote.runner_id))
        outcome, result = await elsewhere._make(item)
        assert outcome == protocol.REFUSED and "another tenant" in result["error"]

    async def signed_call(self, remote: Remote, **changes) -> dict:
        """A call the control plane really signed, with some of its content chosen here.

        This is what a control plane that had been taken over could send.
        """
        values = dict(
            call_id=uuid.uuid4(),
            tenant_id=remote.tenant,
            kind=protocol.KIND_INVOKE,
            connector_id="autosys",
            capability_id=STATUS,
            parameters={"job_name": "NIGHTLY_LOAD"},
            run_id="run-1",
            step_id="step-1",
            purpose="run",
            config={"force_start_jobs": ["*"]},
            secret_refs={},
            grant=None,
            issued_at=time.time(),
            expires_at=time.time() + 60,
        )
        values.update(changes)
        text = protocol.envelope(**values)
        signer = keys.Signer(keys.private_seed(remote.keys.dispatch_pem), protocol.CALL_DOMAIN)
        return {
            "id": str(values["call_id"]),
            "envelope": text,
            "signature": signer.sign(text.encode()),
        }

    async def test_a_properly_signed_write_with_no_grant_is_not_made(self, remote):
        runner = remote.make_runner()
        item = await self.signed_call(remote, capability_id=FORCE_START)
        outcome, result = await runner._make(item)
        assert outcome == protocol.NOT_AUTHORIZED and "needs a grant" in result["error"]
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_a_properly_signed_write_with_a_forged_grant_is_not_made(self, remote):
        from src.l1_agent.actions.grants import GrantAuthority
        from src.l1_agent.connectors.sdk.grant import call_digest

        params = {"job_name": "NIGHTLY_LOAD"}
        # Signed with the dispatch key, which a compromised web tier would hold.
        wrong_key = GrantAuthority(private_key=keys.private_seed(remote.keys.dispatch_pem))
        forged = wrong_key.mint(
            decision_id="d",
            tenant_id=str(remote.tenant),
            run_id="run-1",
            step_id="step-1",
            capability_id=FORCE_START,
            call_digest=call_digest(FORCE_START, params),
        )
        item = await self.signed_call(remote, capability_id=FORCE_START, grant=forged.to_dict())
        outcome, result = await remote.make_runner()._make(item)
        assert outcome == protocol.NOT_AUTHORIZED
        assert "not issued by the policy engine" in result["error"]
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_the_ceiling_is_applied_before_anything_else_about_the_target(self, remote):
        item = await self.signed_call(remote, capability_id=FORCE_START)
        for ceiling, why in (
            (Ceiling(), "goes no higher than T0"),
            (Ceiling(max_tier="T1"), "no list of its own"),
            (Ceiling(max_tier="T1", capabilities=("splunk.*",)), "not a capability"),
            (
                Ceiling(max_tier="T1", targets={"autosys": {"force_start_jobs": ("PAYROLL_*",)}}),
                "not on this runner's own list",
            ),
        ):
            item = await self.signed_call(remote, capability_id=FORCE_START)
            outcome, result = await remote.make_runner(ceiling=ceiling)._make(item)
            assert outcome == protocol.REFUSED and why in result["error"], why
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_a_reference_outside_the_runners_secret_places_is_not_followed(self, remote):
        item = await self.signed_call(
            remote,
            connector_id="splunk",
            capability_id="splunk.search",
            parameters={"query": "index=main error"},
            config={"base_url": "https://attacker.example"},
            secret_refs={"token": "env:RUNNER_PRIVATE_KEY"},
        )
        outcome, result = await remote.make_runner(adapters={})._make(item)
        assert outcome == protocol.REFUSED and "RUNNER_SECRET_REF_PREFIXES" in result["error"]

    async def test_unknown_connectors_capabilities_and_shapes_are_refused(self, remote):
        runner = remote.make_runner()
        for changes, expected in (
            (dict(connector_id="shell"), protocol.UNKNOWN),
            (dict(connector_id="servicenow"), protocol.UNKNOWN),  # not an operations connector
            (dict(capability_id="autosys.job.delete"), protocol.UNKNOWN),
            (dict(capability_id="splunk.search"), protocol.UNKNOWN),  # another connector's
            (dict(parameters={"job_name": "A", "extra": "x"}), protocol.REFUSED),
        ):
            outcome, _ = await runner._make(await self.signed_call(remote, **changes))
            assert outcome == expected, changes
        item = await self.signed_call(remote)
        body = json.loads(item["envelope"])
        for damage in ({"v": 2}, {"connection": {}}, {"context": {}}, {"parameters": []}):
            text = protocol.canonical({**body, "call_id": str(uuid.uuid4()), **damage})
            signer = keys.Signer(keys.private_seed(remote.keys.dispatch_pem), protocol.CALL_DOMAIN)
            broken = {
                "id": json.loads(text)["call_id"],
                "envelope": text,
                "signature": signer.sign(text.encode()),
            }
            assert (await runner._make(broken))[0] == protocol.REFUSED, damage

    def test_a_runner_will_not_start_half_configured(self, backend):
        from src.l1_agent.config.settings import ConfigError, RunnerSettings, Settings

        pem, public = keys.generate()
        good = dict(
            control_plane_url="https://agent.test",
            identity=protocol.identity(uuid.uuid4(), uuid.uuid4()),
            dispatch_public_key=public,
        )
        Runner(Settings(runner=RunnerSettings(**good)), pem)
        for change, why in (
            (dict(identity="runner-1"), "RUNNER_ID"),
            (dict(control_plane_url=""), "RUNNER_CONTROL_PLANE_URL"),
            (dict(control_plane_url="http://agent.test"), "https"),
            (dict(dispatch_public_key=""), "RUNNER_DISPATCH_PUBLIC_KEY"),
            (dict(dispatch_public_key="nope"), "public key"),
            (dict(grant_public_key="nope"), "public key"),
            (dict(max_tier="T9"), "RUNNER_MAX_TIER"),
        ):
            with pytest.raises(ConfigError, match=why):
                Runner(Settings(runner=RunnerSettings(**{**good, **change})), pem)
        with pytest.raises(ConfigError, match="private key"):
            Runner(Settings(runner=RunnerSettings(**good)), "not a key")


class TestWritesThroughTheRunner:
    async def world(self, remote: Remote, **kwargs):
        return await make_world(
            remote.backend,
            tenant=remote.tenant,
            connectors=remote.hub,
            grant_key=remote.keys.grant_seed,
            adapters=remote.runner_adapters,
            **kwargs,
        )

    async def test_an_automatic_restart_is_made_by_the_runner_and_verified(self, remote):
        world = await self.world(remote)
        remote.start()
        incident = await world.run(T1_JOB)
        record = await world.record(incident)
        assert record["outcome"] == "resolved", record
        assert remote.runner_adapters["autosys"].force_starts == [T1_JOB]
        assert remote.untouched()
        action = await world.action(incident)
        assert action["status"] == "verified"

        types = await audit_types(remote.ctx)
        assert "runner.enrolled" in types and "runner.write_handed_over" in types
        handed = [
            e
            for e in await events_for(remote.db, remote.tenant, correlation_id=action["run_id"])
            if e["event_type"] == "runner.write_handed_over"
        ]
        assert len(handed) == 1 and handed[0]["payload"]["capability"] == FORCE_START
        assert handed[0]["payload"]["runner_id"] == str(remote.runner_id)
        assert handed[0]["actor"]["type"] == "runner"
        # It comes after the decision to execute and before the result.
        order = [e["event_type"] for e in await world.events(incident)]
        assert (
            order.index("action.executing")
            < order.index("runner.write_handed_over")
            < order.index("action.executed")
        )

    async def test_an_approved_t2_restart_is_made_by_the_runner(self, remote):
        world = await self.world(remote, autonomy="approval")
        remote.start()
        incident = await world.run(T2_JOB)
        assert (await world.action(incident))["status"] == "pending"
        assert remote.runner_adapters["autosys"].force_starts == []
        await world.decide(incident)
        await world.work()
        assert (await world.record(incident))["outcome"] == "resolved"
        assert remote.runner_adapters["autosys"].force_starts == [T2_JOB]

    async def test_a_failed_verification_still_ends_with_a_person(self, remote):
        world = await self.world(remote)
        remote.start()
        incident = await world.run(FAILS_AGAIN)
        assert (await world.record(incident))["outcome"] == "escalated"
        assert remote.runner_adapters["autosys"].force_starts == [FAILS_AGAIN]

    async def test_a_runner_capped_at_reads_makes_no_write_whatever_policy_allows(self, remote):
        world = await self.world(remote)
        remote.start(remote.make_runner(ceiling=Ceiling(max_tier="T0", targets=RUNNER_TARGETS)))
        incident = await world.run(T1_JOB)
        record, action = await world.record(incident), await world.action(incident)
        assert record["outcome"] == "escalated" and action["status"] == "failed"
        assert "goes no higher than T0" in (await stored_result(world, incident))["message"]
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_a_runner_with_no_grant_key_makes_no_write(self, remote):
        world = await self.world(remote)
        remote.start(remote.make_runner(grant_public_key=""))
        incident = await world.run(T1_JOB)
        action = await world.action(incident)
        assert action["status"] == "failed"
        assert "no grant key" in (await stored_result(world, incident))["message"]
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_a_grant_from_another_deployments_key_is_refused_by_the_runner(self, remote):
        world = await self.world(remote)
        other = keys.generate()[1]
        remote.start(remote.make_runner(grant_public_key=other))
        incident = await world.run(T1_JOB)
        action = await world.action(incident)
        assert action["status"] == "failed"
        message = (await stored_result(world, incident))["message"]
        assert "not issued by the policy engine" in message
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_the_runners_own_kill_switch_stops_a_write(self, remote, tmp_path):
        switch = tmp_path / "stop"
        switch.write_text("")
        world = await self.world(remote)
        remote.start(
            remote.make_runner(
                ceiling=Ceiling(max_tier="T2", targets=RUNNER_TARGETS, kill_switch_file=str(switch))
            )
        )
        incident = await world.run(T1_JOB)
        action = await world.action(incident)
        assert action["status"] == "failed"
        assert "kill switch" in (await stored_result(world, incident))["message"]
        assert remote.runner_adapters["autosys"].force_starts == []

    async def test_a_write_queued_before_the_kill_switch_is_not_handed_to_the_runner(self, remote):
        """The switch is read again when the runner asks, not only when the call was queued."""
        from src.l1_agent.actions.grants import GrantAuthority
        from src.l1_agent.connectors.sdk.grant import call_digest

        params = {"job_name": T1_JOB}
        grant = GrantAuthority(private_key=remote.keys.grant_seed).mint(
            decision_id="decision-1",
            tenant_id=str(remote.tenant),
            run_id="run-1",
            step_id="step-1",
            capability_id=FORCE_START,
            call_digest=call_digest(FORCE_START, params),
        )
        connectors = await remote.hub.for_tenant(remote.tenant)
        waiting = asyncio.create_task(
            connectors.invoke(FORCE_START, params, context(remote), grant)
        )
        for _ in range(200):
            if await queue_size(remote):
                break
            await asyncio.sleep(0.01)
        await action_store.set_kill_switch(remote.db, remote.tenant, ADMIN, True, "stop")
        remote.start()
        with pytest.raises(WriteNotAuthorized, match="kill switch is on: tenant"):
            await waiting
        assert remote.runner_adapters["autosys"].force_starts == []
        assert "runner.write_handed_over" not in await audit_types(remote.ctx)

    async def test_with_no_runner_a_run_stops_at_its_first_read_and_decides_on_no_action(
        self, backend
    ):
        remote = await make_remote(backend, wait_seconds=0.3)
        try:
            world = await make_world(
                backend,
                tenant=remote.tenant,
                connectors=remote.hub,
                grant_key=remote.keys.grant_seed,
                adapters=remote.runner_adapters,
            )
            incident = await world.run(T1_JOB)
            # No runner: the SOP's first read could not be made, so the run stopped there
            # and no action was ever decided on.
            assert await world.action(incident) is None
            assert remote.runner_adapters["autosys"].force_starts == []
            assert (await world.record(incident))["outcome"] == "needs_human"
            assert await queue_size(remote) == 0
        finally:
            await remote.stop()

    async def test_a_write_whose_result_is_lost_is_read_back_and_never_sent_twice(self, backend):
        # Long enough for a busy machine to hand the write over before the wait ends.
        remote = await make_remote(backend, wait_seconds=3.0)
        try:
            world = await make_world(
                backend,
                tenant=remote.tenant,
                connectors=remote.hub,
                grant_key=remote.keys.grant_seed,
                adapters=remote.runner_adapters,
            )
            remote.start(remote.make_runner(cls=_LosesTheWriteResult))
            incident = await world.run(T1_JOB)
            action = await world.action(incident)
            # The write was made; its result never arrived; the target was read instead.
            assert remote.runner_adapters["autosys"].force_starts == [T1_JOB]
            assert action["status"] == "verified", action
            assert (await world.record(incident))["outcome"] == "resolved"
        finally:
            await remote.stop()

    async def test_a_write_taken_and_never_made_ends_as_unknown_with_a_person(self, backend):
        # Long enough for a busy machine to hand the write over before the wait ends.
        remote = await make_remote(backend, wait_seconds=3.0)
        try:
            world = await make_world(
                backend,
                tenant=remote.tenant,
                connectors=remote.hub,
                grant_key=remote.keys.grant_seed,
                adapters=remote.runner_adapters,
            )
            remote.start(remote.make_runner(cls=_TakesTheWriteAndStops))
            incident = await world.run(T1_JOB)
            action, record = await world.action(incident), await world.record(incident)
            assert remote.runner_adapters["autosys"].force_starts == []
            stored = await stored_result(world, incident)
            assert action["status"] == "failed" and stored["unknown"] is True
            assert "returned no result" in stored["message"]
            assert record["outcome"] == "needs_human"
        finally:
            await remote.stop()

    async def test_the_dispatcher_says_unknown_only_for_a_write_a_runner_took(self, backend):
        remote = await make_remote(backend, wait_seconds=2.0)
        try:
            from src.l1_agent.actions.grants import GrantAuthority
            from src.l1_agent.connectors.sdk.grant import call_digest

            params = {"job_name": T1_JOB}
            grant = GrantAuthority(private_key=remote.keys.grant_seed).mint(
                decision_id="d",
                tenant_id=str(remote.tenant),
                run_id="run-1",
                step_id="step-1",
                capability_id=FORCE_START,
                call_digest=call_digest(FORCE_START, params),
            )
            connectors = await remote.hub.for_tenant(remote.tenant)
            with pytest.raises(RunnerUnavailable, match="was not made"):
                await connectors.invoke(FORCE_START, params, context(remote), grant)

            remote.start(remote.make_runner(cls=_TakesTheWriteAndStops))
            grant = GrantAuthority(private_key=remote.keys.grant_seed).mint(
                decision_id="d",
                tenant_id=str(remote.tenant),
                run_id="run-1",
                step_id="step-2",
                capability_id=FORCE_START,
                call_digest=call_digest(FORCE_START, params),
            )
            with pytest.raises(OutcomeUnknown):
                await connectors.invoke(FORCE_START, params, context(remote, "step-2"), grant)
        finally:
            await remote.stop()


class _LosesTheWriteResult(Runner):
    """Makes every call, and loses the answer to a write on the way back."""

    async def _handle(self, item):
        if '"capability_id":"autosys.job.force_start"' in item["envelope"]:
            await self._make(item)
            return
        await super()._handle(item)


class _TakesTheWriteAndStops(Runner):
    """Claims a write and does nothing with it, as a runner that died would."""

    async def _handle(self, item):
        if '"capability_id":"autosys.job.force_start"' in item["envelope"]:
            return
        await super()._handle(item)


class TestEnrolment:
    async def test_enrolling_revoking_and_listing_over_http(self, remote):
        admin, csrf = await session(remote.ctx, ["tenant_admin"])
        viewer, viewer_csrf = await session(remote.ctx, ["viewer"])
        pem, public = keys.generate()
        async with client(remote.app) as http:
            http.cookies.update(viewer)
            listing = await http.get("/api/runners")
            assert listing.status_code == 403
            refused = await http.post(
                "/api/runners",
                json={"name": "r2", "public_key": public},
                headers={"X-CSRF-Token": viewer_csrf},
            )
            assert refused.status_code == 403

        async with client(remote.app) as http:
            http.cookies.update(admin)
            headers = {"X-CSRF-Token": csrf}
            made = await http.post(
                "/api/runners", json={"name": "r2", "public_key": public}, headers=headers
            )
            assert made.status_code == 201, made.text
            identity = made.json()["identity"]
            assert protocol.parse_identity(identity)[0] == remote.tenant

            again = await http.post(
                "/api/runners", json={"name": "r2", "public_key": public}, headers=headers
            )
            assert again.status_code == 400 and "already enrolled" in again.json()["error"]
            bad = await http.post(
                "/api/runners", json={"name": "r3", "public_key": "A" * 44}, headers=headers
            )
            assert bad.status_code == 400

            listing = (await http.get("/api/runners")).json()
            assert listing["mode"] == "remote"
            assert listing["keys"] == {
                "dispatch": remote.keys.dispatch_public,
                "grant": remote.keys.grant_public,
            }
            names = {r["name"]: r for r in listing["items"]}
            assert set(names) == {"runner-1", "r2"}
            assert names["r2"]["connected"] is False and "public_key" not in names["r2"]
            assert pem not in json.dumps(listing) and public not in json.dumps(listing["items"])

            gone = await http.delete(f"/api/runners/{made.json()['id']}", headers=headers)
            assert gone.status_code == 200
            assert (
                await http.delete(f"/api/runners/{made.json()['id']}", headers=headers)
            ).status_code == 404
        types = await audit_types(remote.ctx)
        assert types.count("runner.enrolled") == 2 and types.count("runner.revoked") == 1

    async def test_a_working_runner_shows_as_connected_with_what_it_reports(self, remote):
        runner = remote.make_runner()
        await runner.poll_once(wait_seconds=0)
        listed = {r["name"]: r for r in await runner_store.list_runners(remote.db, remote.tenant)}
        mine = listed["runner-1"]
        assert mine["connected"] is True and mine["reported"]["max_tier"] == "T2"
        assert mine["reported"]["writes"] is True and mine["reported"]["mock"] is True
        assert await runner_store.connected_count(remote.db, remote.tenant) == 1

    async def test_a_runner_name_and_key_are_checked(self, remote):
        public = keys.generate()[1]
        for name in ("", " ", "n" * 101):
            with pytest.raises(runner_store.RunnerError, match="name"):
                await runner_store.enrol(remote.db, remote.tenant, ADMIN, name, public)
        with pytest.raises(runner_store.RunnerError, match="public key"):
            await runner_store.enrol(remote.db, remote.tenant, ADMIN, "ok", "not a key")


class TestDatabaseRules:
    async def test_a_signed_call_cannot_be_rewritten_in_the_queue(self, pg):
        """PostgreSQL only: a trigger keeps what was signed as it was signed."""
        remote = await make_remote(pg)
        try:
            connectors = await remote.hub.for_tenant(remote.tenant)
            waiting = asyncio.create_task(
                connectors.invoke(STATUS, {"job_name": "NIGHTLY_LOAD"}, context(remote))
            )
            for _ in range(200):
                if await queue_size(remote):
                    break
                await asyncio.sleep(0.01)
            for column, value in (
                ("envelope", "{}"),
                ("signature", "00"),
                ("capability_id", FORCE_START),
                ("is_write", True),
            ):
                with pytest.raises(Exception, match="cannot be changed after it was signed"):
                    async with remote.db.tenant(remote.tenant) as conn:
                        await conn.execute(sa.update(runner_calls).values({column: value}))
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        finally:
            await remote.stop()

    async def test_the_application_role_cannot_delete_a_runner(self, pg):
        remote = await make_remote(pg)
        try:
            from src.l1_agent.db.models import runners

            with pytest.raises(Exception, match="permission denied"):
                async with remote.db.tenant(remote.tenant) as conn:
                    await conn.execute(sa.delete(runners))
        finally:
            await remote.stop()


async def test_http_errors_from_the_control_plane_do_not_stop_the_runner_loop(remote):
    """A runner that cannot reach the control plane backs off and keeps trying."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    broken = remote.make_runner()
    broken._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x")
    stop = asyncio.Event()
    task = asyncio.create_task(broken.run(stop))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    await broken._http.aclose()
