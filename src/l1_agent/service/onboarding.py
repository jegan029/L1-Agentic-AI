"""Where a tenant is in getting set up.

Nothing here is stored. Each step is read from what exists now: role
mappings, connections, enrolled runners, published SOPs, the policy and the
runs. A step is done when the thing it names is there, so the list cannot say
"done" for something that was later removed.

The order is the order to work in. A new tenant ends in shadow: it records
what it would do and changes nothing until an administrator raises the level.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List

from src.l1_agent.actions import store as action_store
from src.l1_agent.auth import store as auth_store
from src.l1_agent.connectors.sdk.manifest import Family
from src.l1_agent.runner import store as runner_store
from src.l1_agent.service import runs as run_service
from src.l1_agent.sop import store as sop_store


def _step(
    step_id: str, title: str, done: bool, detail: str, how: str, *, required: bool = True
) -> Dict[str, Any]:
    return {
        "id": step_id,
        "title": title,
        "done": bool(done),
        "required": required,
        "detail": detail,
        "how": how,
    }


async def status(ctx: Any, tenant_id: uuid.UUID) -> Dict[str, Any]:
    """The set-up steps for this tenant, in order, with what is and is not in place."""
    db = ctx.db
    steps: List[Dict[str, Any]] = []

    # 1. Who can sign in, and as what.
    mappings = await auth_store.list_role_mappings(db, tenant_id)
    provider = "OpenID Connect" if ctx.oidc else "SAML" if ctx.saml else ""
    if ctx.demo_open:
        steps.append(
            _step(
                "sign_in",
                "Sign-in and roles",
                True,
                "Demo mode: one built-in user with every role",
                "",
            )
        )
    else:
        steps.append(
            _step(
                "sign_in",
                "Sign-in and roles",
                bool(provider and mappings),
                (
                    f"{provider}; {len(mappings)} group-to-role mapping(s)"
                    if provider
                    else "No identity provider is configured for this deployment"
                ),
                "Configure OIDC_* or SAML_* for the deployment, then map each identity "
                "provider group to a role with `l1-agent roles grant` or the API.",
            )
        )

    # 2. Where incidents come from and where notes go.
    if ctx.itsm is None:
        connected, itsm_detail = False, "No ServiceNow connection is configured"
    elif hasattr(ctx.itsm, "reason"):
        itsm_detail = await ctx.itsm.reason()
        connected, itsm_detail = not itsm_detail, itsm_detail or "Connected"
    else:
        connected, itsm_detail = True, "Demo mode: built-in stand-in"
    steps.append(
        _step(
            "itsm",
            "ServiceNow",
            connected,
            itsm_detail,
            "Enter the instance, the integration user and the intake group under Ticketing on "
            "the Set-up page, then test the connection.",
        )
    )

    # 3. The runner, when calls are made by one.
    remote = bool(ctx.hub is not None and ctx.hub.remote)
    if remote:
        runners = await runner_store.list_runners(db, tenant_id)
        enrolled = [r for r in runners if not r["revoked"]]
        live = [r for r in enrolled if r["connected"]]
        steps.append(
            _step(
                "runner",
                "Runner",
                bool(live),
                f"{len(enrolled)} enrolled, {len(live)} connected",
                "Make a key pair where the runner will run (`l1-agent runner keygen`), enrol "
                "its public key under Runners on the Set-up page, and start the runner.",
            )
        )

    # 4. What it can look at, and 5. where SOPs come from.
    described = await ctx.hub.describe(tenant_id) if ctx.hub is not None else []
    operations = [
        c for c in described if c["family"] == Family.OPERATIONS.value and c["configured"]
    ]
    knowledge = [c for c in described if c["family"] == Family.KNOWLEDGE.value and c["configured"]]
    steps.append(
        _step(
            "connectors",
            "Operations connectors",
            bool(operations),
            ", ".join(sorted(c["display_name"] for c in operations)) or "None connected",
            "Connect the systems your SOPs check (for example Splunk and Autosys) under "
            "Operations on the Set-up page. Each takes a reference to its credential, never "
            "the credential.",
        )
    )
    steps.append(
        _step(
            "knowledge",
            "SOP source",
            bool(knowledge),
            ", ".join(sorted(c["display_name"] for c in knowledge)) or "None connected",
            "Connect a ServiceNow knowledge base or a Git repository to bring articles in as "
            "drafts. Optional: SOPs can also be written here.",
            required=False,
        )
    )

    # 6. Something to run. Only a published version is ever matched.
    published = await sop_store.list_versions(db, tenant_id, status="published")
    waiting = await sop_store.list_versions(db, tenant_id, status="in_review")
    steps.append(
        _step(
            "sop",
            "A published SOP",
            bool(published),
            f"{len(published)} published, {len(waiting)} waiting for review",
            "An author submits a draft; a different person approves and publishes it.",
        )
    )

    # 7. How far it may go. Unset means shadow, and that has to be someone's decision.
    policy = await action_store.get_policy(db, tenant_id)
    steps.append(
        _step(
            "policy",
            "Autonomy level",
            policy.updated_at is not None,
            f"{policy.autonomy.value}" + ("" if policy.updated_at else " (the default; never set)"),
            "Confirm the level on the Actions page. Start in shadow: it records what it would "
            "do and changes nothing.",
        )
    )

    # 8. Proof that the whole path works.
    records = await run_service.recent_records(db, tenant_id)
    steps.append(
        _step(
            "first_run",
            "A first incident",
            bool(records),
            f"{len(records)} run(s) recorded",
            "Assign a test incident to the intake group and watch it on the dashboard.",
        )
    )

    required = [s for s in steps if s["required"]]
    return {
        "steps": steps,
        "complete": all(s["done"] for s in required),
        "done": sum(1 for s in required if s["done"]),
        "total": len(required),
        "runner_mode": "remote" if remote else "embedded",
    }
