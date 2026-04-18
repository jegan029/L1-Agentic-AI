"""Secrets provider — loads sensitive credentials from a vault at startup.

Supports three backends, chosen by the SECRETS_BACKEND env var:
  - "env"   (default) — reads secrets from environment variables (dev/CI)
  - "vault" — HashiCorp Vault KV v2 via the HVAC client
  - "aws"   — AWS SSM Parameter Store (SecureString) via boto3

Usage (called once at service startup before Settings.from_env()):

    from src.l1_agent.utils.secrets import SecretsProvider
    provider = SecretsProvider.from_env()
    provider.inject()   # writes fetched secrets into os.environ

Settings.from_env() then reads them normally from os.environ, so the
rest of the codebase is unchanged regardless of which backend is used.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from src.l1_agent.utils.logging import get_logger

logger = get_logger("secrets")

# Mapping from environment variable name → vault path/key
# Add entries here when new secrets are introduced.
_SECRET_MAPPINGS: List[Dict[str, str]] = [
    # env_key           vault_path (relative to mount/prefix)
    {"env_key": "SERVICENOW_PASSWORD",  "vault_key": "servicenow/password"},
    {"env_key": "SPLUNK_TOKEN",         "vault_key": "splunk/token"},
    {"env_key": "IR360_API_KEY",        "vault_key": "ir360/api_key"},
    {"env_key": "DYNATRACE_API_TOKEN",  "vault_key": "dynatrace/api_token"},
    {"env_key": "LLM_API_KEY",          "vault_key": "llm/api_key"},
    {"env_key": "SMB_PASSWORD",         "vault_key": "smb/password"},
]


class SecretsProvider(ABC):
    """Abstract base — fetch secrets and inject them into os.environ."""

    @abstractmethod
    def fetch(self) -> Dict[str, str]:
        """Return a mapping of env_key → secret_value."""
        ...

    def inject(self) -> None:
        """Write fetched secrets into os.environ (only if not already set)."""
        secrets = self.fetch()
        injected = 0
        for key, value in secrets.items():
            if not os.environ.get(key):
                os.environ[key] = value
                injected += 1
        logger.info("Injected %d secret(s) into environment", injected)

    @classmethod
    def from_env(cls) -> "SecretsProvider":
        """Select and construct the appropriate backend from SECRETS_BACKEND."""
        backend = os.getenv("SECRETS_BACKEND", "env").lower()
        if backend == "vault":
            return VaultSecretsProvider(
                addr=os.getenv("VAULT_ADDR", "http://127.0.0.1:8200"),
                token=os.getenv("VAULT_TOKEN", ""),
                role_id=os.getenv("VAULT_ROLE_ID", ""),
                secret_id=os.getenv("VAULT_SECRET_ID", ""),
                mount=os.getenv("VAULT_MOUNT", "secret"),
                path_prefix=os.getenv("VAULT_PATH_PREFIX", "l1-agent"),
            )
        if backend == "aws":
            return AWSSecretsProvider(
                region=os.getenv("AWS_REGION", "us-east-1"),
                path_prefix=os.getenv("AWS_SSM_PREFIX", "/l1-agent"),
            )
        logger.info("Using env-based secrets provider (no vault)")
        return EnvSecretsProvider()


# ── Backends ──────────────────────────────────────────────────────────


class EnvSecretsProvider(SecretsProvider):
    """No-op provider: secrets already present in environment variables."""

    def fetch(self) -> Dict[str, str]:
        return {}


class VaultSecretsProvider(SecretsProvider):
    """Fetch secrets from HashiCorp Vault KV v2.

    Requires: pip install hvac
    Authentication: token (preferred in CI) or AppRole (preferred in prod).
    """

    def __init__(
        self,
        addr: str,
        token: str = "",
        role_id: str = "",
        secret_id: str = "",
        mount: str = "secret",
        path_prefix: str = "l1-agent",
    ) -> None:
        self._addr = addr
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._mount = mount
        self._path_prefix = path_prefix.strip("/")

    def _client(self):  # type: ignore[return]
        try:
            import hvac  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("Install 'hvac' to use Vault backend: pip install hvac") from exc

        client = hvac.Client(url=self._addr)
        if self._token:
            client.token = self._token
        elif self._role_id and self._secret_id:
            client.auth.approle.login(
                role_id=self._role_id,
                secret_id=self._secret_id,
            )
        else:
            raise RuntimeError(
                "Vault: set VAULT_TOKEN or both VAULT_ROLE_ID and VAULT_SECRET_ID"
            )
        if not client.is_authenticated():
            raise RuntimeError("Vault authentication failed")
        return client

    def fetch(self) -> Dict[str, str]:
        try:
            client = self._client()
        except Exception as exc:
            logger.error("Vault client init failed: %s", exc)
            return {}

        result: Dict[str, str] = {}
        for mapping in _SECRET_MAPPINGS:
            vault_key = mapping["vault_key"]
            env_key = mapping["env_key"]
            path = f"{self._path_prefix}/{vault_key}"
            # vault_key is "group/field" — split into path + field
            parts = path.rsplit("/", 1)
            secret_path = parts[0]
            field = parts[1] if len(parts) > 1 else "value"
            try:
                resp = client.secrets.kv.v2.read_secret_version(
                    path=secret_path,
                    mount_point=self._mount,
                )
                value = resp["data"]["data"].get(field, "")
                if value:
                    result[env_key] = str(value)
            except Exception as exc:
                logger.warning("Failed to fetch Vault secret %s: %s", path, exc)
        logger.info("Fetched %d secret(s) from HashiCorp Vault (%s)", len(result), self._addr)
        return result


class AWSSecretsProvider(SecretsProvider):
    """Fetch secrets from AWS SSM Parameter Store (SecureString).

    Requires: pip install boto3
    IAM permissions: ssm:GetParameters on the path prefix.
    """

    def __init__(self, region: str = "us-east-1", path_prefix: str = "/l1-agent") -> None:
        self._region = region
        self._path_prefix = path_prefix.rstrip("/")

    def fetch(self) -> Dict[str, str]:
        try:
            import boto3  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError("Install 'boto3' to use AWS backend: pip install boto3") from exc

        ssm = boto3.client("ssm", region_name=self._region)
        param_names = [
            f"{self._path_prefix}/{m['vault_key']}"
            for m in _SECRET_MAPPINGS
        ]

        result: Dict[str, str] = {}
        # GetParameters accepts up to 10 names per call
        for chunk_start in range(0, len(param_names), 10):
            chunk = param_names[chunk_start:chunk_start + 10]
            try:
                resp = ssm.get_parameters(Names=chunk, WithDecryption=True)
                params_by_name = {p["Name"]: p["Value"] for p in resp.get("Parameters", [])}
                invalid = resp.get("InvalidParameters", [])
                if invalid:
                    logger.warning("SSM: parameters not found: %s", invalid)
                for mapping, param_name in zip(
                    _SECRET_MAPPINGS[chunk_start:chunk_start + 10], chunk
                ):
                    value = params_by_name.get(param_name, "")
                    if value:
                        result[mapping["env_key"]] = value
            except Exception as exc:
                logger.error("AWS SSM fetch failed: %s", exc)

        logger.info("Fetched %d secret(s) from AWS SSM (%s)", len(result), self._region)
        return result
