"""Check the Splunk connection the agent is configured with.

Builds the connector exactly as the service does (same settings, same guard,
same backend), then: checks health, lists the MCP server's tools (MCP
backend), runs one guarded search, and confirms that a search outside the
allow list is refused before it reaches Splunk. Prints a pass or fail summary
and no secret.

    python scripts/splunk_check.py                 # the configured backend
    python scripts/splunk_check.py --backend rest  # the other one, same settings
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.l1_agent.config.settings import ConfigError, Settings  # noqa: E402
from src.l1_agent.connectors.hub import environment_connections  # noqa: E402
from src.l1_agent.connectors.ops.splunk import SplunkConnector  # noqa: E402
from src.l1_agent.connectors.sdk.base import PURPOSE_TEST, Refused, StepContext  # noqa: E402
from src.l1_agent.connectors.sdk.secrets import (  # noqa: E402
    ConnectionSecrets,
    SecretError,
    SecretResolver,
)

CONTEXT = StepContext(None, "splunk-check", "check", PURPOSE_TEST)


async def check(backend: Optional[str]) -> List[Tuple[str, bool, str]]:
    lines: List[Tuple[str, bool, str]] = []
    settings = Settings.from_env()
    if backend:
        settings = dataclasses.replace(
            settings, splunk=dataclasses.replace(settings.splunk, backend=backend)
        )
    connection = environment_connections(settings).get("splunk")
    if connection is None:
        return [("configuration", False, "Splunk is not configured (or SPLUNK_BACKEND=mock)")]
    config = dict(connection.config)
    connector = SplunkConnector(
        config,
        ConnectionSecrets(SecretResolver.from_settings(settings.secrets), connection.secret_refs),
        demo_mode=settings.agent.demo_mode,
    )
    name = SplunkConnector._backend_of(config)
    target = config.get("mcp_url" if name == "mcp" else "base_url", "")
    indexes = list(config.get("allowed_indexes") or [])
    tls = "verified" if config.get("verify_ssl", True) else "NOT VERIFIED (demo mode only)"
    print(f"backend: {name}   target: {target}   TLS: {tls}")
    print(
        f"allowed indexes: {', '.join(indexes) or '(none)'}   "
        f"max lookback: {config.get('max_lookback')}   max results: {config.get('max_results')}"
    )
    print(f"credentials given: {', '.join(connector.secrets.slots) or '(none)'}")
    lines.append(
        ("configuration", bool(indexes), "" if indexes else "SPLUNK_ALLOWED_INDEXES is empty")
    )

    try:
        health = await connector.health()
        lines.append(("health", health.status.value == "healthy", health.reason))

        if name == "mcp" and health.status.value == "healthy":
            adapter = await connector._adapter()
            tools = await adapter._backend.list_tool_names()
            wanted = config.get("mcp_search_tool") or "splunk_run_query"
            print(f"MCP tools ({len(tools)}): {', '.join(tools)}")
            lines.append((f"search tool '{wanted}' is offered", wanted in tools, ""))

        if indexes:
            query = f"index={indexes[0]} | head 1"
            result = await connector.invoke(
                "splunk.search", {"query": query, "time_range": {"earliest": "-24h"}}, CONTEXT
            )
            detail = (
                f"{result.audit.get('spl')} -> {result.data.get('result_count')} result(s) "
                f"in {result.audit.get('duration_ms')} ms"
                if result.success
                else result.error
            )
            lines.append(("guarded search", result.success, detail))

        refused = await connector.invoke(
            "splunk.search", {"query": "index=_internal | delete"}, CONTEXT
        )
        lines.append(
            (
                "a search outside the allow list is refused",
                (not refused.success) and refused.audit.get("outcome") == "rejected",
                refused.error,
            )
        )
    except Refused as exc:
        lines.append(("guarded search", False, str(exc)))
    finally:
        await connector.close()
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backend", choices=("mcp", "rest"), help="check this backend instead")
    args = parser.parse_args(argv)
    try:
        lines = asyncio.run(check(args.backend))
    except (ConfigError, SecretError) as exc:
        lines = [("configuration", False, str(exc))]
    print()
    for label, ok, detail in lines:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{': ' + detail if detail else ''}")
    passed = all(ok for _, ok, _ in lines)
    print(f"\n{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
