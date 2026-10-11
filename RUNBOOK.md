# L1 Engineer Agent: runbook

How to run and operate the service as it stands after Phase 1. Installation and the demo quick start are in [README.md](README.md); every setting is listed with comments in [.env.example](.env.example).

This is pre-release software. Run it against production systems only with that understood: state is lost on restart, there is one shared credential set, and only the ServiceNow client has been exercised against a real instance.

## 1. Run it

| Mode | Command | Talks to |
|---|---|---|
| One-shot demo | `python -m src.l1_agent.demo` | Nothing. Five sample incidents, all mocks. |
| Demo service | `AGENT_DEMO_MODE=true LLM_ENABLED=false python -m src.l1_agent.main` | Nothing. Loopback only, no credentials needed. |
| Demo service with real ServiceNow | add `SERVICENOW_LIVE_IN_DEMO=true` and the `SERVICENOW_*` settings | ServiceNow for real; tool adapters stay mocked. |
| Full service | `python -m src.l1_agent.main` | Everything configured. |

Containers:

```bash
docker build -t l1-agent .                         # production image
docker build --target demo -t l1-agent-demo .      # one-shot demo image
docker run --rm l1-agent-demo
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 l1-agent
```

The image runs as a non-root user and listens on all interfaces inside the container, so publish the port deliberately and put TLS termination in front of it.

## 2. Before the first start outside demo mode

The service checks its configuration and refuses to start if any of these is missing or unsafe.

| Check | Setting |
|---|---|
| API bearer tokens, 32 or more characters each | `AGENT_API_TOKENS` |
| Webhook signing secret, 32 or more characters | `AGENT_WEBHOOK_SECRET` |
| ServiceNow URL (https), user, password | `SERVICENOW_BASE_URL`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` |
| The one group the poller takes work from | `SERVICENOW_ASSIGNMENT_GROUP` |
| TLS verification left on | `*_VERIFY_SSL`, `MAINFRAME_USE_TLS` |

Generate a token with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## 3. Connect ServiceNow

1. Create a service account with the `itil` role. It needs to read and update incidents and read the knowledge table.
2. Create the intake assignment group and set `SERVICENOW_ASSIGNMENT_GROUP` to its exact name. The poller takes only incidents that are New and assigned to that group.
3. Create every L2 group named in your routing file. An escalation to a group that does not exist on the instance will not land with a team.
4. Set `SERVICENOW_CLOSE_CODE` to a value in your instance's incident *Resolution code* list. A stock instance refuses the Resolved state without one and silently discards a value that is not in the list. If the update is refused, the run is recorded as `needs_human` with ServiceNow's reason.
5. Check the connection: `pytest tests/live -m live_servicenow -v -s`. This creates incidents tagged `[L1-AGENT-TEST]` in the intake group and leaves them there; do not run it against production.

For better SOP matching, populate the CI and category on incidents and list the same values in each SOP. Matching works without them, on ticket text and assignment group, but with less to go on.

Webhook senders must sign each request as described in the README. An unsigned or late request gets `401`.

## 4. Health and monitoring

| Route | Use |
|---|---|
| `GET /health` | Liveness. Returns status only, no credentials needed. |
| `GET /api/status` | Mode and a metrics snapshot. Bearer token. |
| `GET /metrics` | Prometheus text format. Bearer token. |

Logs are JSON on standard output. Useful counters include `servicenow.incidents_polled`, `servicenow.incident_updates` and `http.denied{reason=...}`.

## 5. SOPs

In demo mode the service loads the JSON files in `data/sample_sops/`. Outside demo mode it reads the ServiceNow knowledge table (`SERVICENOW_SOP_TABLE`); an article that is prose, not the JSON schema, becomes a draft and is never matched or run. Loading from a real knowledge base is untested.

An SOP that fails validation is not loaded, and the log says why. The schema and an example are in the README under *Writing an SOP*.

## 6. What to do when

| Symptom | Likely cause | Action |
|---|---|---|
| Service exits at start with `SecurityConfigError` or `ConfigError` | A required setting from section 2 is missing or unsafe | The message names the setting. |
| Every API call returns `401` | Missing or wrong bearer token | Send `Authorization: Bearer <token>`. For the dashboard, set `L1_API_TOKEN` in `frontend/.env`. |
| Webhook returns `401` | Bad signature, clock skew over five minutes, or a replayed request | Check the secret and the sender's clock. |
| ServiceNow calls return `401` | Wrong credentials, locked account, or a hibernating developer instance | Sign in to the instance in a browser with the same account. |
| Incidents are never picked up | Not in state New, or not in the intake group | Check the ticket and `SERVICENOW_ASSIGNMENT_GROUP`. |
| Run ends `escalated` with "Low confidence SOP match" | No SOP reached the threshold | The work note shows the score and which signals were compared. Add keywords, or CI and category, to the SOP. |
| Run ends `needs_human` with "rejected the Resolved update" | Invalid resolution code, or a business rule on the instance | Fix `SERVICENOW_CLOSE_CODE`; the reason from ServiceNow is in the work note. |
| Run ends `needs_human` with an unbound parameter | The ticket text did not contain what the SOP extracts | Correct the ticket, or widen the SOP's extraction pattern. |
| Run ends `needs_human` after an adapter error | Target system down, or its circuit breaker is open | Check the target; the breaker resets after `AGENT_CB_RESET_SECONDS`. |
| Incident accepted but never processed | The service restarted mid-run | Reassign or re-send the incident. In-flight work is not persisted. |

## 7. Data the service keeps

| File | Content | Notes |
|---|---|---|
| `data/incident_history.jsonl` | The last 1,000 runs with their evidence | Read back at start. Contains ticket text. |
| `data/resolution_memory.jsonl` | Outcome per SOP, used to adjust match scores | |

Both are plain files with no retention control and no integrity protection. Protect the directory, and do not treat either as an audit record. To clear history, stop the service and delete the file.

## 8. Known operational limits

- A restart loses in-flight incidents and the record of which incidents were already handled, so an incident still New in the intake group will be picked up again.
- One process only. Two instances polling the same group will both take the same incident.
- Tokens and the webhook secret are shared and are rotated by changing the setting and restarting.
- The service does not serve TLS.
