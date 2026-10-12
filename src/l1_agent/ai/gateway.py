"""The only way to a model provider (ADR 0009).

Every request goes through `ModelGateway.ask`, in this order:

1. the tenant's model policy: is the model on, is this task enabled, is this
   provider allowed, with its stated region and retention;
2. redaction of everything that will be sent;
3. the budgets, per incident and per tenant per day, counted from the record
   of earlier calls;
4. the provider, with a time limit;
5. validation of the answer against the task's closed schema;
6. a record of the call and its audit event, with digests and token counts.

`ask` returns the validated answer or None. It never raises and it never
returns free text. A caller that gets None carries on without the model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from src.l1_agent.ai import policy as model_policy
from src.l1_agent.ai.prompts import Prompt
from src.l1_agent.ai.providers import Provider, ToolSpec
from src.l1_agent.ai.redaction import Redactor
from src.l1_agent.ai.schema import check_schema, problems
from src.l1_agent.audit.log import Actor, canonical
from src.l1_agent.db.engine import Database
from src.l1_agent.utils.logging import get_logger
from src.l1_agent.utils.metrics import metrics

logger = get_logger("model_gateway")

ADVISOR = Actor.system("advisor")
MAX_SECTION_CHARS = 4000
MAX_TRUSTED_CHARS = 24_000
# A deliberately high estimate of tokens from characters, for budgets checked before a call.
CHARS_PER_TOKEN = 3


@dataclass(frozen=True)
class Scope:
    """What a request is for: a tenant, and a run when there is one."""

    tenant_id: uuid.UUID
    run_id: Optional[uuid.UUID] = None
    subject: str = ""  # an incident number, or the article a draft is made from


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section(label: str, text: object, redactor: Redactor, limit: int = MAX_SECTION_CHARS) -> str:
    """One piece of untrusted text: redacted, bounded and delimited."""
    cleaned = redactor.redact(text)[:limit]
    cleaned = cleaned.replace("<<<", "< < <").replace(">>>", "> > >")
    return f"{label}: <<<{cleaned}>>>"


def compose(
    trusted: str,
    sections: Iterable[Tuple[str, object]],
    redactor: Redactor,
    limit: int = MAX_SECTION_CHARS,
) -> str:
    """The user message: our own text, then each untrusted section. All of it redacted."""
    parts = [redactor.redact(trusted)[:MAX_TRUSTED_CHARS]] if trusted else []
    parts += [section(label, text, redactor, limit) for label, text in sections]
    return "\n".join(parts)


def estimate_tokens(*texts: str) -> int:
    return sum(len(t) for t in texts) // CHARS_PER_TOKEN + 1


class ModelGateway:
    def __init__(
        self, db: Database, provider: Optional[Provider], *, timeout_seconds: float = 60.0
    ) -> None:
        self._db = db
        self._provider = provider
        self._timeout = timeout_seconds

    @property
    def provider(self) -> Optional[Provider]:
        return self._provider

    def describe(self) -> Dict[str, Any]:
        """The configured provider as the operator declared it. Never the key."""
        p = self._provider
        if p is None:
            return {"configured": False}
        return {
            "configured": True,
            "provider": p.id,
            "model": p.model,
            "region": p.region,
            "retention_days": p.retention_days,
            "priced": bool(p.input_price_per_mtok or p.output_price_per_mtok),
        }

    def advisor(
        self,
        tenant_id: uuid.UUID,
        run_id: Optional[uuid.UUID] = None,
        subject: str = "",
        personal: Iterable[str] = (),
    ) -> Any:
        """The four advisor tasks for one run (or one article), through this gateway."""
        from src.l1_agent.ai.advisor import Advisor

        return Advisor(self, Scope(tenant_id, run_id, subject), Redactor(personal))

    async def close(self) -> None:
        if self._provider is not None:
            await self._provider.close()

    def _cost(self, input_tokens: int, output_tokens: int) -> int:
        # A price in dollars per million tokens is the same number in micro-dollars per token.
        p = self._provider
        return round(
            input_tokens * p.input_price_per_mtok + output_tokens * p.output_price_per_mtok
        )

    async def _refusal(
        self, scope: Scope, prompt: Prompt, needed_tokens: int, needed_cost: int
    ) -> Tuple[Optional[str], str]:
        """(why not, which budget). Reads the policy and the record each time: no cache."""
        async with self._db.tenant(scope.tenant_id) as conn:
            policy = await model_policy.read_policy(conn, scope.tenant_id)
            reason = model_policy.permits(policy, self._provider, prompt.task)
            if reason:
                return reason, ""
            if scope.run_id is not None and await model_policy.stopped(
                conn, scope.tenant_id, scope.run_id
            ):
                return "budget_already_exceeded", ""
            run = (
                await model_policy.spent(conn, scope.tenant_id, run_id=scope.run_id)
                if scope.run_id is not None
                else model_policy.Spent()
            )
            day = await model_policy.spent(conn, scope.tenant_id, since=model_policy.start_of_day())
            over = ""
            if run.tokens + needed_tokens > policy.incident_tokens:
                over = "incident_tokens"
            elif day.tokens + needed_tokens > policy.daily_tokens:
                over = "daily_tokens"
            elif (
                policy.incident_cost_micros is not None
                and run.cost_micros + needed_cost > policy.incident_cost_micros
            ):
                over = "incident_cost"
            elif (
                policy.daily_cost_micros is not None
                and day.cost_micros + needed_cost > policy.daily_cost_micros
            ):
                over = "daily_cost"
            return ("over_budget" if over else None), over

    async def _record(
        self,
        scope: Scope,
        prompt: Prompt,
        *,
        outbound: str,
        answer: Optional[Dict[str, Any]],
        input_tokens: int,
        output_tokens: int,
        outcome: str,
        detail: str = "",
    ) -> None:
        async with self._db.tenant(scope.tenant_id) as conn:
            await model_policy.record_call(
                conn,
                scope.tenant_id,
                ADVISOR,
                run_id=scope.run_id,
                subject=scope.subject,
                task=prompt.task,
                provider=self._provider.id,
                model=self._provider.model,
                prompt_version=prompt.version,
                prompt_digest=prompt.digest,
                input_digest=_sha256(outbound),
                output_digest=_sha256(canonical(answer)) if answer is not None else "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_micros=self._cost(input_tokens, output_tokens),
                outcome=outcome,
                detail=detail,
            )
        metrics.increment("model.calls", labels={"task": prompt.task, "outcome": outcome})

    async def ask(
        self,
        scope: Scope,
        redactor: Redactor,
        prompt: Prompt,
        *,
        trusted: str,
        sections: Sequence[Tuple[str, object]],
        parameters: Mapping[str, Any],
        max_output_tokens: int,
        section_chars: int = MAX_SECTION_CHARS,
    ) -> Optional[Dict[str, Any]]:
        """Ask the model one question. Returns the validated answer, or None."""
        if self._provider is None:
            return None
        try:
            return await self._ask(
                scope,
                redactor,
                prompt,
                trusted,
                sections,
                parameters,
                max_output_tokens,
                section_chars,
            )
        except Exception as exc:  # the advisor must never break a run
            logger.error("Model request failed in the gateway: %s", type(exc).__name__)
            metrics.increment("model.calls", labels={"task": prompt.task, "outcome": "fault"})
            return None

    async def _ask(
        self,
        scope: Scope,
        redactor: Redactor,
        prompt: Prompt,
        trusted: str,
        sections: Sequence[Tuple[str, object]],
        parameters: Mapping[str, Any],
        max_output_tokens: int,
        section_chars: int,
    ) -> Optional[Dict[str, Any]]:
        check_schema(parameters)
        user = compose(trusted, sections, redactor, section_chars)
        tool = ToolSpec(prompt.tool, prompt.tool_description, parameters)
        outbound = "\n".join((prompt.system, user, canonical(dict(parameters))))
        input_estimate = estimate_tokens(outbound)
        reason, budget = await self._refusal(
            scope,
            prompt,
            input_estimate + max_output_tokens,
            self._cost(input_estimate, max_output_tokens),
        )
        if reason == "over_budget":
            logger.warning("Model budget reached (%s); the advisor is off for this run", budget)
            await self._record(
                scope,
                prompt,
                outbound=outbound,
                answer=None,
                input_tokens=0,
                output_tokens=0,
                outcome="over_budget",
                detail=budget,
            )
            return None
        if reason:
            metrics.increment("model.refused", labels={"reason": reason})
            return None

        try:
            completion = await asyncio.wait_for(
                self._provider.complete(prompt.system, user, tool, max_output_tokens),
                timeout=self._timeout,
            )
        except Exception as exc:
            # The request may have been processed and billed: count what was sent.
            await self._record(
                scope,
                prompt,
                outbound=outbound,
                answer=None,
                input_tokens=input_estimate,
                output_tokens=0,
                outcome="error",
                detail=type(exc).__name__,
            )
            return None

        answer = completion.arguments
        found = ["no function call"] if answer is None else problems(parameters, answer)
        answered = json.dumps(answer, default=str) if answer is not None else ""
        await self._record(
            scope,
            prompt,
            outbound=outbound,
            answer=answer if not found else None,
            input_tokens=(
                completion.input_tokens if completion.input_tokens is not None else input_estimate
            ),
            output_tokens=(
                completion.output_tokens
                if completion.output_tokens is not None
                else estimate_tokens(answered)
            ),
            outcome="invalid" if found else "ok",
            # How it was wrong, never what it said.
            detail=f"{len(found)} schema problem(s)" if found else "",
        )
        return None if found else answer
