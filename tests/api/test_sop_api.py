"""SOP authoring, review and connector routes over HTTP, with real sessions and roles."""

from __future__ import annotations

import copy
import json

from src.l1_agent.connectors.catalog import connectors_for_adapters
from src.l1_agent.connectors.hub import ConnectorHub
from src.l1_agent.connectors.sdk.secrets import SecretResolver
from src.l1_agent.demo import build_mock_adapters, sample_sop_contents, seed_demo_sops
from src.l1_agent.sop import store
from tests.api.support import audit_types, build, client, session, token_headers

MQ = next(c for c in sample_sop_contents() if c["sop_id"] == "SOP-MQ-001")


async def signed_in(ctx, http_roles):
    cookies, csrf = await session(ctx, http_roles)
    return cookies, {"X-CSRF-Token": csrf}


class TestReviewFlow:
    async def test_author_submits_a_different_person_approves_and_publishes(self, backend):
        app, ctx = await build(backend)
        author, author_h = await signed_in(ctx, ["sop_author"])
        approver, approver_h = await signed_in(ctx, ["sop_approver"])
        async with client(app) as http:
            created = await http.post(
                "/api/sops", json={"content": MQ}, cookies=author, headers=author_h
            )
            version_id = created.json()["id"]
            url = f"/api/sop-versions/{version_id}"
            submitted = await http.post(f"{url}/submit", cookies=author, headers=author_h)
            digest = submitted.json()["content_hash"]

            review = (await http.get(url, cookies=approver)).json()
            approved = await http.post(
                f"{url}/approve",
                json={"content_hash": digest},
                cookies=approver,
                headers=approver_h,
            )
            published = await http.post(f"{url}/publish", cookies=approver, headers=approver_h)
            listing = (await http.get("/api/sops?status=published", cookies=approver)).json()

        assert created.status_code == 201 and submitted.status_code == 200
        assert review["status"] == "in_review" and review["problems"] == []
        assert review["content_hash"] == review["current_hash"] == digest
        assert {c["tier"] for c in review["capabilities"]} == {"T0"}
        assert approved.status_code == 200
        assert published.json() == {"status": "published", "version": 1}
        assert [v["sop_id"] for v in listing["items"]] == ["SOP-MQ-001"]
        assert [s.sop_id for s in await store.published(ctx.db, ctx.tenant_id)] == ["SOP-MQ-001"]
        types = await audit_types(ctx)
        for expected in ("sop.draft_created", "sop.submitted", "sop.approved", "sop.published"):
            assert expected in types

    async def test_one_person_with_both_roles_still_cannot_approve_their_own_version(self, backend):
        """Exit criterion 4 at the API: holding the approver role is not enough."""
        app, ctx = await build(backend)
        both, h = await signed_in(ctx, ["sop_author", "sop_approver"])
        other, other_h = await signed_in(ctx, ["sop_approver"])
        async with client(app) as http:
            version_id = (
                await http.post("/api/sops", json={"content": MQ}, cookies=both, headers=h)
            ).json()["id"]
            url = f"/api/sop-versions/{version_id}"
            digest = (await http.post(f"{url}/submit", cookies=both, headers=h)).json()[
                "content_hash"
            ]
            own = await http.post(
                f"{url}/approve", json={"content_hash": digest}, cookies=both, headers=h
            )
            still = (await http.get(url, cookies=both)).json()["status"]
            by_other = await http.post(
                f"{url}/approve", json={"content_hash": digest}, cookies=other, headers=other_h
            )
        assert own.status_code == 403
        assert own.json()["error"] == "The author of a version cannot approve it"
        assert still == "in_review" and by_other.status_code == 200

    async def test_each_role_can_do_only_its_own_part(self, backend):
        app, ctx = await build(backend)
        author, author_h = await signed_in(ctx, ["sop_author"])
        approver, approver_h = await signed_in(ctx, ["sop_approver"])
        admin, admin_h = await signed_in(ctx, ["tenant_admin"])
        viewer, viewer_h = await signed_in(ctx, ["viewer"])
        async with client(app) as http:
            version_id = (
                await http.post("/api/sops", json={"content": MQ}, cookies=author, headers=author_h)
            ).json()["id"]
            url = f"/api/sop-versions/{version_id}"
            digest = (await http.post(f"{url}/submit", cookies=author, headers=author_h)).json()[
                "content_hash"
            ]
            body = {"content_hash": digest}
            refused = [
                await http.post(
                    "/api/sops", json={"content": MQ}, cookies=approver, headers=approver_h
                ),
                await http.post(
                    "/api/sops", json={"content": MQ}, cookies=viewer, headers=viewer_h
                ),
                await http.post(f"{url}/approve", json=body, cookies=author, headers=author_h),
                await http.post(f"{url}/approve", json=body, cookies=admin, headers=admin_h),
                await http.post(f"{url}/approve", json=body, cookies=viewer, headers=viewer_h),
                await http.post(f"{url}/publish", cookies=author, headers=author_h),
                await http.post("/api/sops/SOP-MQ-001/retire", cookies=author, headers=author_h),
                await http.post("/api/knowledge/sync", cookies=viewer, headers=viewer_h),
            ]
            no_csrf = await http.post(f"{url}/approve", json=body, cookies=approver)
            readable = await http.get(url, cookies=viewer)
        assert [r.status_code for r in refused] == [403] * len(refused)
        assert no_csrf.status_code == 403
        assert readable.status_code == 200

    async def test_a_machine_token_cannot_author_or_approve(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(ctx, "incidents.read")
        async with client(app) as http:
            listed = await http.get("/api/sops", headers=headers)
            write = await http.post("/api/sops", json={"content": MQ}, headers=headers)
            minted = await http.post(
                "/api/tokens",
                json={"name": "x", "permissions": ["sops.approve"]},
                headers=headers,
            )
        assert listed.status_code == 200 and write.status_code == 403
        assert minted.status_code == 403

    async def test_problems_come_back_to_the_author_and_states_are_enforced(self, backend):
        app, ctx = await build(backend)
        author, h = await signed_in(ctx, ["sop_author"])
        approver, approver_h = await signed_in(ctx, ["sop_approver"])
        broken = copy.deepcopy(MQ)
        broken["steps"][1]["parameters"]["purge"] = True
        async with client(app) as http:
            not_an_sop = await http.post(
                "/api/sops", json={"content": {"title": "no id"}}, cookies=author, headers=h
            )
            version_id = (
                await http.post("/api/sops", json={"content": broken}, cookies=author, headers=h)
            ).json()["id"]
            url = f"/api/sop-versions/{version_id}"
            invalid = await http.post(f"{url}/submit", cookies=author, headers=h)
            fixed = await http.put(url, json={"content": MQ}, cookies=author, headers=h)
            digest = (await http.post(f"{url}/submit", cookies=author, headers=h)).json()[
                "content_hash"
            ]
            late_edit = await http.put(url, json={"content": MQ}, cookies=author, headers=h)
            stale = await http.post(
                f"{url}/approve",
                json={"content_hash": "0" * 64},
                cookies=approver,
                headers=approver_h,
            )
            early = await http.post(f"{url}/publish", cookies=approver, headers=approver_h)
            back = await http.post(
                f"{url}/request-changes",
                json={"note": "Explain step 4"},
                cookies=approver,
                headers=approver_h,
            )
            discarded = await http.delete(url, cookies=author, headers=h)
            gone = await http.get(url, cookies=author)
            missing = await http.post(
                "/api/sops/SOP-NONE/retire", cookies=approver, headers=approver_h
            )
        assert not_an_sop.status_code == 400
        assert invalid.status_code == 400
        assert any("'purge' is not allowed" in p for p in invalid.json()["problems"])
        assert fixed.status_code == 200 and len(digest) == 64
        assert late_edit.status_code == 409 and stale.status_code == 409
        assert early.status_code == 409 and back.status_code == 200
        assert discarded.status_code == 200 and gone.status_code == 404
        assert missing.status_code == 404

    async def test_demo_tenant_starts_with_published_sops_and_one_to_review(self, backend):
        app, ctx = await build(backend)
        await seed_demo_sops(ctx.db, ctx.tenant_id)
        await seed_demo_sops(ctx.db, ctx.tenant_id)  # starting twice adds nothing
        approver, h = await signed_in(ctx, ["sop_approver"])
        async with client(app) as http:
            items = (await http.get("/api/sops", cookies=approver)).json()["items"]
            waiting = [v for v in items if v["status"] == "in_review"]
            review = (
                await http.get(f"/api/sop-versions/{waiting[0]['id']}", cookies=approver)
            ).json()
            approved = await http.post(
                f"/api/sop-versions/{waiting[0]['id']}/approve",
                json={"content_hash": review["content_hash"]},
                cookies=approver,
                headers=h,
            )
        assert sorted(v["status"] for v in items) == ["in_review"] + ["published"] * 3
        assert waiting[0]["sop_id"] == "SOP-LOG-001" and review["problems"] == []
        assert [c["capability"] for c in review["capabilities"]] == ["splunk.search"] * 2
        assert review["diff"]["first_version"] is True
        assert approved.status_code == 200, "written by the sample library, approved by a person"


class TestConnectorsApi:
    async def test_the_catalogue_lists_every_connector_and_no_credential(self, backend):
        app, ctx = await build(backend)
        admin, h = await signed_in(ctx, ["tenant_admin"])
        async with client(app) as http:
            saved = await http.put(
                "/api/connectors/splunk/connection",
                json={
                    "config": {"base_url": "https://splunk.example"},
                    "secrets": {"token": "vault:splunk/token"},
                },
                cookies=admin,
                headers=h,
            )
            items = (await http.get("/api/connectors", cookies=admin)).json()["items"]
        by_id = {c["id"]: c for c in items}
        assert saved.status_code == 200
        assert {
            "servicenow",
            "servicenow_kb",
            "git_knowledge",
            "splunk",
            "dynatrace",
            "ibm_mq",
            "autosys",
            "fileshare",
            "browser",
            "mainframe",
        } == set(by_id)
        assert by_id["splunk"]["configured"] and by_id["splunk"]["origin"] == "connection"
        assert by_id["splunk"]["credentials"] == {"token": "vault:splunk/token"}
        assert by_id["browser"]["preview"] and by_id["mainframe"]["preview"]
        assert by_id["splunk"]["capabilities"][0]["parameters"]["additionalProperties"] is False
        assert "connection.updated" in await audit_types(ctx)

    async def test_only_an_administrator_changes_connections_and_values_are_refused(self, backend):
        app, ctx = await build(backend)
        admin, h = await signed_in(ctx, ["tenant_admin"])
        operator, operator_h = await signed_in(ctx, ["operator"])
        body = {"config": {"base_url": "https://splunk.example"}, "secrets": {}}
        async with client(app) as http:
            by_operator = await http.put(
                "/api/connectors/splunk/connection", json=body, cookies=operator, headers=operator_h
            )
            raw_secret = await http.put(
                "/api/connectors/splunk/connection",
                json={**body, "secrets": {"token": "eyJ-real-token"}},
                cookies=admin,
                headers=h,
            )
            unknown = await http.put(
                "/api/connectors/teleport/connection", json=body, cookies=admin, headers=h
            )
            test_by_operator = await http.post(
                "/api/connectors/splunk/test", cookies=operator, headers=operator_h
            )
            absent = await http.delete(
                "/api/connectors/splunk/connection", cookies=admin, headers=h
            )
        assert by_operator.status_code == 403 and test_by_operator.status_code == 403
        assert raw_secret.status_code == 400 and "eyJ-real-token" not in raw_secret.text
        assert unknown.status_code == 400 and absent.status_code == 404

    async def test_connection_test_reports_each_check_and_is_audited(self, backend):
        app, ctx = await build(backend)
        ctx.hub = ConnectorHub(
            ctx.db,
            ctx.settings,
            SecretResolver({}),
            fixed=connectors_for_adapters(build_mock_adapters()),
        )
        admin, h = await signed_in(ctx, ["tenant_admin"])
        async with client(app) as http:
            ok = await http.post("/api/connectors/splunk/test", cookies=admin, headers=h)
            unknown = await http.post("/api/connectors/teleport/test", cookies=admin, headers=h)
            demo = (await http.get("/api/connectors", cookies=admin)).json()["items"]
        assert ok.json() == {
            "ok": True,
            "checks": [{"name": "reach the target", "ok": True, "detail": ""}],
        }
        assert unknown.status_code == 404
        assert {c["id"]: c["origin"] for c in demo}["splunk"] == "demo"
        assert "connection.tested" in await audit_types(ctx)

    async def test_knowledge_sync_with_no_source_does_nothing(self, backend):
        app, ctx = await build(backend)
        author, h = await signed_in(ctx, ["sop_author"])
        async with client(app) as http:
            resp = await http.post("/api/knowledge/sync", cookies=author, headers=h)
        assert resp.status_code == 200 and resp.json() == {"items": []}
        assert json.dumps(resp.json())
