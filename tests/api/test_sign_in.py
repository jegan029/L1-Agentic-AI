"""Sign-in with OpenID Connect, sessions, CSRF, API tokens, role mappings and break-glass.

The identity provider is a fake one in this process. Behaviour against a real
provider (group claim formats, key rotation timing) is not verified here.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric import rsa

from src.l1_agent.api.security import CSRF_HEADER, SESSION_COOKIE
from src.l1_agent.audit.log import events_for
from src.l1_agent.auth import breakglass, store
from src.l1_agent.auth.oidc import ALLOWED_ALGORITHMS, OidcClient, code_challenge
from src.l1_agent.auth.permissions import Permission
from src.l1_agent.config.settings import AuthSettings, ConfigError
from src.l1_agent.db.engine import utcnow
from src.l1_agent.db.models import api_tokens, login_states, sessions, users
from tests.api.support import (
    ADMIN,
    BASE_URL,
    CLIENT_ID,
    FakeIdp,
    audit_types,
    build,
    client,
    session,
    start_login,
    token_headers,
)


async def oidc_app(backend, *, groups=(("ops", "operator"),), **auth):
    idp = FakeIdp()
    app, ctx = await build(backend, oidc=idp.client(**auth))
    for group, role in groups:
        await store.set_role_mapping(ctx.db, ctx.tenant_id, group, role, ADMIN, grant=True)
    return idp, app, ctx


async def sign_in(http, idp: FakeIdp, **claims):
    """Run the whole flow. Returns the callback response."""
    sent = await start_login(http)
    merged = {**idp.standard_claims(sent["nonce"]), **claims}
    idp.id_token = idp.sign({k: v for k, v in merged.items() if v is not None})
    return await http.get("/auth/callback", params={"code": "the-code", "state": sent["state"]})


class TestOidcSignIn:
    async def test_full_flow_creates_a_session_with_mapped_roles(self, backend):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            sent = await start_login(http)
            idp.id_token = idp.sign(idp.standard_claims(sent["nonce"]))
            callback = await http.get(
                "/auth/callback", params={"code": "c1", "state": sent["state"]}
            )
            me = await http.get("/api/me")
            incidents = await http.get("/api/incidents")
            tokens = await http.get("/api/tokens")

        assert sent["response_type"] == "code" and sent["client_id"] == CLIENT_ID
        assert sent["code_challenge_method"] == "S256"
        assert sent["redirect_uri"] == f"{BASE_URL}/auth/callback"
        # The provider received the verifier that matches the challenge (PKCE).
        assert code_challenge(idp.token_requests[0]["code_verifier"]) == sent["code_challenge"]
        assert idp.token_requests[0]["code"] == "c1"

        assert callback.status_code == 303
        cookie = callback.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert me.json()["roles"] == ["operator"] and me.json()["kind"] == "user"
        assert me.json()["id"] == "https://idp.test|alice"
        assert incidents.status_code == 200
        assert tokens.status_code == 403, "an operator does not manage tokens"
        assert "auth.login" in await audit_types(ctx)

    async def test_only_a_hash_of_the_session_is_stored(self, backend):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            await sign_in(http, idp)
            value = http.cookies[SESSION_COOKIE]
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            row = (await conn.execute(sa.select(sessions))).one()
        assert value not in str(tuple(row))
        assert len(row.token_hash) == 64

    async def test_user_with_no_mapped_group_is_refused(self, backend):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            callback = await sign_in(http, idp, groups=["finance"])
            me = await http.get("/api/me")
        assert callback.status_code == 401 and "set-cookie" not in callback.headers
        assert me.status_code == 401
        failed = [
            e
            for e in await events_for(ctx.db, ctx.tenant_id)
            if e["event_type"] == "auth.login_failed"
        ]
        assert "no role is mapped" in failed[0]["payload"]["reason"]

    async def test_roles_follow_the_group_mapping_and_unknown_roles_are_dropped(self, backend):
        idp, app, ctx = await oidc_app(
            backend, groups=(("ops", "operator"), ("audit", "auditor"), ("other", "viewer"))
        )
        async with client(app) as http:
            await sign_in(http, idp, groups=["ops", "audit", "superuser"])
            me = await http.get("/api/me")
        assert me.json()["roles"] == ["auditor", "operator"]

    @pytest.mark.parametrize(
        "claims, expected",
        [
            ({"aud": "another-client"}, "InvalidAudienceError"),
            ({"iss": "https://evil.test"}, "InvalidIssuerError"),
            ({"exp": int(time.time()) - 3600}, "ExpiredSignatureError"),
            ({"nonce": "not-the-one-we-issued"}, "nonce"),
            ({"nonce": None}, "nonce"),
            ({"sub": None}, "MissingRequiredClaimError"),
        ],
    )
    async def test_id_token_with_wrong_claims_is_refused(self, backend, claims, expected):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            callback = await sign_in(http, idp, **claims)
        assert callback.status_code == 401
        assert callback.json() == {"error": "sign-in failed"}, "the browser is not told why"
        failed = [
            e
            for e in await events_for(ctx.db, ctx.tenant_id)
            if e["event_type"] == "auth.login_failed"
        ]
        assert expected in failed[-1]["payload"]["reason"]

    async def test_token_signed_by_an_unknown_key_is_refused(self, backend):
        idp, app, ctx = await oidc_app(backend)
        attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        async with client(app) as http:
            sent = await start_login(http)
            claims = idp.standard_claims(sent["nonce"])
            idp.id_token = idp.sign(claims, key=attacker)  # right kid, wrong key
            same_kid = await http.get(
                "/auth/callback", params={"code": "c", "state": sent["state"]}
            )
            sent = await start_login(http)
            idp.id_token = idp.sign(idp.standard_claims(sent["nonce"]), key=attacker, kid="other")
            other_kid = await http.get(
                "/auth/callback", params={"code": "c", "state": sent["state"]}
            )
        assert same_kid.status_code == 401 and other_kid.status_code == 401

    async def test_symmetric_and_unsigned_tokens_are_refused(self, backend):
        """A token signed with the client secret, or not signed, must never be accepted."""
        import jwt

        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            for algorithm, key in (("HS256", "client-secret-value" * 3), ("none", None)):
                sent = await start_login(http)
                idp.id_token = jwt.encode(
                    idp.standard_claims(sent["nonce"]),
                    key,
                    algorithm=algorithm,
                    headers={"kid": idp.kid},
                )
                resp = await http.get(
                    "/auth/callback", params={"code": "c", "state": sent["state"]}
                )
                assert resp.status_code == 401, algorithm
        assert "HS256" not in ALLOWED_ALGORITHMS and "none" not in ALLOWED_ALGORITHMS

    async def test_rotated_provider_key_is_picked_up(self, backend):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            await sign_in(http, idp)  # loads the first key set
            new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            idp.published_keys = [idp.jwk(new_key, "key-2")]
            sent = await start_login(http)
            idp.id_token = idp.sign(idp.standard_claims(sent["nonce"]), key=new_key, kid="key-2")
            resp = await http.get("/auth/callback", params={"code": "c", "state": sent["state"]})
        assert resp.status_code == 303

    async def test_state_is_single_use_and_must_be_one_we_issued(self, backend):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            sent = await start_login(http)
            idp.id_token = idp.sign(idp.standard_claims(sent["nonce"]))
            first = await http.get("/auth/callback", params={"code": "c", "state": sent["state"]})
            http.cookies.clear()
            reused = await http.get("/auth/callback", params={"code": "c", "state": sent["state"]})
            forged = await http.get("/auth/callback", params={"code": "c", "state": "made-up"})
            missing = await http.get("/auth/callback", params={"code": "c"})
        assert first.status_code == 303
        assert [reused.status_code, forged.status_code, missing.status_code] == [401, 401, 401]

    async def test_expired_state_is_refused(self, backend):
        idp, app, ctx = await oidc_app(backend)
        async with client(app) as http:
            sent = await start_login(http)
            async with ctx.db.tenant(ctx.tenant_id) as conn:
                await conn.execute(
                    sa.update(login_states).values(expires_at=utcnow() - timedelta(minutes=1))
                )
            idp.id_token = idp.sign(idp.standard_claims(sent["nonce"]))
            resp = await http.get("/auth/callback", params={"code": "c", "state": sent["state"]})
        assert resp.status_code == 401

    async def test_provider_refusing_the_code_fails_sign_in(self, backend):
        idp, app, ctx = await oidc_app(backend)
        idp.token_status = 400
        async with client(app) as http:
            sent = await start_login(http)
            resp = await http.get("/auth/callback", params={"code": "bad", "state": sent["state"]})
        assert resp.status_code == 401

    async def test_metadata_for_a_different_issuer_is_refused(self, backend):
        idp, app, ctx = await oidc_app(backend)
        idp.metadata_issuer = "https://evil.test"
        async with client(app) as http:
            with pytest.raises(Exception, match="different issuer"):
                await http.get("/auth/login")

    def test_plain_http_issuer_is_refused(self):
        settings = AuthSettings(
            oidc_issuer="http://idp.test", oidc_client_id="x", oidc_redirect_uri="https://a/cb"
        )
        with pytest.raises(ConfigError, match="https"):
            OidcClient(settings, http=None)  # type: ignore[arg-type]

    async def test_sign_in_routes_do_not_exist_when_not_configured(self, backend):
        app, _ = await build(backend)
        async with client(app) as http:
            for method, path in (
                ("GET", "/auth/login"),
                ("GET", "/auth/callback"),
                ("GET", "/auth/saml/login"),
                ("POST", "/auth/saml/acs"),
                ("GET", "/auth/saml/metadata"),
            ):
                assert (await http.request(method, path)).status_code == 404, path
            methods = (await http.get("/auth/methods")).json()
        assert methods == {"oidc": False, "saml": False, "break_glass": False, "demo": False}


class TestSessions:
    async def test_state_changing_request_needs_the_csrf_token(self, backend):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["tenant_admin"])
        body = {"group": "ops", "role": "operator"}
        async with client(app) as http:
            http.cookies.update(cookies)
            without = await http.put("/api/role-mappings", json=body)
            wrong = await http.put("/api/role-mappings", json=body, headers={CSRF_HEADER: "guess"})
            read = await http.get("/api/role-mappings")
            right = await http.put("/api/role-mappings", json=body, headers={CSRF_HEADER: csrf})
        assert [without.status_code, wrong.status_code] == [403, 403]
        assert read.status_code == 200 and read.json() == {"items": []}
        assert right.status_code == 200 and right.json() == {"status": "granted"}

    async def test_api_token_needs_no_csrf_token(self, backend):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["tenant_admin"])
        async with client(app) as http:
            http.cookies.update(cookies)
            created = await http.post(
                "/api/tokens",
                json={"name": "ci", "permissions": ["incidents.read"]},
                headers={CSRF_HEADER: csrf},
            )
            http.cookies.clear()
            resp = await http.get(
                "/api/incidents", headers={"Authorization": f"Bearer {created.json()['token']}"}
            )
        assert created.status_code == 201 and resp.status_code == 200

    async def test_logout_revokes_the_session_on_the_server(self, backend):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["viewer"])
        async with client(app) as http:
            http.cookies.update(cookies)
            out = await http.post("/auth/logout", headers={CSRF_HEADER: csrf})
            http.cookies.update(cookies)  # an attacker who kept a copy of the cookie
            after = await http.get("/api/me")
        assert out.status_code == 200
        assert after.status_code == 401
        assert "auth.logout" in await audit_types(ctx)

    @pytest.mark.parametrize(
        "change",
        [
            {"expires_at": utcnow() - timedelta(seconds=1)},
            {"last_seen_at": utcnow() - timedelta(minutes=31)},
            {"revoked_at": utcnow()},
        ],
        ids=["expired", "idle", "revoked"],
    )
    async def test_expired_idle_and_revoked_sessions_are_refused(self, backend, change):
        app, ctx = await build(backend)
        cookies, _ = await session(ctx, ["viewer"])
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            await conn.execute(sa.update(sessions).values(**change))
        async with client(app) as http:
            http.cookies.update(cookies)
            assert (await http.get("/api/me")).status_code == 401

    async def test_forged_and_malformed_cookies_are_refused(self, backend):
        app, ctx = await build(backend)
        cookies, _ = await session(ctx, ["viewer"])
        real = cookies[SESSION_COOKIE]
        tenant_hex, secret = real.split(".")
        async with client(app) as http:
            for forged in (
                f"{tenant_hex}.{secret[:-1]}x",
                f"{'0' * 32}.{secret}",
                secret,
                "not-a-cookie",
                f"{tenant_hex}.",
                "'; DROP TABLE sessions;--.x",
            ):
                http.cookies.clear()
                http.cookies.set(SESSION_COOKIE, forged)
                assert (await http.get("/api/me")).status_code == 401, forged

    async def test_session_roles_are_fixed_at_sign_in_and_unknown_ones_ignored(self, backend):
        app, ctx = await build(backend)
        cookies, _ = await session(ctx, ["viewer", "root", "admin"])
        async with client(app) as http:
            http.cookies.update(cookies)
            me = (await http.get("/api/me")).json()
        assert me["roles"] == ["viewer"]
        assert me["permissions"] == ["incidents.read"]


class TestApiTokens:
    async def test_create_use_list_and_revoke(self, backend):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["tenant_admin"])
        async with client(app) as http:
            http.cookies.update(cookies)
            created = await http.post(
                "/api/tokens",
                json={"name": "siem", "permissions": ["audit.read"], "days": 30},
                headers={CSRF_HEADER: csrf},
            )
            token = created.json()["token"]
            listed = (await http.get("/api/tokens")).json()["items"]
            http.cookies.clear()
            bearer = {"Authorization": f"Bearer {token}"}
            used = await http.get("/api/audit/events", headers=bearer)
            outside_scope = await http.get("/api/incidents", headers=bearer)
            http.cookies.update(cookies)
            revoked = await http.delete(
                f"/api/tokens/{created.json()['id']}", headers={CSRF_HEADER: csrf}
            )
            twice = await http.delete(
                f"/api/tokens/{created.json()['id']}", headers={CSRF_HEADER: csrf}
            )
            http.cookies.clear()
            after = await http.get("/api/audit/events", headers=bearer)

        assert created.status_code == 201
        assert listed[0]["name"] == "siem" and "token" not in listed[0]
        assert used.status_code == 200 and outside_scope.status_code == 403
        assert revoked.status_code == 200 and twice.status_code == 404
        assert after.status_code == 401
        types = await audit_types(ctx)
        assert "token.created" in types and "token.revoked" in types

    async def test_only_a_hash_of_the_token_is_stored(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(ctx, "incidents.read")
        token = headers["Authorization"].split()[1]
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            row = (await conn.execute(sa.select(api_tokens))).one()
        assert token not in str(tuple(row)) and token.split("_")[-1] not in str(tuple(row))

    @pytest.mark.parametrize(
        "permissions",
        [
            ["tokens.manage"],
            ["roles.manage"],
            ["actions.approve"],
            ["sops.approve"],
            ["incidents.read", "nope"],
        ],
    )
    async def test_a_token_cannot_carry_administrative_or_approval_rights(
        self, backend, permissions
    ):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["tenant_admin"])
        async with client(app) as http:
            http.cookies.update(cookies)
            resp = await http.post(
                "/api/tokens",
                json={"name": "x", "permissions": permissions},
                headers={CSRF_HEADER: csrf},
            )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "x", "permissions": []},
            {"name": "", "permissions": ["incidents.read"]},
            {"name": "x", "permissions": ["incidents.read"], "days": 4000},
        ],
    )
    async def test_invalid_token_requests_are_rejected(self, backend, body):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["tenant_admin"])
        async with client(app) as http:
            http.cookies.update(cookies)
            resp = await http.post("/api/tokens", json=body, headers={CSRF_HEADER: csrf})
        assert resp.status_code == 422

    async def test_expired_token_is_refused(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(ctx, "incidents.read")
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            await conn.execute(
                sa.update(api_tokens).values(expires_at=utcnow() - timedelta(seconds=1))
            )
        async with client(app) as http:
            assert (await http.get("/api/incidents", headers=headers)).status_code == 401

    async def test_token_cannot_manage_tokens(self, backend):
        app, ctx = await build(backend)
        headers = await token_headers(
            ctx,
            *(
                p.value
                for p in (Permission.INCIDENTS_READ, Permission.SYSTEM_READ, Permission.AUDIT_READ)
            ),
        )
        async with client(app) as http:
            resp = await http.post(
                "/api/tokens",
                json={"name": "x", "permissions": ["incidents.read"]},
                headers=headers,
            )
        assert resp.status_code == 403


class TestRoleMappings:
    async def test_grant_list_remove_are_audited(self, backend):
        app, ctx = await build(backend)
        cookies, csrf = await session(ctx, ["tenant_admin"])
        h = {CSRF_HEADER: csrf}
        async with client(app) as http:
            http.cookies.update(cookies)
            granted = await http.put(
                "/api/role-mappings", json={"group": "ops", "role": "operator"}, headers=h
            )
            again = await http.put(
                "/api/role-mappings", json={"group": "ops", "role": "operator"}, headers=h
            )
            unknown = await http.put(
                "/api/role-mappings", json={"group": "ops", "role": "god"}, headers=h
            )
            listed = await http.get("/api/role-mappings")
            removed = await http.delete(
                "/api/role-mappings", params={"group": "ops", "role": "operator"}, headers=h
            )
            missing = await http.delete(
                "/api/role-mappings", params={"group": "ops", "role": "operator"}, headers=h
            )
        assert granted.json() == {"status": "granted"} and again.json() == {"status": "unchanged"}
        assert unknown.status_code == 422
        assert listed.json() == {"items": [{"group": "ops", "role": "operator"}]}
        assert removed.status_code == 200 and missing.status_code == 404
        types = await audit_types(ctx)
        assert (
            types.count("roles.mapping_granted") == 1 and types.count("roles.mapping_removed") == 1
        )

    async def test_only_an_administrator_changes_mappings(self, backend):
        app, ctx = await build(backend)
        for role in ("viewer", "operator", "auditor", "approver"):
            cookies, csrf = await session(ctx, [role])
            async with client(app) as http:
                http.cookies.update(cookies)
                resp = await http.put(
                    "/api/role-mappings",
                    json={"group": "me", "role": "tenant_admin"},
                    headers={CSRF_HEADER: csrf},
                )
            assert resp.status_code == 403, role


class TestBreakGlass:
    PASSWORD = "correct horse battery staple"

    async def test_disabled_unless_a_hash_is_configured(self, backend):
        app, _ = await build(backend)
        async with client(app) as http:
            resp = await http.post("/auth/break-glass", json={"password": self.PASSWORD})
        assert resp.status_code == 404

    async def test_use_is_audited_and_gives_a_short_admin_session(self, backend):
        stored = breakglass.hash_password(self.PASSWORD)
        app, ctx = await build(backend, auth={"break_glass_password_hash": stored})
        async with client(app) as http:
            wrong = await http.post("/auth/break-glass", json={"password": "wrong password here"})
            assert (await http.get("/api/me")).status_code == 401
            right = await http.post("/auth/break-glass", json={"password": self.PASSWORD})
            me = (await http.get("/api/me")).json()
        assert wrong.status_code == 401 and right.status_code == 303
        assert me["roles"] == ["tenant_admin"] and me["id"] == "local|break-glass"
        assert "Max-Age=3600" in right.headers["set-cookie"]
        types = await audit_types(ctx)
        assert "auth.login_failed" in types and "auth.break_glass_used" in types
        async with ctx.db.tenant(ctx.tenant_id) as conn:
            assert (await conn.execute(sa.select(users.c.subject))).scalar() == "break-glass"

    def test_hash_is_salted_and_verifies_only_the_right_password(self):
        one, two = breakglass.hash_password(self.PASSWORD), breakglass.hash_password(self.PASSWORD)
        assert one != two and self.PASSWORD not in one
        assert breakglass.verify_password(self.PASSWORD, one)
        assert not breakglass.verify_password(self.PASSWORD + "x", one)
        for malformed in ("", "plain", "scrypt$1$2", "md5$1$1$1$a$b"):
            assert not breakglass.verify_password(self.PASSWORD, malformed)
        with pytest.raises(ValueError):
            breakglass.hash_password("short")
