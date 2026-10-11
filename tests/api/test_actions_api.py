"""Approvals, policy and the kill switch over HTTP: who may do what (ADR 0008)."""

from __future__ import annotations

from typing import List, Tuple

from src.l1_agent.api.app import AppContext, create_app
from src.l1_agent.auth import store
from src.l1_agent.connectors.hub import ConnectorHub
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from tests.actions_support import T1_JOB, T2_JOB, World, make_world
from tests.api.support import ISSUER, audit_types, client, make_settings

ZERO = "00000000-0000-0000-0000-000000000000"


async def application(backend, **world_options) -> Tuple[object, AppContext, World]:
    world = await make_world(backend, **world_options)
    settings = make_settings()
    ctx = AppContext(
        settings=settings,
        hub=ConnectorHub(backend.db, settings, SecretResolver({}), refresh_seconds=0),
        db=backend.db,
        runs=world.service,
        tenant_id=world.tenant,
        actions=world.actions,
    )
    return create_app(ctx), ctx, world


async def sign_in(ctx: AppContext, roles: List[str], subject: str) -> dict:
    """Cookie and CSRF header for a user with a known identity."""
    async with ctx.db.tenant(ctx.tenant_id) as conn:
        user_id = await store.upsert_user(conn, ctx.tenant_id, ISSUER, subject)
        cookie, _ = await store.create_session(conn, ctx.tenant_id, user_id, roles, "oidc", hours=1)
    principal = await store.load_session(ctx.db, cookie, idle_minutes=30)
    return {
        "cookies": {"l1_session": cookie},
        "headers": {"X-CSRF-Token": principal.csrf_token},
    }


def session_cookie(user: dict) -> dict:
    from src.l1_agent.api.security import SESSION_COOKIE

    return {SESSION_COOKIE: next(iter(user["cookies"].values()))}


async def post(http, user: dict, path: str, body: dict):
    return await http.post(path, json=body, cookies=session_cookie(user), headers=user["headers"])


async def get(http, user: dict, path: str):
    return await http.get(path, cookies=session_cookie(user))


