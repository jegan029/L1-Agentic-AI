"""Credential references and their resolution (ADR 0005).

A connection never holds a credential. It holds a reference such as
`vault:splunk/token` or `file:/run/secrets/splunk-token`, and the value is
read from the backend when a call needs it. Values are cached for a short
time only, so a credential rotated in the backend is picked up without a
restart; a caller whose credential was rejected can ask for a fresh read.

Values are never written to the process environment, the database or a log.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol, Tuple

from src.l1_agent.config.settings import ConfigError, SecretsSettings

DEFAULT_TTL_SECONDS = 30.0
MAX_SECRET_BYTES = 64 * 1024
_REF_RE = re.compile(r"^(env|file|vault|aws):(.+)$", re.DOTALL)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ABSOLUTE_RE = re.compile(r"^(/|[A-Za-z]:[\\/])")
_PATH_RE = re.compile(r"^[A-Za-z0-9_./\-]+$")


class SecretError(RuntimeError):
    """A reference is malformed, or its value could not be read. Never carries the value."""


@dataclass(frozen=True)
class SecretRef:
    backend: str
    locator: str

    @classmethod
    def parse(cls, text: str) -> "SecretRef":
        match = _REF_RE.match(text or "")
        if not match:
            raise SecretError(
                "A credential reference looks like env:NAME, file:/path, vault:path/field "
                "or aws:name"
            )
        backend, locator = match.group(1), match.group(2).strip()
        if backend == "env" and not _ENV_NAME_RE.match(locator):
            raise SecretError("env: needs a variable name")
        if backend in ("vault", "aws") and (not _PATH_RE.match(locator) or ".." in locator):
            raise SecretError(f"{backend}: needs a plain path")
        # Decided by the text, not by the machine this runs on.
        if backend == "file" and not _ABSOLUTE_RE.match(locator):
            raise SecretError("file: needs an absolute path")
        return cls(backend, locator)

    def __str__(self) -> str:
        return f"{self.backend}:{self.locator}"


class SecretBackend(Protocol):
    def read(self, locator: str) -> str:
        """Return the current value, or raise. Blocking; called from a worker thread."""


class EnvBackend:
    """Process environment. Rotation needs a restart; use it for development only."""

    def read(self, locator: str) -> str:
        value = os.environ.get(locator, "")
        if not value:
            raise SecretError(f"environment variable {locator} is not set")
        return value


class FileBackend:
    """A mounted secret file, the form Kubernetes and most agents deliver secrets in."""

    def __init__(self, roots: Tuple[str, ...] = ()) -> None:
        self._roots = tuple(Path(r).resolve() for r in roots)

    def read(self, locator: str) -> str:
        path = Path(locator).resolve()
        if self._roots and not any(root == path or root in path.parents for root in self._roots):
            raise SecretError("the file is outside the configured secret directories")
        try:
            if path.stat().st_size > MAX_SECRET_BYTES:
                raise SecretError("the secret file is too large")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretError(f"secret file could not be read: {type(exc).__name__}") from None
        if not value:
            raise SecretError("the secret file is empty")
        return value


class VaultBackend:
    """HashiCorp Vault KV v2. The locator is `<path>/<field>` under the configured prefix."""

    def __init__(self, settings: SecretsSettings) -> None:
        self._settings = settings

    def _client(self):
        try:
            import hvac  # type: ignore[import]
        except ImportError:
            raise SecretError("install 'hvac' to use vault: references") from None
        s = self._settings
        client = hvac.Client(url=s.vault_addr)
        if s.vault_token:
            client.token = s.vault_token
        elif s.vault_role_id and s.vault_secret_id:
            client.auth.approle.login(role_id=s.vault_role_id, secret_id=s.vault_secret_id)
        else:
            raise SecretError("set VAULT_TOKEN or both VAULT_ROLE_ID and VAULT_SECRET_ID")
        return client

    def read(self, locator: str) -> str:
        path, _, field = locator.rpartition("/")
        if not path:
            raise SecretError("vault: needs path/field")
        prefix = self._settings.vault_path_prefix.strip("/")
        try:
            response = self._client().secrets.kv.v2.read_secret_version(
                path=f"{prefix}/{path}" if prefix else path,
                mount_point=self._settings.vault_mount,
            )
            value = response["data"]["data"].get(field, "")
        except SecretError:
            raise
        except Exception as exc:
            raise SecretError(f"vault read failed: {type(exc).__name__}") from None
        if not value:
            raise SecretError("the vault field is empty or missing")
        return str(value)


class AwsBackend:
    """AWS SSM Parameter Store, SecureString. The locator is the name under the prefix."""

    def __init__(self, settings: SecretsSettings) -> None:
        self._settings = settings

    def read(self, locator: str) -> str:
        try:
            import boto3  # type: ignore[import]
        except ImportError:
            raise SecretError("install 'boto3' to use aws: references") from None
        name = f"{self._settings.aws_ssm_prefix.rstrip('/')}/{locator.lstrip('/')}"
        try:
            client = boto3.client("ssm", region_name=self._settings.aws_region)
            value = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except Exception as exc:
            raise SecretError(f"parameter store read failed: {type(exc).__name__}") from None
        if not value:
            raise SecretError("the parameter is empty")
        return str(value)


class SecretResolver:
    """Reads references through a short-lived cache."""

    def __init__(
        self,
        backends: Mapping[str, SecretBackend],
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backends = dict(backends)
        self._ttl = ttl_seconds
        self._clock = clock
        self._cache: Dict[str, Tuple[float, str]] = {}

    @classmethod
    def from_settings(
        cls, settings: SecretsSettings, *, ttl_seconds: float = DEFAULT_TTL_SECONDS
    ) -> "SecretResolver":
        roots = tuple(r for r in os.environ.get("SECRETS_FILE_ROOTS", "").split(os.pathsep) if r)
        return cls(
            {
                "env": EnvBackend(),
                "file": FileBackend(roots),
                "vault": VaultBackend(settings),
                "aws": AwsBackend(settings),
            },
            ttl_seconds=ttl_seconds,
        )

    async def get(self, reference: str, *, fresh: bool = False) -> str:
        """The value a reference points at. `fresh` skips the cache."""
        ref = SecretRef.parse(reference)
        key = str(ref)
        cached = self._cache.get(key)
        if cached and not fresh and self._clock() < cached[0]:
            return cached[1]
        backend = self._backends.get(ref.backend)
        if backend is None:
            raise SecretError(f"no '{ref.backend}' secrets backend is configured")
        value = await asyncio.to_thread(backend.read, ref.locator)
        self._cache[key] = (self._clock() + self._ttl, value)
        return value

    def invalidate(self, reference: Optional[str] = None) -> None:
        if reference is None:
            self._cache.clear()
        else:
            self._cache.pop(str(SecretRef.parse(reference)), None)


class ConnectionSecrets:
    """The secret slots of one connection: slot name to reference."""

    def __init__(self, resolver: SecretResolver, references: Mapping[str, str]) -> None:
        self._resolver = resolver
        self._references = {k: v for k, v in references.items() if v}
        for reference in self._references.values():
            SecretRef.parse(reference)  # reject a malformed reference when it is configured

    @property
    def slots(self) -> Tuple[str, ...]:
        return tuple(sorted(self._references))

    @property
    def references(self) -> Dict[str, str]:
        """Slot name to reference. Where each credential is kept; never a value."""
        return dict(self._references)

    def has(self, slot: str) -> bool:
        return slot in self._references

    async def get(self, slot: str, *, fresh: bool = False) -> str:
        reference = self._references.get(slot)
        if not reference:
            return ""
        return await self._resolver.get(reference, fresh=fresh)

    async def all(self, *, fresh: bool = False) -> Dict[str, str]:
        return {slot: await self.get(slot, fresh=fresh) for slot in self._references}


class LiteralSecrets(ConnectionSecrets):
    """Values that were read once at start-up. They do not follow a rotation."""

    def __init__(self, values: Mapping[str, str]) -> None:
        super().__init__(SecretResolver({}), {})
        self._values = {k: v for k, v in values.items() if v}

    @property
    def slots(self) -> Tuple[str, ...]:
        return tuple(sorted(self._values))

    def has(self, slot: str) -> bool:
        return slot in self._values

    async def get(self, slot: str, *, fresh: bool = False) -> str:
        return self._values.get(slot, "")

    async def all(self, *, fresh: bool = False) -> Dict[str, str]:
        return dict(self._values)


def check_reference(reference: str) -> None:
    """Raise ConfigError for a reference that could never resolve."""
    try:
        SecretRef.parse(reference)
    except SecretError as exc:
        raise ConfigError(str(exc)) from None
