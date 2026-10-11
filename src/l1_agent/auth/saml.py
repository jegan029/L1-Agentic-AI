"""SAML 2.0 service provider (sign-in only), on python3-saml.

Strict mode: the assertion must be signed by the identity provider's
certificate, addressed to this service provider, inside its validity window,
and an answer to a request this service issued. Unsolicited responses are
refused, which also makes a captured response useless a second time.

The library is an optional dependency (requirements-saml.txt). It needs the
native xmlsec library.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

from src.l1_agent.auth.oidc import SignInError
from src.l1_agent.config.settings import AuthSettings, ConfigError


class SamlProvider:
    def __init__(self, settings: AuthSettings, *, allow_http: bool = False) -> None:
        try:
            from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise ConfigError(
                "SAML is configured but python3-saml is not installed "
                "(pip install -r requirements-saml.txt)"
            ) from exc
        if not (settings.saml_sp_entity_id and settings.saml_acs_url):
            raise ConfigError("SAML needs SAML_SP_ENTITY_ID and SAML_ACS_URL")
        if not allow_http and not settings.saml_acs_url.startswith("https://"):
            raise ConfigError("SAML_ACS_URL must be an https URL")
        try:
            xml = Path(settings.saml_idp_metadata_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read SAML_IDP_METADATA_PATH: {exc}") from exc
        idp = OneLogin_Saml2_IdPMetadataParser.parse(xml).get("idp")
        if not idp or not (idp.get("x509cert") or idp.get("x509certMulti")):
            raise ConfigError("The SAML IdP metadata has no signing certificate")
        self._groups_attribute = settings.saml_groups_attribute
        self._acs = urlsplit(settings.saml_acs_url)
        self.idp_entity_id = idp["entityId"]
        self._settings: Dict[str, Any] = {
            "strict": True,
            "debug": False,
            "sp": {
                "entityId": settings.saml_sp_entity_id,
                "assertionConsumerService": {
                    "url": settings.saml_acs_url,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
                "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
            },
            "idp": idp,
            "security": {
                "wantAssertionsSigned": True,
                "wantMessagesSigned": False,
                "wantNameId": True,
                "requestedAuthnContext": False,
                "rejectDeprecatedAlgorithm": True,
                "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
                "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
            },
        }

    def _auth(self, post_data: Dict[str, str]):
        from onelogin.saml2.auth import OneLogin_Saml2_Auth

        # Built from configuration, not from request headers, so a forged Host
        # header cannot change which destination the response is checked against.
        request = {
            "https": "on" if self._acs.scheme == "https" else "off",
            "http_host": self._acs.netloc,
            "script_name": self._acs.path,
            "get_data": {},
            "post_data": post_data,
        }
        return OneLogin_Saml2_Auth(request, old_settings=self._settings)

    def login_redirect(self, relay_state: str) -> Tuple[str, str]:
        """Returns (URL to send the browser to, id of the request it carries)."""
        auth = self._auth({})
        url = auth.login(return_to=relay_state)
        return url, auth.get_last_request_id()

    def metadata(self) -> str:
        from onelogin.saml2.settings import OneLogin_Saml2_Settings

        settings = OneLogin_Saml2_Settings(self._settings, sp_validation_only=True)
        xml = settings.get_sp_metadata()
        errors = settings.validate_metadata(xml)
        if errors:
            raise ConfigError("SAML service provider metadata is invalid: " + ", ".join(errors))
        return xml if isinstance(xml, str) else xml.decode("utf-8")

    def process(self, saml_response: str, request_id: str) -> Dict[str, Any]:
        """Validate a response to `request_id`. Returns subject, name, email and groups."""
        auth = self._auth({"SAMLResponse": saml_response})
        try:
            auth.process_response(request_id=request_id)
        except Exception as exc:
            raise SignInError(f"the SAML response could not be processed: {type(exc).__name__}")
        errors = auth.get_errors()
        if errors or not auth.is_authenticated():
            reason = auth.get_last_error_reason() or ", ".join(errors) or "not authenticated"
            raise SignInError(f"the SAML response was refused: {reason}")
        subject = auth.get_nameid()
        if not subject:
            raise SignInError("the SAML assertion has no NameID")
        attributes = auth.get_attributes() or {}

        def first(*names: str) -> str:
            for name in names:
                values = attributes.get(name) or []
                if values:
                    return str(values[0])
            return ""

        groups: List[str] = [str(g) for g in attributes.get(self._groups_attribute, [])]
        return {
            "subject": subject,
            "email": first("email", "mail", "urn:oid:0.9.2342.19200300.100.1.3"),
            "name": first("displayName", "name", "cn"),
            "groups": groups,
        }