class TestApprovalOverHttp:
    async def test_an_approver_sees_exactly_what_is_asked_and_approves_it(self, backend):
        app, ctx, world = await application(backend, autonomy="approval")
        incident = await world.run()
        approver = await sign_in(ctx, ["approver"], "approver-9")
        async with client(app) as http:
            waiting = (await get(http, approver, "/api/actions?status=pending")).json()["items"]
            shown = (await get(http, approver, f"/api/actions/{waiting[0]['id']}")).json()
            done = await post(
                http,
                approver,
                f"/api/actions/{shown['id']}/approve",
                {"action_hash": shown["action_hash"], "note": "go ahead"},
            )
        assert len(waiting) == 1 and shown["incident_number"] == incident.number
        assert shown["capability"] == "autosys.job.force_start"
        assert shown["parameters"] == {"job_name": T1_JOB} and shown["tier"] == "T1"
        assert shown["sop_hash"] == world.sop.content_hash and shown["expires_at"]
        assert shown["change_number"] and len(shown["action_hash"]) == 64
        assert done.status_code == 200 and done.json()["status"] == "approved"
        assert done.json()["approver"] == f"user:{ISSUER}|approver-9"

        await world.work()
        assert (await world.record(incident))["outcome"] == "resolved"
        assert world.autosys.force_starts == [T1_JOB]
        assert "approval.decided" in await audit_types(ctx)

    async def test_only_the_approver_role_can_decide(self, backend):
        app, ctx, world = await application(backend, autonomy="approval")
        incident = await world.run()
        action = await world.action(incident)
        body = {"action_hash": action["action_hash"]}
        async with client(app) as http:
            for roles in (
                ["viewer"],
                ["operator"],
                ["tenant_admin"],
                ["sop_approver"],
                ["auditor"],
            ):
                user = await sign_in(ctx, roles, f"u-{roles[0]}")
                for verb in ("approve", "reject"):
                    resp = await post(http, user, f"/api/actions/{action['id']}/{verb}", body)
                    assert resp.status_code == 403, f"{roles} {verb}"
                assert (await get(http, user, "/api/actions")).status_code == 200
            anonymous = await http.post(f"/api/actions/{action['id']}/approve", json=body)
            token = await store.create_api_token(
                ctx.db, ctx.tenant_id, "t", ["incidents.read"], 1, world_admin()
            )
            machine = await http.post(
                f"/api/actions/{action['id']}/approve",
                json=body,
                headers={"Authorization": f"Bearer {token[0]}"},
            )
        assert anonymous.status_code == 401 and machine.status_code == 403
        assert (await world.action(incident))["status"] == "pending"
        assert world.autosys.force_starts == []

    async def test_an_approval_needs_the_csrf_header(self, backend):
        app, ctx, world = await application(backend, autonomy="approval")
        incident = await world.run()
        action = await world.action(incident)
        approver = await sign_in(ctx, ["approver"], "approver-9")
        async with client(app) as http:
            resp = await http.post(
                f"/api/actions/{action['id']}/approve",
                json={"action_hash": action["action_hash"]},
                cookies=session_cookie(approver),
            )
        assert resp.status_code == 403
        assert (await world.action(incident))["status"] == "pending"

    async def test_the_sop_author_cannot_approve_a_t2_action_even_with_the_role(self, backend):
        app, ctx, world = await application(backend)
        incident = await world.run(T2_JOB)
        action = await world.action(incident)
        body = {"action_hash": action["action_hash"]}
        author = await sign_in(ctx, ["approver", "sop_author"], "author-1")
        other = await sign_in(ctx, ["approver"], "approver-9")
        async with client(app) as http:
            refused = await post(http, author, f"/api/actions/{action['id']}/approve", body)
            still = (await get(http, author, f"/api/actions/{action['id']}")).json()
            accepted = await post(http, other, f"/api/actions/{action['id']}/approve", body)
        assert refused.status_code == 403 and "author of the SOP" in refused.json()["error"]
        assert still["status"] == "pending"
        assert accepted.status_code == 200

    async def test_a_stale_or_wrong_hash_a_second_decision_and_an_unknown_id(self, backend):
        app, ctx, world = await application(backend, autonomy="approval")
        incident = await world.run()
        action = await world.action(incident)
        approver = await sign_in(ctx, ["approver"], "approver-9")
        path = f"/api/actions/{action['id']}"
        async with client(app) as http:
            wrong = await post(http, approver, f"{path}/approve", {"action_hash": "0" * 64})
            short = await post(http, approver, f"{path}/approve", {"action_hash": "abc"})
            missing = await post(http, approver, f"{path}/approve", {})
            unknown = await post(
                http, approver, f"/api/actions/{ZERO}/approve", {"action_hash": "0" * 64}
            )
            rejected = await post(
                http, approver, f"{path}/reject", {"action_hash": action["action_hash"]}
            )
            again = await post(
                http, approver, f"{path}/approve", {"action_hash": action["action_hash"]}
            )
        assert [wrong.status_code, short.status_code, missing.status_code] == [409, 422, 422]
        assert unknown.status_code == 404
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        assert again.status_code == 409

    async def test_another_tenants_approver_cannot_decide(self, backend):
        app, ctx, world = await application(backend, autonomy="approval")
        incident = await world.run()
        action = await world.action(incident)
        other_tenant = await backend.new_tenant("other")
        async with ctx.db.tenant(other_tenant) as conn:
            user_id = await store.upsert_user(conn, other_tenant, ISSUER, "outsider")
            cookie, _ = await store.create_session(
                conn, other_tenant, user_id, ["approver"], "oidc", hours=1
            )
        principal = await store.load_session(ctx.db, cookie, idle_minutes=30)
        outsider = {
            "cookies": {"l1_session": cookie},
            "headers": {"X-CSRF-Token": principal.csrf_token},
        }
        async with client(app) as http:
            resp = await post(
                http,
                outsider,
                f"/api/actions/{action['id']}/approve",
                {"action_hash": action["action_hash"]},
            )
            listing = await get(http, outsider, "/api/actions")
        assert resp.status_code == 404
        assert listing.json()["items"] == []
        assert (await world.action(incident))["status"] == "pending"


def world_admin():
    from tests.actions_support import ADMIN

    return ADMIN


