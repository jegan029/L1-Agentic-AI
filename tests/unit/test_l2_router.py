"""Unit tests for L2Router CI-based team routing."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.l1_agent.escalation.l2_router import L2Router

_ROUTING = {
    "default_team": {"name": "L2-General", "email": "l2-general@example.com"},
    "ci_mappings": {
        "DB*": {"name": "L2-Database", "email": "l2-db@example.com"},
        "APP*": {"name": "L2-AppSupport", "email": "l2-app@example.com"},
        "MQ*": {"name": "L2-Middleware", "email": "l2-mq@example.com"},
    },
}


@pytest.fixture()
def routing_file(tmp_path: Path) -> Path:
    p = tmp_path / "l2_routing.json"
    p.write_text(json.dumps(_ROUTING), encoding="utf-8")
    return p


@pytest.fixture()
def router(routing_file: Path) -> L2Router:
    return L2Router(routing_file)


class TestWildcardMatching:
    def test_db_prefix_matches(self, router: L2Router) -> None:
        team = router.resolve_l2_team("DB-PROD-01")
        assert team["name"] == "L2-Database"
        assert team["email"] == "l2-db@example.com"

    def test_app_prefix_matches(self, router: L2Router) -> None:
        team = router.resolve_l2_team("APP-PAYMENT-SVC")
        assert team["name"] == "L2-AppSupport"

    def test_mq_prefix_matches(self, router: L2Router) -> None:
        team = router.resolve_l2_team("MQ-BROKER-02")
        assert team["name"] == "L2-Middleware"

    def test_case_insensitive_match(self, router: L2Router) -> None:
        team = router.resolve_l2_team("db-prod-01")
        assert team["name"] == "L2-Database"

    def test_exact_prefix_matches(self, router: L2Router) -> None:
        team = router.resolve_l2_team("DB")
        assert team["name"] == "L2-Database"


class TestDefaultFallback:
    def test_unknown_ci_falls_back_to_default(self, router: L2Router) -> None:
        team = router.resolve_l2_team("UNKNOWN-SYSTEM")
        assert team["name"] == "L2-General"
        assert team["email"] == "l2-general@example.com"

    def test_empty_ci_falls_back_to_default(self, router: L2Router) -> None:
        team = router.resolve_l2_team("")
        assert team["name"] == "L2-General"

    def test_none_ci_falls_back_to_default(self, router: L2Router) -> None:
        team = router.resolve_l2_team(None)  # type: ignore[arg-type]
        assert team["name"] == "L2-General"

    def test_whitespace_ci_falls_back_to_default(self, router: L2Router) -> None:
        team = router.resolve_l2_team("   ")
        assert team["name"] == "L2-General"


class TestMissingConfig:
    def test_missing_file_uses_hardcoded_default(self, tmp_path: Path) -> None:
        router = L2Router(tmp_path / "does_not_exist.json")
        team = router.resolve_l2_team("DB-PROD")
        assert team["name"] == "L2-General"

    def test_returns_copy_not_reference(self, router: L2Router) -> None:
        t1 = router.resolve_l2_team("DB-01")
        t2 = router.resolve_l2_team("DB-02")
        t1["name"] = "MUTATED"
        assert t2["name"] == "L2-Database"
