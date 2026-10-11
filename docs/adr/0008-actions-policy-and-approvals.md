# ADR 0008: Actions, policy and approvals

Status: accepted (Phase 4)

## Context

Until Phase 4 the agent could only read. A run ended `RESOLVED` only when the symptom had gone away by itself. Buyers want the agent to carry out the low-risk fix an SOP describes (the reference case is an Autosys force start), and they want to control, per tenant, how far it may go without a person.

A write to a production system is the highest-risk thing the product does. The design has to make three things true by construction, not by convention:

1. No write happens without a recorded policy decision.
2. An approval is for one exact action. It cannot be reused for another, or for the same action with different parameters.
3. A person can stop all writes at once.

Decisions D5 and D8 (see `docs/delivery-plan.md`) are fixed: T1 is the highest tier that may run without a human approval, and approvals in ServiceNow go through a change request created from a standard change template.

## Decision

### One new step type

An SOP step of type `ACTION` names a write capability and its parameters. It is the only way an SOP can change anything. The validator rejects an `ACTION` that names a read capability, an unknown one, or parameters outside the capability's closed schema, and rejects an SOP that has an `ACTION` step but names no change template.

A passed action does not count as verification for `RESOLVE`. The rule from ADR 0002 is unchanged: `RESOLVED` is entered only from a `VERIFY` step that passed, with only notes in between.

### The pipeline

`actions/service.py` runs every `ACTION` step through the same stages:

1. **Decide.** `actions/policy.py → evaluate` is a pure function over the request and the tenant's rules. The checks run in a fixed order and the first refusal wins: kill switch, deployment ceiling, change freeze, permitted hours, connector allow list, rate limit, then autonomy. The result is one of deny, shadow, recommend, require approval, allow. The decision is written to the `actions` table and the audit log in one transaction before anything else happens.
2. **Change record.** For "require approval" and "allow", a standard change is opened from the template the SOP names. If that fails the decision becomes a denial.
3. **Approval**, when required. The run is suspended (`AWAITING_APPROVAL`) and its job is parked. An approval or rejection, or the expiry of the request, wakes it.
4. **Pre-checks.** The read checks the capability's manifest declares, each with an assertion.
5. **Dry run**, where the capability supports one.
6. **Authorise.** One database update moves the row from `approved` or `allowed` to `executing`. Only if that update changes a row is a grant minted. The kill switch is read again in the same step.
7. **Execute** through `Connector.invoke`, which redeems the grant.
8. **Verify** with the capability's declared verify operation, polling within declared bounds.
9. **Roll back** when verification fails and the manifest declares a rollback, then escalate.

### Tiers and autonomy

A capability's manifest sets its tier. Tenant rules may raise the tier for named targets and can never lower it.

| Autonomy | What an `ACTION` step does |
|---|---|
| `shadow` | Records what it would have done. The run continues as if the step had been skipped. |
| `recommend` | Posts the proposed action to the ticket and escalates. |
| `approval` | Asks for approval, and executes when it is given. |
| `automatic` | Executes without approval, only for T1 and only when the SOP, capability and target are on the tenant's automatic list. Anything else asks for approval. |

The effective level is the lowest of three: the tenant's level, the level set for the SOP version that is running, and the deployment ceiling (`ACTIONS_MAX_AUTONOMY`). The first two default to `shadow`. The SOP level is recorded against the version's content hash, so a newly published version starts in `shadow` again.

**T2 always needs an approval**, whatever the autonomy level (D5). For T2 the approver must not be the author of the SOP version.

### Grants: the decision token

`Connector.invoke` refuses a write capability unless it is given a grant, and the connector has been given a verifier to check it with. A grant is an HMAC-signed record naming the tenant, run, step, capability and a digest of the exact parameters. It lasts 60 seconds and is redeemed once.

The signing key is random per process and lives in `actions/grants.py → GrantAuthority`. The only code that calls `mint` is `ActionService._authorise`, and it does so only after the database update in stage 6 succeeds. `tests/unit/actions/test_write_needs_a_decision.py` proves this three ways: every write capability in the registry is refused without a grant; a forged, expired, reused or mismatched grant is refused; and a search of the source tree finds `mint` called nowhere else.

A grant is minted and redeemed in the same process. When the runner is split out in Phase 6 the key becomes an asymmetric pair, with the private half in the control plane.

### Approvals