class TestPolicyOverHttp:
    async def test_the_admin_sets_the_policy_and_others_can_only_read_it(self, backend):
        app, ctx, world = await application(backend, autonomy="shadow")
        admin = await sign_in(ctx, ["tenant_admin"], "admin-9")
        operator = await sign_in(ctx, ["operator"], "operator-9")
        approver = await sign_in(ctx, ["approver"], "approver-9")
        async with client(app) as http:
            before = (await get(http, admin, "/api/policy")).json()
            changed = await http.put(
                "/api/policy",
                json={"autonomy": "approval"},
                cookies=session_cookie(admin),
                headers=admin["headers"],
            )
            by_operator = await http.put(
                "/api/policy",
                json={"autonomy": "automatic"},
                cookies=session_cookie(operator),
                headers=operator["headers"],
            )
            by_approver = await http.put(
                "/api/policy",
                json={"autonomy": "automatic"},
                cookies=session_cookie(approver),
                headers=approver["headers"],
            )
            seen_by_operator = await get(http, operator, "/api/policy")
            seen_by_approver = await get(http, approver, "/api/policy")
        assert before["autonomy"] == "shadow" and before["kill_switch"]["on"] is False
        assert before["ceiling"] == {"max_tier": "T2", "max_autonomy": "automatic"}
        assert before["global_kill_switch"]["on"] is False and before["local_kill_switch"] is False
        assert changed.status_code == 200 and changed.json()["autonomy"] == "approval"
        assert changed.json()["rules"] == before["rules"], "the rules were not touched"
        assert by_operator.status_code == 403 and by_approver.status_code == 403
        assert seen_by_operator.status_code == 200 and seen_by_approver.status_code == 403
        assert "policy.changed" in await audit_types(ctx)

    async def test_invalid_rules_are_refused_with_every_problem(self, backend):
        app, ctx, world = await application(backend)
        admin = await sign_in(ctx, ["tenant_admin"], "admin-9")
        async with client(app) as http:
            resp = await http.put(
                "/api/policy",
                json={"rules": {"surprise": 1, "rate": {"max_actions": -5}}},
                cookies=session_cookie(admin),
                headers=admin["headers"],
            )
            bad_level = await http.put(
                "/api/policy",
                json={"autonomy": "yolo"},
                cookies=session_cookie(admin),
                headers=admin["headers"],
            )
            after = (await get(http, admin, "/api/policy")).json()
        assert resp.status_code == 400 and len(resp.json()["problems"]) == 2
        assert bad_level.status_code == 400
        assert after["autonomy"] == "automatic" and "surprise" not in after["rules"]

    async def test_an_operator_can_stop_the_agent_and_only_an_admin_can_start_it_again(
        self, backend
    ):
        app, ctx, world = await application(backend)
        operator = await sign_in(ctx, ["operator"], "operator-9")
        admin = await sign_in(ctx, ["tenant_admin"], "admin-9")
        approver = await sign_in(ctx, ["approver"], "approver-9")
        async with client(app) as http:
            by_approver = await post(http, approver, "/api/kill-switch/engage", {"reason": "x"})
            engaged = await post(
                http, operator, "/api/kill-switch/engage", {"reason": "runaway restarts"}
            )
            stopped = await world.run()
            shown = (await get(http, operator, "/api/policy")).json()
            operator_release = await post(http, operator, "/api/kill-switch/release", {})
            released = await post(http, admin, "/api/kill-switch/release", {"reason": "fixed"})
            resumed = await world.run("NIGHTLY_OTHER_LOAD")
        assert by_approver.status_code == 403
        assert engaged.status_code == 200 and engaged.json() == {"kill_switch": True}
        assert (await world.record(stopped))["outcome"] == "aborted"
        assert shown["kill_switch"]["on"] is True
        assert shown["kill_switch"]["reason"] == "runaway restarts"
        assert shown["kill_switch"]["set_by"] == f"user:{ISSUER}|operator-9"
        assert operator_release.status_code == 403
        assert released.status_code == 200
        assert (await world.record(resumed))["outcome"] == "resolved"
        assert world.autosys.force_starts == ["NIGHTLY_OTHER_LOAD"]
        types = await audit_types(ctx)
        assert "killswitch.engaged" in types and "killswitch.released" in types
