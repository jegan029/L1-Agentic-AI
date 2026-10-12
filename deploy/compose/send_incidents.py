"""Send the sample incidents to the demo's webhook. DEMO ONLY.

The demo's webhook is unsigned, which demo mode allows on loopback and nowhere else.
"""

from __future__ import annotations

import json
import os
import urllib.request

from src.l1_agent.demo import ACTION_DEMO_INCIDENTS, DEMO_INCIDENTS


def main() -> None:
    url = os.environ["WEBHOOK_URL"]
    for incident in DEMO_INCIDENTS + ACTION_DEMO_INCIDENTS:
        request = urllib.request.Request(
            url,
            data=json.dumps(incident).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            answer = json.loads(response.read())
        print(f"{incident['number']}: {answer['status']}")


if __name__ == "__main__":
    main()
