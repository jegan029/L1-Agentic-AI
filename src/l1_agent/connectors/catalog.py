"""The first-party connectors, and the names SOPs written before the SDK use for them."""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, Mapping, Optional

from src.l1_agent.adapters.base import BaseAdapter
from src.l1_agent.connectors.itsm.servicenow import ServiceNowConnector
from src.l1_agent.connectors.knowledge.git import GitKnowledge
from src.l1_agent.connectors.knowledge.servicenow_kb import ServiceNowKnowledge
from src.l1_agent.connectors.ops.autosys import AutosysConnector
from src.l1_agent.connectors.ops.browser import BrowserConnector
from src.l1_agent.connectors.ops.dynatrace import DynatraceConnector
from src.l1_agent.connectors.ops.fileshare import FileShareConnector
from src.l1_agent.connectors.ops.ibm_mq import IbmMqConnector
from src.l1_agent.connectors.ops.mainframe import MainframeConnector
from src.l1_agent.connectors.ops.splunk import SplunkConnector
from src.l1_agent.connectors.sdk.registry import ConnectorSet, Registry, connectors_from_adapters

# Only these are loaded. A third-party connector is a later decision (ADR 0005).
FIRST_PARTY = (
    ServiceNowConnector,
    ServiceNowKnowledge,
    GitKnowledge,
    SplunkConnector,
    DynatraceConnector,
    IbmMqConnector,
    AutosysConnector,
    FileShareConnector,
    BrowserConnector,
    MainframeConnector,
)

# Step types from before capabilities had ids. An SOP may use either form.
STEP_CAPABILITIES: Dict[str, str] = {
    "SPLUNK_SEARCH": "splunk.search",
    "MQ_CHECK": "ibm_mq.queue.check",
    "FILE_CHECK": "fileshare.file.read",
    "AUTOSYS_STATUS": "autosys.job.status",
    "DYNATRACE_VM_CHECK": "dynatrace.host.health",
    "DYNATRACE_METRICS": "dynatrace.metrics.query",
    "WEB_UI_CHECK": "browser.page.check",
    "MAINFRAME_CHECK": "mainframe.screen.check",
}

# The original adapter keys, still used for mocks, settings and circuit breakers.
ADAPTER_CONNECTORS: Dict[str, str] = {
    "splunk": "splunk",
    "ir360": "ibm_mq",
    "windows_share": "fileshare",
    "autosys": "autosys",
    "dynatrace": "dynatrace",
    "webui": "browser",
    "mainframe": "mainframe",
}
CONNECTOR_ADAPTERS: Dict[str, str] = {v: k for k, v in ADAPTER_CONNECTORS.items()}


@lru_cache(maxsize=1)
def default_registry() -> Registry:
    return Registry(FIRST_PARTY)


def capability_for(step_type: str, capability: str = "") -> Optional[str]:
    """The capability id a check names: its own `capability`, or its legacy step type."""
    if capability:
        return capability
    return STEP_CAPABILITIES.get((step_type or "").upper())


def connectors_for_adapters(
    adapters: Mapping[str, BaseAdapter],
    settings: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> ConnectorSet:
    return connectors_from_adapters(default_registry(), adapters, ADAPTER_CONNECTORS, settings)
