"""What a runner can check, and what it cannot make (ADR 0010).

A runner is given public keys only. With them it can tell a call the control
plane signed from one it did not, and a grant the policy engine minted from
one it did not. It cannot produce either.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.l1_agent.actions.grants import GrantAuthority
from src.l1_agent.config.settings import ConfigError
from src.l1_agent.connectors.sdk.base import WriteNotAuthorized
from src.l1_agent.connectors.sdk.grant import Grant, GrantCheck, call_digest, grant_body
from src.l1_agent.runner import keys, protocol

SRC = Path(__file__).resolve().parents[2] / "src" / "l1_agent"
FORCE_START = "autosys.job.force_start"
PARAMS = {"job_name": "NIGHTLY_LOAD"}
CALL = dict(
    tenant_id="tenant-1",
    run_id="run-1",
    step_id="step-1",
    capability_id=FORCE_START,
    digest=call_digest(FORCE_START, PARAMS),
)


def mint(authority: GrantAuthority, **changes) -> Grant:
    values = dict(
        decision_id="decision-1",
        tenant_id="tenant-1",
        run_id="run-1",
        step_id="step-1",
        capability_id=FORCE_START,
        call_digest=call_digest(FORCE_START, PARAMS),
    )
    values.update(changes)
    return authority.mint(**values)


class TestAGrantCheckedWithThePublicKey:
    def test_the_runner_accepts_the_policy_engines_grant_once(self):
        authority = GrantAuthority()
        check = GrantCheck(authority.public_key)
        grant = mint(authority)
        check.redeem(grant, **CALL)
        with pytest.raises(WriteNotAuthorized, match="already been used"):
            check.redeem(grant, **CALL)

    def test_a_grant_survives_the_wire_and_nothing_else_becomes_one(self):
        authority = GrantAuthority()
        grant = mint(authority)
        sent = json.loads(protocol.canonical(grant.to_dict()))
        assert Grant.from_dict(sent) == grant
        GrantCheck(authority.public_key).redeem(Grant.from_dict(sent), **CALL)

        for not_a_grant in (
            None,
            "grant",
            [],
            {},
            {**sent, "extra": 1},
            {k: v for k, v in sent.items() if k != "nonce"},
            {**sent, "expires_at": "tomorrow"},
            {**sent, "expires_at": True},
            {**sent, "signature": 7},
        ):
            assert Grant.from_dict(not_a_grant) is None

    def test_a_grant_from_any_other_key_is_refused(self):
        check = GrantCheck(GrantAuthority().public_key)
        with pytest.raises(WriteNotAuthorized, match="not issued by the policy engine"):
            check.redeem(mint(GrantAuthority()), **CALL)

    def test_changing_any_field_is_seen_by_the_runner(self):
        authority = GrantAuthority()
        grant = mint(authority)
        for name, value in (
            ("decision_id", "decision-2"),
            ("tenant_id", "tenant-2"),
            ("run_id", "run-2"),
            ("step_id", "step-2"),
            ("capability_id", "autosys.job.delete"),
            ("call_digest", "0" * 64),
            ("expires_at", grant.expires_at + 3600),
            ("nonce", "other"),
            ("signature", "00" * 64),
            ("signature", "not hex"),
            ("signature", ""),
        ):
            with pytest.raises(WriteNotAuthorized, match="not issued by the policy engine"):
                GrantCheck(authority.public_key).redeem(replace(grant, **{name: value}), **CALL)

    @pytest.mark.parametrize(
        "change, why",
        [
            (dict(tenant_id="tenant-2"), "different step"),
            (dict(run_id="run-2"), "different step"),
            (dict(step_id="step-2"), "different step"),
            (dict(capability_id="autosys.job.delete"), "different step"),
            (dict(digest=call_digest(FORCE_START, {"job_name": "PAYROLL_POST"})), "parameters"),
        ],
    )
    def test_it_is_good_for_no_other_call(self, change, why):
        authority = GrantAuthority()
        with pytest.raises(WriteNotAuthorized, match=why):
            GrantCheck(authority.public_key).redeem(mint(authority), **{**CALL, **change})

    def test_it_expires_by_the_runners_own_clock(self):
        now = [1000.0]
        authority = GrantAuthority(ttl_seconds=60, clock=lambda: now[0])
        grant = mint(authority)
        runner_clock = [1059.0]
        check = GrantCheck(authority.public_key, clock=lambda: runner_clock[0])
        runner_clock[0] = 1060.0
        with pytest.raises(WriteNotAuthorized, match="expired"):
            check.redeem(grant, **CALL)

    def test_a_deployment_key_gives_the_same_public_key_every_time(self):
        pem, public = keys.generate()
        seed = keys.private_seed(pem)
        first, second = GrantAuthority(private_key=seed), GrantAuthority(private_key=seed)
        assert keys.public_text(first.public_key) == public == keys.public_text(second.public_key)
        # A grant minted by one worker is good at a runner that knows the deployment's key.
        GrantCheck(keys.parse_public(public)).redeem(mint(first), **CALL)
        with pytest.raises(ValueError):
            GrantAuthority(private_key=b"short")

    def test_the_public_key_cannot_sign(self):
        """What the runner holds verifies and has no way to mint."""
        authority = GrantAuthority()
        check = GrantCheck(authority.public_key)
        assert not hasattr(check, "mint") and not hasattr(check, "_signer")
        assert authority._key not in authority.public_key
        assert authority.public_key != authority._key and len(authority.public_key) == 32


class TestKeys:
    def test_a_generated_pair_round_trips(self):
        pem, public = keys.generate()
        assert "PRIVATE KEY" in pem and len(keys.parse_public(public)) == 32
        assert keys.public_text(keys.public_of(keys.private_seed(pem))) == public
        assert keys.generate()[1] != public

    @pytest.mark.parametrize("text", ["", "not base64!", "QUJD", "A" * 44 + "=="])
    def test_a_bad_public_key_is_refused(self, text):
        with pytest.raises(ConfigError, match="public key"):
            keys.parse_public(text)

    def test_a_bad_or_wrong_kind_of_private_key_is_refused(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        other = (
            ec.generate_private_key(ec.SECP256R1())
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        # Built in pieces so that a secret scanner does not take this file for a key.
        marker = "PRIVATE " + "KEY"
        not_a_key = f"-----BEGIN {marker}-----\nnope\n-----END {marker}-----"
        for text in ("", not_a_key, other):
            with pytest.raises(ConfigError, match="private key"):
                keys.private_seed(text)

    def test_a_signature_is_good_for_one_purpose_only(self):
        pem, public = keys.generate()
        seed, public_key = keys.private_seed(pem), keys.parse_public(public)
        message = b"the same bytes"
        as_call = keys.Signer(seed, protocol.CALL_DOMAIN).sign(message)
        assert keys.verify(public_key, protocol.CALL_DOMAIN, message, as_call)
        assert not keys.verify(public_key, protocol.REQUEST_DOMAIN, message, as_call)
        assert not keys.verify(public_key, protocol.CALL_DOMAIN, message + b"!", as_call)
        assert not keys.verify(
            keys.parse_public(keys.generate()[1]), protocol.CALL_DOMAIN, message, as_call
        )
        for junk in ("", "zz", None, 5):
            assert not keys.verify(public_key, protocol.CALL_DOMAIN, message, junk)

    def test_a_key_that_signs_calls_cannot_be_used_to_forge_a_grant(self):
        """Even if one key were used for both, a call's signature is not a grant's."""
        pem, public = keys.generate()
        seed = keys.private_seed(pem)
        authority = GrantAuthority(private_key=seed)
        grant = mint(authority)
        forged = keys.Signer(seed, protocol.CALL_DOMAIN).sign(grant_body(grant))
        with pytest.raises(WriteNotAuthorized, match="not issued by the policy engine"):
            GrantCheck(keys.parse_public(public)).redeem(replace(grant, signature=forged), **CALL)


class TestTheRunnerPackageCannotMint:
    """A search of the source tree, like the one that pins where a grant is minted."""

    def test_nothing_under_runner_builds_an_authority_or_holds_the_grant_key(self):
        for path in (SRC / "runner").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            assert "GrantAuthority" not in names | attributes, path.name
            assert "mint" not in attributes, path.name
            imported = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
                for alias in node.names
                if node.module.endswith("actions.grants")
            }
            assert imported == set(), f"{path.name} imports from actions.grants"

    def test_the_runner_process_is_not_configured_with_a_private_grant_key(self):
        source = (SRC / "runner" / "agent.py").read_text(encoding="utf-8")
        assert "grant_public_key" in source
        assert "grant_key)" not in source and "ACTIONS_GRANT_KEY" not in source
