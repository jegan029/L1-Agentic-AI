"""Seed a Splunk lab with the events the sample SOPs search for.

Events are sent to the HTTP Event Collector with their indexed metadata set
(index, host, source, sourcetype), so each sample SOP's search matches as it
is written. Timestamps are relative to now. This is for a lab: never point it
at a Splunk anyone relies on.

    python scripts/seed_splunk.py --all
    python scripts/seed_splunk.py --scenario autosys

Reads SPLUNK_HEC_URL (default https://localhost:8088), SPLUNK_HEC_TOKEN,
SPLUNK_CA_BUNDLE and SPLUNK_VERIFY_SSL from the environment or `.env`. The
token is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

JOB = "BATCH_PAYMENT_PROCESS"
HEALTHY_JOB = "BATCH_SETTLEMENT_EOD"

# (seconds before now, index, host, source, sourcetype, text)
_Event = Tuple[int, str, str, str, str, str]

SCENARIOS: Dict[str, List[_Event]] = {
    # SOP-AUTOSYS-001: index=batch_logs source=autosys "<job>" (FAILURE OR ERROR), last 4h
    "autosys": [
        (900, "batch_logs", "batch-server-01", "autosys", "autosys",
         f"INFO  job={JOB} event=STARTING machine=batch-server-01"),
        (780, "batch_logs", "batch-server-01", "autosys", "autosys",
         f"ERROR job={JOB} step=load_payments message=\"Connection refused: paydb01:1521\""),
        (770, "batch_logs", "batch-server-01", "autosys", "autosys",
         f"ERROR job={JOB} event=FAILURE status=FA exit_code=1 message=\"Job terminated with exit code 1\""),
        (760, "batch_logs", "batch-server-01", "autosys", "autosys",
         f"WARN  job={JOB} event=ALARM alarm=JOBFAILURE status=FA"),
    ],
    # SOP memory: index=app_logs host=app-server-prod01 ("OutOfMemoryError" OR ...), last 2h
    "oom": [
        (1500, "app_logs", "app-server-prod01", "/var/log/app/gc.log", "log4j",
         "WARN  [GC] Full GC (Allocation Failure) 7921M->7903M(8192M), 4.81 secs"),
        (1200, "app_logs", "app-server-prod01", "/var/log/app/app.log", "log4j",
         "ERROR [OrderService] java.lang.OutOfMemoryError: Java heap space"),
        (1190, "app_logs", "app-server-prod01", "/var/log/app/app.log", "log4j",
         "ERROR [OrderService] Exception in thread \"http-nio-8080-exec-7\" java.lang.OutOfMemoryError: GC overhead limit exceeded"),
        (600, "app_logs", "app-server-prod01", "/var/log/messages", "syslog",
         "kernel: Out of memory: OOM Killer terminated process 4121 (java) total-vm:9437184kB"),
    ],
    # SOP MQ: index=app_logs sourcetype=log4j host=app-server-* "PaymentService" (ERROR OR FATAL), last 2h
    "mq": [
        (1000, "app_logs", "app-server-01", "/var/log/payment-service/app.log", "log4j",
         "ERROR [PaymentService] Connection timeout to MQ broker mqprod01:1414"),
        (940, "app_logs", "app-server-01", "/var/log/payment-service/app.log", "log4j",
         "WARN  [PaymentService] Retry 1/3 for queue PAYMENT.REQUEST"),
        (880, "app_logs", "app-server-01", "/var/log/payment-service/app.log", "log4j",
         "ERROR [PaymentService] All retries exhausted for PAYMENT.REQUEST, consumer stopped"),
    ],
    # Nothing here matches any SOP search: no failure words, another job, another host.
    "healthy": [
        (900, "batch_logs", "batch-server-02", "autosys", "autosys",
         f"INFO  job={HEALTHY_JOB} event=STARTING machine=batch-server-02"),
        (300, "batch_logs", "batch-server-02", "autosys", "autosys",
         f"INFO  job={HEALTHY_JOB} event=SUCCESS status=SU exit_code=0"),
        (600, "app_logs", "web-server-02", "/var/log/settlement-service/app.log", "log4j",
         "INFO  [SettlementService] Heartbeat ok, heap 41% used"),
        (240, "app_logs", "web-server-02", "/var/log/settlement-service/app.log", "log4j",
         "INFO  [SettlementService] Processed 1200 messages from SETTLEMENT.REQUEST"),
    ],
}  # fmt: skip


def events_for(scenario: str, run_id: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """The HEC events of one scenario. Each carries `seed_run=<id>` so a test can find its own."""
    now = time.time() if now is None else now
    return [
        {
            "time": round(now - ago, 3),
            "index": index,
            "host": host,
            "source": source,
            "sourcetype": sourcetype,
            "event": f"{text} seed_run={run_id}",
        }
        for ago, index, host, source, sourcetype, text in SCENARIOS[scenario]
    ]


def _verify() -> Any:
    if os.environ.get("SPLUNK_VERIFY_SSL", "true").lower() == "false":
        return False
    return os.environ.get("SPLUNK_CA_BUNDLE") or True


def seed(
    scenarios: Sequence[str],
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
    run_id: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Send the scenarios. Returns {"run_id": ..., "sent": {scenario: count}}. Raises on failure."""
    url = (url or os.environ.get("SPLUNK_HEC_URL") or "https://localhost:8088").rstrip("/")
    token = token or os.environ.get("SPLUNK_HEC_TOKEN", "")
    if not token:
        raise RuntimeError("SPLUNK_HEC_TOKEN is not set")
    if token.lower().startswith("splunk "):
        token = token[7:].strip()
    run_id = run_id or uuid.uuid4().hex[:12]
    owned = client is None
    client = client or httpx.Client(verify=_verify(), timeout=20)
    sent: Dict[str, int] = {}
    try:
        for scenario in scenarios:
            events = events_for(scenario, run_id)
            response = client.post(
                f"{url}/services/collector/event",
                headers={"Authorization": f"Splunk {token}"},
                content="\n".join(json.dumps(event) for event in events),
            )
            if response.status_code != 200:
                detail = response.text.replace(token, "<redacted>")[:200]
                raise RuntimeError(f"HEC answered HTTP {response.status_code}: {detail}")
            sent[scenario] = len(events)
    finally:
        if owned:
            client.close()
    return {"run_id": run_id, "sent": sent}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--scenario", choices=sorted(SCENARIOS), help="seed one scenario")
    which.add_argument("--all", action="store_true", help="seed all four scenarios")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    if os.environ.get("SPLUNK_VERIFY_SSL", "true").lower() == "false":
        print("WARNING: TLS verification is off for this run (SPLUNK_VERIFY_SSL=false)")
    try:
        result = seed(sorted(SCENARIOS) if args.all else [args.scenario])
    except (RuntimeError, httpx.HTTPError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    for scenario, count in result["sent"].items():
        print(f"seeded {scenario}: {count} event(s)")
    print(f"seed_run={result['run_id']}  (events are searchable a few seconds after this)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
