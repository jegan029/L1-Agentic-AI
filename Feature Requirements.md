Feature Requirements (Must Implement)
1) Confidence Threshold Enforcement

When the agent selects an SOP:

If confidence_score is missing OR < CONFIDENCE_THRESHOLD, the ticket must be escalated.
If confidence_score >= CONFIDENCE_THRESHOLD, proceed with execution.

This must be enforced in the decision logic (central place, not duplicated).

2) CI-Based L2 Team Routing (NEW)

When escalating:

Use the ticket’s CI field (configuration item) to determine the correct L2 team.
Routing must be configurable via a mapping file, not hardcoded.

Add a new config file:

📌 backend/config/l2_routing.json

Example format:

{
  "default_team": {
    "name": "L2-General",
    "email": "l2-general@example.com"
  },
  "ci_mappings": {
    "DB*": {
      "name": "L2-Database",
      "email": "l2-db@example.com"
    },
    "APP*": {
      "name": "L2-AppSupport",
      "email": "l2-app@example.com"
    }
  }
}

Rules:

Support wildcard matching (DB*, APP*, etc.).
If no CI match → use default_team.
3) Email Notification (NEW)

When escalation happens:

Send an email to the mapped L2 team email address.
Email must include:
ticket number
short description
CI
predicted SOP (if available)
confidence score
reason for escalation (low confidence / missing SOP / execution failure)

Email config must come from environment variables:

SMTP_HOST
SMTP_PORT
SMTP_USER
SMTP_PASSWORD
SMTP_FROM

If SMTP is missing, escalation must still occur, but log warning.

Implement email sender in:

📌 backend/notifications/email_notifier.py

4) ServiceNow Escalation Update (Existing Integration)

When escalating, ensure the SNOW ticket is updated with:

assignment group / assigned_to / work_notes (whatever the repo currently supports)
status should reflect escalation to L2

If repo already has SNOW update method, reuse it.
If missing, implement minimal SNOW patch update API call.

5) Dashboard Update Event (NEW)

When escalation happens:

Ensure dashboard stream (SSE/websocket) receives an event with:
ticket id
status = "ESCALATED"
routed L2 team name/email
email_sent true/false

This must happen after escalation completes.

Constraints
Do NOT rewrite the system.
Do NOT refactor unrelated modules.
Only implement minimal clean additions.
Reuse existing patterns (logging, config loader, adapters).
Must not break demo mode.
Add type hints where reasonable.
Implementation Steps (Follow Exactly)
Step A — Locate Existing Modules

Scan the repo and identify:

Where ServiceNow tickets are fetched.
Where SOP selection/confidence score is produced.
Where execution vs escalation decision is made.
Where escalation currently happens (if any).
Where dashboard events are emitted.

Before coding, print a short mapping:

file paths
function names
Step B — Implement L2 Routing Engine (NEW)

Create:

📌 backend/escalation/l2_router.py

It must:

load backend/config/l2_routing.json
expose function:
def resolve_l2_team(ci_name: str) -> dict:
    ...

Return:

{"name": "...", "email": "..."}

Implement wildcard matching with fnmatch.

Step C — Implement Email Notifier (NEW)

Create:

📌 backend/notifications/email_notifier.py

Expose:

def send_escalation_email(team_email: str, subject: str, body: str) -> bool:
    ...

Return True if sent, False if failed.

Use smtplib with TLS if possible.

Step D — Update Escalation Flow (Modify Existing Code)

Find the escalation function (or create one if missing):

📌 preferred location: backend/escalation/escalator.py

Implement:

async def escalate_to_l2(ticket, reason: str, sop_name: str | None, confidence: float | None):
    ...

Flow:

resolve team from CI
update SNOW ticket assignment/work_notes
send email
emit dashboard event
persist escalation history if repo supports persistence
Step E — Enforce Confidence Threshold in Decision Logic

Find the decision point where SOP execution happens.

Implement threshold enforcement:

define CONFIDENCE_THRESHOLD in config/env
apply consistently

Example logic:

if confidence is None or confidence < threshold:
    await escalate_to_l2(...)
    return
Step F — Add Unit Tests

Use pytest.

Add tests under:

📌 tests/unit/test_l2_router.py
📌 tests/unit/test_confidence_threshold.py

Test cases:

wildcard mapping works
fallback default works
confidence None escalates
confidence below threshold escalates
confidence above threshold executes

Mock SNOW + SMTP calls.

Output Requirements

When done, output:

1) Summary of changes
2) Patch-style diffs for all modified/created files
3) New env vars required
4) Commands to run tests
Start Now

Scan repo first and identify the correct insertion points. Then implement changes with minimal disruption.