An approval request is the `actions` row: capability, rendered parameters, their hash, the SOP version hash, the incident and an expiry. The hash covers the tenant, the incident, the SOP version hash, the capability and the parameters.

- The approver submits the hash they were shown. A different hash is refused.
- When the run resumes, the engine renders the step again and recomputes the hash. If it differs from the approved one the approval is void and the run escalates.
- An approval given after the request expires is refused. An approval that is not acted on within `execute_within_minutes` is void.
- Database constraints repeat the rules: an approved row carries an approver and an approved hash equal to the action hash, and for T2 the approver differs from the SOP author. On PostgreSQL a trigger makes the capability, parameters, hash and author of a row unchangeable, and prevents the tier being lowered.

Two channels:

- **The product UI**, for a user with the `approver` role (`actions.approve`).
- **ServiceNow**, when the tenant enables it: the agent adds approval requests for the members of a configured group to the change request and polls them. The change request carries the action hash in `correlation_id`; an approval is accepted only if the change still carries that hash. For T2 the approver's email must be known and differ from the SOP author's; otherwise the approval is not accepted.

### Kill switch

There are three flags, and any one of them stops writes:

| Flag | Where | Who sets it |
|---|---|---|
| Tenant | `policies.kill_switch` | Operator or tenant admin engages (`actions.stop`); only the tenant admin releases (`policy.manage`) |
| Global | `platform_flags` | The platform operator, with `l1-agent killswitch on --global` and the migration role |
| Local | The file named by `ACTIONS_KILL_SWITCH_FILE` | Whoever runs the process |

The flags are read from the database every time, with no cache. They are read when the policy decides, again in the update that authorises execution, and again when the connector redeems the grant. A run that meets the switch ends `ABORTED` and the ticket goes to a person.

Engaging the switch is deliberately easier than releasing it.

`platform_flags` is the second table without a tenant column (the first is `tenants`). The application role can read it and cannot write it.

### Who may do what

| Permission | Roles | Allows |
|---|---|---|
| `actions.approve` | approver | Approve or reject an action |
| `actions.stop` | operator, tenant admin | Engage the tenant kill switch |
| `policy.manage` | tenant admin | Change autonomy and rules; release the kill switch |

A tenant admin can raise autonomy and cannot approve. An approver cannot change policy. An SOP author cannot approve a T2 action from their own SOP. `test_role_table_keeps_duties_apart` pins these.

### Resuming

An `ACTION` step is journalled like any other, and the `actions` row is its own journal within the step. A resumed run finds the row and continues from its status. A row found in `executing` means the worker died between sending the write and recording its result. The engine cannot know whether the write happened, so it never sends it again. It runs the verification read first: if the target shows the result the action was for, the run goes on. If not, the run ends `NEEDS_HUMAN`, and nothing is rolled back, because undoing something that may not have been done would be a second change nobody asked for. A row found in `executed` only has its verification repeated.

One execution of a capability with the same parameters is allowed per incident (a unique index), so a second run for the same ticket cannot force start the job again.

## Engineering choices made here

- **Hours are in UTC.** A time zone database is not guaranteed in the container image.
- **Rules are edited as JSON** through the API or `l1-agent policy set`. The UI sets the autonomy level and the kill switch and shows the rules read only.
- **Autosys force start uses the argument list** (`sendevent -E FORCE_STARTJOB -J <job>` through `create_subprocess_exec`). The Autosys REST API is not implemented.
- **A change is closed on a best-effort basis.** If the instance refuses the close, a note is left on the change and the refusal is audited; the run's outcome is not affected.

## Not done

- An existing approved normal change supplied on the ticket is not accepted as the change record. Only a standard change opened by the agent is.
- ServiceNow approvals are polled. There is no signed webhook for them.
- No operator "stop this run" action. The kill switch and the approval expiry are the ways to stop a waiting run.
- The pre-check that a job's box is not on hold: `autorep` output is not parsed for it.
- A ServiceNow scoped application (deferred by D8).

## Consequences

- The transition table gains four states (`POLICY_CHECK`, `AWAITING_APPROVAL`, `ACTING`, `ROLLING_BACK`). None of them has an edge to `VERIFIED` or `RESOLVED`, and the table search in `test_no_unverified_resolve.py` covers them.
- A run can now be open for as long as an approval request lives. Open runs hold the one-open-run-per-incident slot.
- Everything on the ServiceNow change and approval side is written from vendor documentation and is unverified until it has run against an instance.
