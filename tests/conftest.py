"""Shared test fixtures."""

from __future__ import annotations

import pytest

from src.l1_agent.config.settings import Settings


@pytest.fixture
def demo_settings() -> Settings:
    """Settings configured for demo/test mode."""
    return Settings.from_env()
