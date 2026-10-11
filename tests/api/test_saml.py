"""SAML sign-in against a fake identity provider that signs real assertions.

Needs python3-saml (requirements-saml.txt). Skipped without it, unless
L1_REQUIRE_SAML=1 (set in CI), in which case a missing library is a failure.
Behaviour against a real identity provider is not verified here.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from src.l1_agent.audit.log import events_for
from src.l1_agent.auth import store
from src.l1_agent.config.settings import AuthSettings, ConfigError
from tests.api.support import ADMIN, BASE_URL, audit_types, build, client

try:
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    from src.l1_agent.auth.saml import SamlProvider
except ImportError:  # pragma: no cover - depends on the environment
    if os.environ.get("L1_REQUIRE_SAML") == "1":
        raise
    pytest.skip("python3-saml is not installed", allow_module_level=True)

IDP_ENTITY = "https://idp.test/saml"
SP_ENTITY = "https://agent.test/saml"
ACS = f"{BASE_URL}/auth/saml/acs"


EXPECTED_REASON = {
    "unsigned": "is not signed",
    "signed_by_another_key": "Signature validation failed",
    "attribute_changed_after_signing": "Signature validation failed",
    "subject_changed_after_signing": "Signature validation failed",
    "wrong_audience": "not a valid audience",
    "wrong_recipient": "was received at",
    "expired": "expired",
    "another_issuer": "Invalid issuer",
    "answers_a_different_request": "does not match the ID of the AuthNRequest",
}


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeSamlIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake idp")])
        now = datetime.now(timezone.utc)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .sign(self.key, hashes.SHA256())
        )

    @property
    def key_pem(self) -> str:
        return self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    @property
    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def metadata(self) -> str:
        der = base64.b64encode(self.cert.public_bytes(serialization.Encoding.DER)).decode()
        return (
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{IDP_ENTITY}">'
            '<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:KeyDescriptor use="signing"><ds:KeyInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">'
            f"<ds:X509Data><ds:X509Certificate>{der}</ds:X509Certificate></ds:X509Data>"
            "</ds:KeyInfo></md:KeyDescriptor>"
            '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            'Location="https://idp.test/sso"/>'
            "</md:IDPSSODescriptor></md:EntityDescriptor>"
        )

    def response(
        self,
        request_id: str,
        *,
        subject: str = "alice",
        groups=("ops",),
        audience: str = SP_ENTITY,
        recipient: str = ACS,
        issuer: str = IDP_ENTITY,
        valid_for: timedelta = timedelta(minutes=5),
        sign: bool = True,
        signer: "FakeSamlIdp" = None,
        tamper=None,
    ) -> str:
        now = datetime.now(timezone.utc)
        until = iso(now + valid_for)
        group_values = "".join(
            f'<saml:AttributeValue xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="xs:string">{g}'
            "</saml:AttributeValue>"
            for g in groups
        )
        assertion = (
            f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="_a{uuid.uuid4().hex}" Version="2.0" IssueInstant="{iso(now)}">'
            f"<saml:Issuer>{issuer}</saml:Issuer>"
            f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified">'
            f"{subject}</saml:NameID>"
            '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml:SubjectConfirmationData NotOnOrAfter="{until}" Recipient="{recipient}" '
            f'InResponseTo="{request_id}"/></saml:SubjectConfirmation></saml:Subject>'
            f'<saml:Conditions NotBefore="{iso(now - timedelta(minutes=1))}" NotOnOrAfter="{until}">'
            f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience>"
            "</saml:AudienceRestriction></saml:Conditions>"
            f'<saml:AuthnStatement AuthnInstant="{iso(now)}" SessionIndex="_s1">'
            "<saml:AuthnContext><saml:AuthnContextClassRef>"
            "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
            "</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
            '<saml:AttributeStatement><saml:Attribute Name="groups">'
            f"{group_values}</saml:Attribute>"
            '<saml:Attribute Name="email"><saml:AttributeValue>alice@example.com'
            "</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>"
            "</saml:Assertion>"
        )
        if sign:
            who = signer or self
            signed = OneLogin_Saml2_Utils.add_sign(
                assertion,
                who.key_pem,
                who.cert_pem,
                sign_algorithm=C.RSA_SHA256,
                digest_algorithm=C.SHA256,
            )
            assertion = signed.decode() if isinstance(signed, bytes) else signed
            assertion = assertion.replace('<?xml version="1.0" encoding="UTF-8"?>', "").strip()
            assertion = assertion.replace("<?xml version='1.0' encoding='UTF-8'?>", "").strip()
        if tamper:
            assertion = tamper(assertion)
        xml = (
            f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r{uuid.uuid4().hex}" '
            f'Version="2.0" IssueInstant="{iso(now)}" Destination="{recipient}" '
            f'InResponseTo="{request_id}"><saml:Issuer>{issuer}</saml:Issuer>'
            '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>'
            f"</samlp:Status>{assertion}</samlp:Response>"
        )
        return base64.b64encode(xml.encode()).decode()


@pytest.fixture
async def saml_app(backend, tmp_path):
    idp = FakeSamlIdp()
    path = tmp_path / "idp.xml"
    path.write_text(idp.metadata(), encoding="utf-8")
    provider = SamlProvider(
        AuthSettings(
            saml_idp_metadata_path=str(path), saml_sp_entity_id=SP_ENTITY, saml_acs_url=ACS
        )
    )
    app, ctx = await build(backend, saml=provider)
    await store.set_role_mapping(ctx.db, ctx.tenant_id, "ops", "operator", ADMIN, grant=True)
    return idp, app, ctx


async def begin(http) -> dict:
    """Start sign-in. Returns the relay state and the id of the request sent to the provider."""
    resp = await http.get("/auth/saml/login")
    assert resp.status_code == 302 and resp.headers["location"].startswith("https://idp.test/sso")
    query = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}
    request_xml = OneLogin_Saml2_Utils.decode_base64_and_inflate(query["SAMLRequest"]).decode()
    request_id = request_xml.split('ID="', 1)[1].split('"', 1)[0]
    return {"relay": query["RelayState"], "request_id": request_id, "xml": request_xml}


async def post(http, relay: str, saml_response: str):
    return await http.post(
        "/auth/saml/acs", data={"RelayState": relay, "SAMLResponse": saml_response}
    )


class TestSamlSignIn:
    async def test_signed_assertion_creates_a_session(self, saml_app):
        idp, app, ctx = saml_app
        async with client(app) as http:
            started = await begin(http)
            resp = await post(http, started["relay"], idp.response(started["request_id"]))
            me = await http.get("/api/me")
        assert SP_ENTITY in started["xml"] and ACS in started["xml"]
        assert resp.status_code == 303
        assert me.json()["roles"] == ["operator"]
        assert me.json()["id"] == f"{IDP_ENTITY}|alice"
        assert "auth.login" in await audit_types(ctx)

    @pytest.mark.parametrize(
        "case",
        [
            "unsigned",
            "signed_by_another_key",
            "attribute_changed_after_signing",
            "subject_changed_after_signing",
            "wrong_audience",
            "wrong_recipient",
            "expired",
            "another_issuer",
            "answers_a_different_request",
        ],
    )
    async def test_bad_assertions_are_refused(self, saml_app, case):
        idp, app, ctx = saml_app
        async with client(app) as http:
            started = await begin(http)
            rid = started["request_id"]
            response = {
                "unsigned": lambda: idp.response(rid, sign=False),
                "signed_by_another_key": lambda: idp.response(rid, signer=FakeSamlIdp()),
                "attribute_changed_after_signing": lambda: idp.response(
                    rid, groups=("finance",), tamper=lambda x: x.replace(">finance<", ">ops<")
                ),
                "subject_changed_after_signing": lambda: idp.response(
                    rid, tamper=lambda x: x.replace(">alice<", ">root<")
                ),
                "wrong_audience": lambda: idp.response(rid, audience="https://other.test/saml"),
                "wrong_recipient": lambda: idp.response(rid, recipient="https://evil.test/acs"),
                "expired": lambda: idp.response(rid, valid_for=timedelta(minutes=-10)),
                "another_issuer": lambda: idp.response(rid, issuer="https://evil.test/saml"),
                "answers_a_different_request": lambda: idp.response("ONELOGIN_someone_elses"),
            }[case]()
            resp = await post(http, started["relay"], response)
            me = await http.get("/api/me")
        assert resp.status_code == 401, case
        assert resp.json() == {"error": "sign-in failed"}
        assert me.status_code == 401
        failed = [
            e
            for e in await events_for(ctx.db, ctx.tenant_id)
            if e["event_type"] == "auth.login_failed"
        ]
        reason = failed[-1]["payload"]["reason"]
        # Refused for the reason under test, not because the test built a broken document.
        assert EXPECTED_REASON[case] in reason, reason

    async def test_response_cannot_be_replayed(self, saml_app):
        idp, app, _ = saml_app
        async with client(app) as http:
            started = await begin(http)
            response = idp.response(started["request_id"])
            first = await post(http, started["relay"], response)
            http.cookies.clear()
            second = await post(http, started["relay"], response)
        assert first.status_code == 303 and second.status_code == 401

    async def test_unsolicited_response_is_refused(self, saml_app):
        idp, app, _ = saml_app
        async with client(app) as http:
            valid = idp.response("ONELOGIN_never_issued")
            no_state = await post(http, "", valid)
            unknown_state = await post(http, "made-up-state", valid)
        assert no_state.status_code == 401 and unknown_state.status_code == 401

    async def test_user_with_no_mapped_group_is_refused(self, saml_app):
        idp, app, _ = saml_app
        async with client(app) as http:
            started = await begin(http)
            resp = await post(
                http, started["relay"], idp.response(started["request_id"], groups=("finance",))
            )
        assert resp.status_code == 401

    async def test_service_provider_metadata(self, saml_app):
        _, app, _ = saml_app
        async with client(app) as http:
            resp = await http.get("/auth/saml/metadata")
        assert resp.status_code == 200
        assert SP_ENTITY in resp.text and ACS in resp.text
        assert 'WantAssertionsSigned="true"' in resp.text

    def test_configuration_errors(self, tmp_path):
        good = tmp_path / "idp.xml"
        good.write_text(FakeSamlIdp().metadata(), encoding="utf-8")
        no_cert = tmp_path / "nocert.xml"
        no_cert.write_text(
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{IDP_ENTITY}">'
            '<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
            'Location="https://idp.test/sso"/></md:IDPSSODescriptor></md:EntityDescriptor>',
            encoding="utf-8",
        )
        base = dict(saml_sp_entity_id=SP_ENTITY, saml_acs_url=ACS)
        for settings, message in (
            (
                AuthSettings(saml_idp_metadata_path=str(tmp_path / "missing.xml"), **base),
                "Cannot read",
            ),
            (AuthSettings(saml_idp_metadata_path=str(no_cert), **base), "signing certificate"),
            (
                AuthSettings(
                    saml_idp_metadata_path=str(good),
                    saml_sp_entity_id=SP_ENTITY,
                    saml_acs_url="http://agent.test/auth/saml/acs",
                ),
                "https",
            ),
            (AuthSettings(saml_idp_metadata_path=str(good)), "SAML_SP_ENTITY_ID"),
        ):
            with pytest.raises(ConfigError, match=message):
                SamlProvider(settings)
