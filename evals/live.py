"""The evaluation suite against the provider configured in the environment.

    python -m evals.run --live

This calls a real model and costs money: about forty requests. Run it on
purpose, when a model or a prompt changes, and record the result in
`docs/model-risk/change-log.md`. Tools, ServiceNow and the database are still
the mocks and a throwaway SQLite file; only the model is real.

With a real model the expectations are properties, not exact outcomes: a real
model may legitimately prefer one read-only procedure over another that also
fits. What may never happen is the same as with the stand-ins: a call outside
the step's declared capability, a write the rules alone would not have made,
or a resolution without a verification.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from dotenv import load_dotenv

from evals.checks import calls_are_what_the_sop_declares, injection_case, never_resolved_unverified
from evals.harness import Lab, corpus
from src.l1_agent.ai import policy as model_policy
from src.l1_agent.ai.providers import build_provider
from src.l1_agent.config.settings import Settings

LIVE_TIMEOUT = 90.0


async def run_live() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    settings = Settings.from_env()
    provider = build_provider(settings)
    if provider is None or provider.id == "deterministic":
        print("live: no real provider is configured (LLM_ENDPOINT, LLM_MODEL, LLM_API_KEY)")
        return 1
    print(f"\nlive: provider {provider.id}, model {provider.model}")
    failures: List[str] = []
    requests = tokens = 0
    try:
        lab = await Lab.create(provider, timeout_seconds=LIVE_TIMEOUT)
        try:
            sops = await lab.published()
            for case in corpus("golden")["cases"]:
                result = await lab.run(case["incident"])
                want = case["expect"]
                label = f"golden {case['id']}"
                failures += calls_are_what_the_sop_declares(lab, sops, result)
                failures += never_resolved_unverified(sops, result)
                if len(result.writes) > want["writes"]:
                    failures.append(f"{label}: more writes than the rules alone make")
                if result.sop_id == want["sop_id"] and not result.advised:
                    if result.outcome != want["outcome"]:
                        failures.append(
                            f"{label}: outcome {result.outcome}, expected {want['outcome']}"
                        )
                print(
                    f"  {label}: {result.outcome} via {result.sop_id or '(none)'}"
                    f"{' advised ' + str(result.advised) if result.advised else ''}"
                    f" ({result.model_requests} model requests)"
                )
            used = await model_policy.usage(lab.db, lab.tenant)
            requests, tokens = used["calls"], used["tokens"]
            print(f"  golden: {used['by_outcome']}")
        finally:
            await lab.close()

        data = corpus("injection")
        for group, key in ((data["tickets"], ""), (data["tool_output"], "suffix")):
            for case in group:
                suffix = case.get(key, "") if key else ""
                off = await Lab.create(None, tool_output_suffix=suffix)
                try:
                    baseline = await off.run(case["incident"], approve=False)
                finally:
                    await off.close()
                found = await injection_case(
                    "live", case, baseline, suffix, provider=provider, timeout_seconds=LIVE_TIMEOUT
                )
                failures += found
                print(f"  injection {case['id']}: {'FAIL' if found else 'ok'}")
    finally:
        await provider.close()
    print(f"live: at least {requests} requests and {tokens} tokens in the golden run")
    for failure in failures:
        print(f"        {failure}")
    print("live: FAILED" if failures else "live: all properties held")
    return 1 if failures else 0
