"""Browser check: load a page on an allowed host and look for text or an element.

Preview. It drives a local Chrome through Selenium, which is not in the
default image. ADR 0005 wants the browser in its own sandboxed container
before this is relied on.
"""

from __future__ import annotations

from typing import Mapping

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.adapters.webui_scraper_adapter import WebUIScraperAdapter
from src.l1_agent.config.settings import WebUISettings
from src.l1_agent.connectors.sdk.base import AdapterConnector
from src.l1_agent.connectors.sdk.manifest import (
    AllowListBinding,
    Capability,
    Family,
    Kind,
    Manifest,
    RiskTier,
)
from src.l1_agent.connectors.sdk.schema import closed


class BrowserConnector(AdapterConnector):
    manifest = Manifest(
        id="browser",
        version="0.9.0",
        family=Family.OPERATIONS,
        display_name="Browser check",
        preview=True,
        notes="Needs Chrome and chromedriver; not installed in the default image.",
        config=closed(
            {
                "allowed_domains": {"type": "array"},
                "page_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                "chrome_binary_path": {"type": "string"},
            }
        ),
        capabilities=(
            Capability(
                id="browser.page.check",
                kind=Kind.READ,
                tier=RiskTier.T0,
                summary="Load an https page on an allowed host and check its content",
                parameters=closed(
                    {
                        "url": {"type": "string", "maxLength": 2000},
                        "check_type": {
                            "type": "string",
                            "enum": ["page_load", "click_path", "element_check"],
                        },
                        "click_steps": {"type": "array"},
                        "expected_text": {"type": "string", "maxLength": 500},
                        "expected_element": {"type": "string", "maxLength": 500},
                        "screenshot": {"type": "boolean"},
                    },
                    required=["url"],
                ),
                scopes=("http.get",),
                # The adapter matches the exact host; the SDK refuses an empty list first.
                allow_lists=(AllowListBinding("url", "allowed_domains", "custom"),),
                target_parameters=("url",),
                timeout_seconds=60,
            ),
        ),
    )

    def _build(self, secrets: Mapping[str, str]) -> BaseAdapter:
        return WebUIScraperAdapter(
            WebUISettings(
                page_timeout_seconds=int(self.config.get("page_timeout_seconds", 30)),
                allowed_domains=list(self.config.get("allowed_domains") or []),
                chrome_binary_path=self.config.get("chrome_binary_path", ""),
            )
        )
