"""Credential references, the short-lived cache and rotation (Phase 3 exit criterion 5)."""

from __future__ import annotations

from typing import Dict, List, Mapping

import pytest

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.connectors.sdk.base import AdapterConnector, StepContext
from src.l1_agent.connectors.sdk.manifest import (
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
    SecretSlot,
)
from src.l1_agent.connectors.sdk.schema import closed
from src.l1_agent.connectors.sdk.secrets import (
    ConnectionSecrets,
    FileBackend,
    SecretError,
    SecretRef,
    SecretResolver,
)

CTX = StepContext(None, "run-1", "step-1")


class Vault:
    """A secrets backend held in memory, with a count of reads."""

    def __init__(self, **values: str) -> None:
        self.values: Dict[str, str] = dict(values)
        self.reads: List[str] = []

    def read(self, locator: str) -> str:
        self.reads.append(locator)
        if locator not in self.values:
            raise SecretError("no such secret")
        return self.values[locator]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestReferences:
    @pytest.mark.parametrize(
        "text", ["env:SPLUNK_TOKEN", "vault:splunk/token", "aws:splunk/token", "file:/run/s/t"]
    )
    def test_well_formed_references_parse(self, text):
        assert str(SecretRef.parse(text)) == text

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "hunter2",
            "splunk/token",
            "http://vault/secret",
            "env:not a name",
            "vault:../../other-tenant/token",
            "vault:a b",
            "file:relative/path",
        ],
    )
    def test_anything_else_is_refused(self, text):
        with pytest.raises(SecretError):
            SecretRef.parse(text)

    def test_a_malformed_reference_is_refused_when_the_connection_is_configured(self):
        with pytest.raises(SecretError):
            ConnectionSecrets(SecretResolver({}), {"token": "the-actual-token"})


class TestResolver:
    async def test_a_value_is_cached_for_the_ttl_and_then_read_again(self):
        vault, clock = Vault(**{"splunk/token": "one"}), Clock()
        resolver = SecretResolver({"vault": vault}, ttl_seconds=30, clock=clock)
        assert await resolver.get("vault:splunk/token") == "one"
        vault.values["splunk/token"] = "two"
        clock.now += 29
        assert await resolver.get("vault:splunk/token") == "one"
        clock.now += 2
        assert await resolver.get("vault:splunk/token") == "two"
        assert len(vault.reads) == 2

    async def test_fresh_and_invalidate_skip_the_cache(self):
        vault = Vault(**{"a/b": "one"})
        resolver = SecretResolver({"vault": vault}, ttl_seconds=3600)
        await resolver.get("vault:a/b")
        vault.values["a/b"] = "two"
        assert await resolver.get("vault:a/b", fresh=True) == "two"
        vault.values["a/b"] = "three"
        resolver.invalidate("vault:a/b")
        assert await resolver.get("vault:a/b") == "three"

    async def test_an_unconfigured_backend_and_a_missing_secret_are_errors(self):
        resolver = SecretResolver({"vault": Vault()})
        with pytest.raises(SecretError):
            await resolver.get("aws:splunk/token")
        with pytest.raises(SecretError):
            await resolver.get("vault:splunk/absent")

    async def test_file_backend_reads_the_current_content(self, tmp_path):
        path = tmp_path / "token"
        path.write_text("one\n", encoding="utf-8")
        resolver = SecretResolver({"file": FileBackend((str(tmp_path),))}, ttl_seconds=0)
        ref = f"file:{path.as_posix()}"
        assert await resolver.get(ref) == "one"
        path.write_text("two", encoding="utf-8")
        assert await resolver.get(ref) == "two"

    async def test_file_backend_stays_inside_its_directories(self, tmp_path):
        inside, outside = tmp_path / "secrets", tmp_path / "elsewhere"
        inside.mkdir()
        outside.mkdir()
        (outside / "token").write_text("x", encoding="utf-8")
        resolver = SecretResolver({"file": FileBackend((str(inside),))})
        with pytest.raises(SecretError, match="outside"):
            await resolver.get(f"file:{(outside / 'token').as_posix()}")

    async def test_errors_never_carry_a_value(self, tmp_path):
        path = tmp_path / "empty"
        path.write_text("", encoding="utf-8")
        resolver = SecretResolver({"file": FileBackend()})
        with pytest.raises(SecretError) as err:
            await resolver.get(f"file:{path.as_posix()}")
        assert "empty" in str(err.value)


