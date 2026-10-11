"""OpenID Connect sign-in: authorisation code flow with PKCE.

The ID token is verified against the provider's published keys: signature,
issuer, audience, expiry and the nonce issued at the start of the flow. Only
asymmetric algorithms are accepted, so a token signed with the client secret
or with `none` is refused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
import jwt

from src.l1_agent.config.settings import AuthSettings, ConfigError

ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384")
METADATA_TTL_SECONDS = 3600


class SignInError(Exception):
    """Sign-in was refused. The message is for the audit log, not for the browser."""


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OidcClient:
    def __init__(
        self, settings: AuthSettings, http: httpx.AsyncClient, *, allow_http: bool = False
    ) -> None:
        if not (settings.oidc_client_id and settings.oidc_redirect_uri):
            raise ConfigError("OIDC needs OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_REDIRECT_URI")
        if not allow_http and not settings.oidc_issuer.startswith("https://"):
            raise ConfigError("OIDC_ISSUER must be an https URL")
        self._s = settings
        self._http = http
        self._allow_http = allow_http
        self._metadata: Optional[Dict[str, Any]] = None
        self._keys: List[Dict[str, Any]] = []
        self._loaded_at = 0.0

    @property
    def issuer(self) -> str:
        return self._s.oidc_issuer

    def _check_url(self, name: str, url: str) -> str:
        if not url or not (url.startswith("https://") or self._allow_http):
            raise SignInError(f"provider metadata has no usable {name}")
        return url

    async def _load(self, force: bool = False) -> Dict[str, Any]:
        fresh = time.monotonic() - self._loaded_at < METADATA_TTL_SECONDS
        if self._metadata and fresh and not force:
            return self._metadata
        resp = await self._http.get(f"{self._s.oidc_issuer}/.well-known/openid-configuration")
        resp.raise_for_status()
        metadata = resp.json()
        # The document must describe the issuer we asked for (OIDC Discovery 4.3).
        if str(metadata.get("issuer", "")).rstrip("/") != self._s.oidc_issuer:
            raise SignInError("provider metadata names a different issuer")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            self._check_url(name, metadata.get(name, ""))
        keys = await self._http.get(metadata["jwks_uri"])
        keys.raise_for_status()
        self._keys = list(keys.json().get("keys", []))
        self._metadata = metadata
        self._loaded_at = time.monotonic()
        return metadata

    async def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        metadata = await self._load()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._s.oidc_client_id,
                "redirect_uri": self._s.oidc_redirect_uri,
                "scope": self._s.oidc_scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}"

    async def exchange(self, code: str, verifier: str, nonce: str) -> Dict[str, Any]:
        """Swap the code for tokens and return the verified ID token claims."""
        metadata = await self._load()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._s.oidc_redirect_uri,
            "client_id": self._s.oidc_client_id,
            "code_verifier": verifier,
        }
        if self._s.oidc_client_secret:
            data["client_secret"] = self._s.oidc_client_secret
        resp = await self._http.post(metadata["token_endpoint"], data=data)
        if resp.status_code != 200:
            raise SignInError(f"token endpoint answered {resp.status_code}")
        id_token = resp.json().get("id_token")
        if not isinstance(id_token, str):
            raise SignInError("token response has no id_token")
        return await self.verify_id_token(id_token, nonce)

    async def _key_for(self, kid: Optional[str]) -> Dict[str, Any]:
        def find() -> Optional[Dict[str, Any]]:
            usable = [k for k in self._keys if k.get("use", "sig") == "sig"]
            if kid is None:
                return usable[0] if len(usable) == 1 else None
            return next((k for k in usable if k.get("kid") == kid), None)

        key = find()
        if key is None:  # the provider may have rotated its keys
            await self._load(force=True)
            key = find()
        if key is None:
            raise SignInError("the ID token is signed with a key the provider does not publish")
        return key

    async def verify_id_token(self, id_token: str, nonce: str) -> Dict[str, Any]:
        await self._load()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SignInError("the ID token is not a JWT") from exc
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise SignInError(f"the ID token uses an algorithm that is not accepted: {algorithm}")
        jwk = await self._key_for(header.get("kid"))
        try:
            key = jwt.PyJWK.from_json(json.dumps(jwk)).key
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=[algorithm],
                audience=self._s.oidc_client_id,
                issuer=self._s.oidc_issuer,
                leeway=60,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as exc:
            raise SignInError(f"the ID token did not verify: {type(exc).__name__}") from exc
        if not nonce or claims.get("nonce") != nonce:
            raise SignInError("the ID token does not carry the nonce issued for this sign-in")
        return claims

    def groups(self, claims: Dict[str, Any]) -> List[str]:
        value = claims.get(self._s.oidc_groups_claim, [])
        if isinstance(value, str):
            value = [value]
        return (
            [str(v) for v in value if isinstance(v, (str, int))] if isinstance(value, list) else []
        )
