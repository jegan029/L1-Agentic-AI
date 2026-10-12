"""Start-up security checks and the webhook signature.

The listener is deny by default (api/access.py). This module holds the checks
that stop the service starting in an unsafe configuration, and the HMAC
scheme a webhook sender uses.

Demo mode may run without credentials, and only on a loopback address.
"""

from __future__ import annotations

import hashlib
import hmac

from src.l1_agent.config.settings import Settings
from src.l1_agent.utils.logging import get_logger

logger = get_logger("security")

TIMESTAMP_HEADER = "X-L1-Timestamp"
SIGNATURE_HEADER = "X-L1-Signature"
CSRF_HEADER = "X-CSRF-Token"
SESSION_COOKIE = "l1_session"
MAX_BODY_BYTES = 256 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MIN_SECRET_LENGTH = 32

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS = "max-age=31536000; includeSubDomains"


class SecurityConfigError(RuntimeError):
    """The service refuses to start with this configuration."""


def sign_in_configured(settings: Settings) -> bool:
    return bool(settings.auth.oidc_issuer or settings.auth.saml_idp_metadata_path)


def demo_open(settings: Settings) -> bool:
    """Demo mode with no identity provider: every caller is the demo user."""
    return settings.agent.demo_mode and not sign_in_configured(settings)


def webhook_open(settings: Settings) -> bool:
    return settings.agent.demo_mode and not settings.agent.webhook_secret


def validate_security_config(settings: Settings) -> None:
    """Fail start-up when the listener would be reachable without credentials."""
    agent, auth = settings.agent, settings.auth
    if agent.webhook_secret and len(agent.webhook_secret) < MIN_SECRET_LENGTH:
        raise SecurityConfigError(
            f"AGENT_WEBHOOK_SECRET must be at least {MIN_SECRET_LENGTH} characters"
        )
    if auth.audit_checkpoint_key and len(auth.audit_checkpoint_key) < MIN_SECRET_LENGTH:
        raise SecurityConfigError(
            f"AUDIT_CHECKPOINT_KEY must be at least {MIN_SECRET_LENGTH} characters"
        )
    if bool(agent.tls_cert_file) != bool(agent.tls_key_file):
        raise SecurityConfigError(
            "AGENT_TLS_CERT_FILE and AGENT_TLS_KEY_FILE are set together or not at all"
        )
    if "*" in agent.cors_allowed_origins:
        raise SecurityConfigError("AGENT_CORS_ORIGINS must list origins; '*' is not allowed")
    if auth.session_hours < 1 or auth.session_idle_minutes < 1:
        raise SecurityConfigError("Session lifetime and idle timeout must be at least 1")

    if not agent.demo_mode:
        if not agent.webhook_secret:
            raise SecurityConfigError(
                "Refusing to start without credentials: set AGENT_WEBHOOK_SECRET"
            )
        if not sign_in_configured(settings):
            # Not fatal: a deployment may be driven by API tokens alone.
            logger.warning(
                "No identity provider is configured (OIDC_ISSUER or SAML_IDP_METADATA_PATH); "
                "only API tokens and the break-glass account can reach the API"
            )
        return

    unauthenticated = [
        name
        for name, is_open in (
            ("the API (no identity provider)", demo_open(settings)),
            ("the webhook (AGENT_WEBHOOK_SECRET unset)", webhook_open(settings)),
        )
        if is_open
    ]
    if unauthenticated and agent.webhook_host not in LOOPBACK_HOSTS:
        raise SecurityConfigError(
            "Demo mode leaves " + " and ".join(unauthenticated) + " unauthenticated, so it may "
            "only listen on a loopback address; set AGENT_WEBHOOK_HOST=127.0.0.1 or configure "
            "the credentials"
        )
    if unauthenticated:
        logger.warning("DEMO MODE: unauthenticated on loopback: %s", "; ".join(unauthenticated))


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """Signature a sender must put in X-L1-Signature."""
    mac = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()
