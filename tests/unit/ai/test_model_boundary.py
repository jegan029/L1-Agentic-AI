"""Structural proof that the gateway is the only way to a model provider (ADR 0009).

A search of the source tree, in the manner of
tests/unit/actions/test_write_needs_a_decision.py. Adding a second caller of a
provider, or building a provider client somewhere else, fails here on purpose.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set

SRC = Path(__file__).resolve().parents[3] / "src" / "l1_agent"
PROVIDER_CLASSES = {"AnthropicProvider", "OpenAICompatibleProvider", "LLMClient"}


def trees() -> Dict[str, ast.AST]:
    return {
        path.relative_to(SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8"))
        for path in SRC.rglob("*.py")
    }


def calls(tree: ast.AST) -> List[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def name_of(call: ast.Call) -> str:
    target = call.func
    return target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")


def imported(tree: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
    return names


def test_only_the_gateway_asks_a_provider_to_complete():
    callers = {
        path
        for path, tree in trees().items()
        if any(name_of(call) == "complete" for call in calls(tree))
    }
    assert callers == {"ai/gateway.py"}, callers


def test_provider_clients_are_built_in_one_place():
    builders: Dict[str, Set[str]] = {}
    for path, tree in trees().items():
        found = {name_of(call) for call in calls(tree)} & PROVIDER_CLASSES
        if found:
            builders[path] = found
    assert builders == {
        # build_provider, and the OpenAI-compatible provider wrapping its HTTP client.
        "ai/providers.py": {"AnthropicProvider", "OpenAICompatibleProvider", "LLMClient"},
    }, builders


def test_the_service_never_uses_the_older_ungoverned_analyzer():
    """`AIAnalyzer` talks to a client directly. It remains for unit tests and the
    one-shot demo's fallback; nothing the service runs may build one."""
    users = {
        path
        for path, tree in trees().items()
        if any(name_of(call) == "AIAnalyzer" for call in calls(tree))
    }
    assert users == {"demo.py"}, users
    for path in ("main.py", "api/app.py", "service/runs.py", "cli.py", "sop/ingest.py"):
        modules = imported(trees()[path])
        assert not any("analyzer" in m or "llm_client" in m or "mock_llm" in m for m in modules), (
            path
        )


def test_only_the_model_package_opens_a_connection_to_a_model_endpoint():
    for path, tree in trees().items():
        if path.startswith("ai/"):
            continue
        modules = imported(tree)
        assert "src.l1_agent.ai.llm_client.LLMClient" not in modules or path in (
            "engine/incident_processor.py",  # a type for the older optional argument
            "demo.py",
        ), path


def test_the_gateway_redacts_checks_policy_and_budget_before_it_calls():
    """The order inside `_ask`: compose (which redacts), refusal, then the provider."""
    tree = trees()["ai/gateway.py"]
    ask = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ask"
    )
    order = [
        (node.lineno, name_of(node))
        for node in ast.walk(ask)
        if isinstance(node, ast.Call)
        and name_of(node) in {"check_schema", "compose", "_refusal", "complete", "problems"}
    ]
    names = [name for _, name in sorted(order)]
    assert names == ["check_schema", "compose", "_refusal", "complete", "problems"], names


def test_ask_has_no_way_to_return_text():
    """`ask` returns the validated arguments or None; there is no free-text path."""
    source = (SRC / "ai" / "gateway.py").read_text(encoding="utf-8")
    assert ".content" not in source and ".ask(" not in source
    providers = (SRC / "ai" / "providers.py").read_text(encoding="utf-8")
    assert "response.content" not in providers, "a model's text is never read"


def test_nothing_a_model_says_is_logged():
    for name in ("gateway.py", "advisor.py", "providers.py", "policy.py"):
        tree = ast.parse((SRC / "ai" / name).read_text(encoding="utf-8"))
        for call in calls(tree):
            if name_of(call) not in {"info", "warning", "error", "debug", "exception"}:
                continue
            passed = {
                n.id for arg in call.args[1:] for n in ast.walk(arg) if isinstance(n, ast.Name)
            }
            assert not passed & {"answer", "completion", "arguments", "user", "outbound"}, (
                name,
                call.lineno,
            )
