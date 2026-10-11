"""A worker in its own process, for test_worker_killed.py. Not a test module.

usage: worker_child.py <database url> <tenant id> <call log> <die on call N, or 0>

Works one job. Every adapter call is appended to the call log before it is made.
On call N the process exits at once with os._exit, as if it had been killed:
no exception handlers, no cleanup, no commit.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from src.l1_agent.adapters.base import AdapterResult, BaseAdapter
from src.l1_agent.db.engine import Database
from src.l1_agent.demo import build_mock_adapters
from tests.conftest import make_service

KILLED = 137


class LoggingAdapter(BaseAdapter):
    def __init__(self, inner: BaseAdapter, name: str, log_path: str, die_on: int) -> None:
        self._inner, self._name, self._log, self._die_on = inner, name, log_path, die_on

    @property
    def adapter_name(self) -> str:
        return self._inner.adapter_name

    async def execute(self, params: dict) -> AdapterResult:
        with open(self._log, "a", encoding="utf-8") as f:
            f.write(self._name + "\n")
        with open(self._log, encoding="utf-8") as f:
            made = sum(1 for _ in f)
        if made == self._die_on:
            os._exit(KILLED)
        return await self._inner.execute(params)

    async def health_check(self) -> bool:
        return True


async def main(url: str, tenant: str, log_path: str, die_on: int) -> int:
    db = Database.create(url, demo_mode=url.startswith("sqlite"))
    adapters = {
        name: LoggingAdapter(adapter, name, log_path, die_on)
        for name, adapter in build_mock_adapters().items()
    }
    # The dying worker holds its lease for one second; the survivor uses the default.
    service = make_service(db, adapters=adapters, lease_seconds=1 if die_on else 300)
    try:
        worked = await service.work_once([uuid.UUID(tenant)])
    finally:
        await db.close()
    return 0 if worked else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))))