class _Target(BaseAdapter):
    """Stands for the target system: it accepts one token at a time."""

    accepted = "one"
    built: List[str] = []
    closed: List[str] = []

    def __init__(self, token: str) -> None:
        self.token = token
        _Target.built.append(token)

    @property
    def adapter_name(self) -> str:
        return "Target"

    async def execute(self, parameters) -> AdapterResult:
        if self.token != _Target.accepted:
            return AdapterResult(success=False, error="HTTP 401: Unauthorized")
        return AdapterResult(success=True, data={"used": self.token})

    async def health_check(self) -> bool:
        return self.token == _Target.accepted

    async def close(self) -> None:
        _Target.closed.append(self.token)


class _Rotating(AdapterConnector):
    manifest = Manifest(
        id="rotating",
        version="1.0.0",
        family=Family.OPERATIONS,
        display_name="Rotating",
        secrets=(SecretSlot("token", "the token"),),
        capabilities=(
            Capability(
                id="rotating.thing.read",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Read a thing",
                parameters=closed({}),
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return _Target(secrets.get("token", ""))


@pytest.fixture
def target():
    _Target.accepted, _Target.built, _Target.closed = "one", [], []
    return _Target


class TestRotation:
    def connector(self, vault: Vault, clock: Clock, ttl: float = 3600) -> _Rotating:
        resolver = SecretResolver({"vault": vault}, ttl_seconds=ttl, clock=clock)
        return _Rotating({}, ConnectionSecrets(resolver, {"token": "vault:app/token"}))

    async def test_a_rotated_credential_takes_effect_on_the_next_call(self, target):
        """The old credential is revoked and the new one is in the backend.

        The connector object is the same one throughout: nothing is restarted.
        """
        vault, clock = Vault(**{"app/token": "one"}), Clock()
        connector = self.connector(vault, clock)
        first = await connector.invoke("rotating.thing.read", {}, CTX)
        assert first.success and first.data == {"used": "one"}

        vault.values["app/token"] = "two"  # rotated in the secrets backend
        target.accepted = "two"  # and the target no longer accepts the old one

        second = await connector.invoke("rotating.thing.read", {}, CTX)
        assert second.success and second.data == {"used": "two"}
        assert target.built == ["one", "two"] and target.closed == ["one"]

    async def test_without_a_failure_the_new_credential_is_used_once_the_cache_expires(
        self, target
    ):
        vault, clock = Vault(**{"app/token": "one"}), Clock()
        connector = self.connector(vault, clock, ttl=30)
        await connector.invoke("rotating.thing.read", {}, CTX)
        vault.values["app/token"] = "two"
        target.accepted = "two"
        clock.now += 31
        result = await connector.invoke("rotating.thing.read", {}, CTX)
        assert result.data == {"used": "two"} and target.built == ["one", "two"]

    async def test_a_failure_with_an_unchanged_credential_is_not_repeated(self, target):
        vault, clock = Vault(**{"app/token": "one"}), Clock()
        connector = self.connector(vault, clock)
        target.accepted = "something-else"
        result = await connector.invoke("rotating.thing.read", {}, CTX)
        assert not result.success and target.built == ["one"], "one attempt, one adapter"

    async def test_an_unreadable_credential_is_a_failed_call_not_a_crash(self, target):
        connector = self.connector(Vault(), Clock())
        result = await connector.invoke("rotating.thing.read", {}, CTX)
        assert not result.success and "credential unavailable" in result.error
        health = await connector.health()
        assert health.status.value == "unavailable"

    async def test_health_follows_the_target(self, target):
        connector = self.connector(Vault(**{"app/token": "one"}), Clock())
        assert (await connector.health()).status.value == "healthy"
        target.accepted = "two"
        assert (await connector.health()).status.value == "unavailable"
        checks = await connector.test_connection()
        assert [c.ok for c in checks] == [False]
