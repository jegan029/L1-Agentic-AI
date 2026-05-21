"""CrewAI agent definitions for the L1 incident resolution pipeline.

Call build_agents(crewai_settings) to get the four agents. Agents are
created fresh per call so that LLM credentials are picked up at runtime
rather than at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.l1_agent.config.settings import CrewAISettings


def build_agents(crewai_settings: "CrewAISettings") -> dict:
    """Build and return the four CrewAI agents as a dict keyed by role name."""
    from crewai import Agent, LLM

    from src.l1_agent.crew.tools import (
        autosys_status,
        dynatrace_metrics,
        dynatrace_vm_health,
        file_check,
        get_sop_details,
        list_sops,
        mainframe_check,
        mq_check,
        splunk_search,
        web_ui_check,
    )

    llm_kwargs: dict = {
        "model": f"anthropic/{crewai_settings.model}",
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    if crewai_settings.api_key:
        llm_kwargs["api_key"] = crewai_settings.api_key

    llm = LLM(**llm_kwargs)

    triage_agent = Agent(
        role="L1 Incident Triage Specialist",
        goal=(
            "Read and parse the incoming ServiceNow incident. Extract every relevant "
            "detail: incident number, category, CMDB CI, assignment group, priority, "
            "short description, and a concise bullet-list of symptoms. Produce a "
            "structured triage report that the SOP analyst can act on immediately."
        ),
        backstory=(
            "You are a senior IT operations analyst with ten years of experience "
            "reading ServiceNow tickets. You know how to cut through vague descriptions "
            "and extract the signal from the noise — the exact host, service, error "
            "pattern, and urgency level that downstream engineers need to start their "
            "investigation. You never guess; you report exactly what the ticket says."
        ),
        tools=[],
        llm=llm,
        verbose=crewai_settings.verbose,
        max_iter=crewai_settings.max_iter,
    )

    review_agent = Agent(
        role="SOP Analyst and Investigation Planner",
        goal=(
            "Select the most appropriate SOP for the incident. Call list_sops to see "
            "all available runbooks, then call get_sop_details on the top one or two "
            "candidates. Return the winning SOP ID, its title, your confidence score "
            "(0.0–1.0), and a one-paragraph rationale. If no SOP matches with "
            "confidence ≥ 0.6, clearly state that L2 escalation is required and why."
        ),
        backstory=(
            "You are a senior IT operations engineer who wrote most of the runbooks "
            "used by the L1 team. You have deep knowledge of how incidents map to "
            "SOPs — the right keywords, CI names, assignment groups, and tool "
            "signatures. You pick the single best SOP and explain your reasoning "
            "so the investigator knows exactly which steps to run."
        ),
        tools=[list_sops, get_sop_details],
        llm=llm,
        verbose=crewai_settings.verbose,
        max_iter=crewai_settings.max_iter,
    )

    resolution_agent = Agent(
        role="L1 Technical Investigator",
        goal=(
            "Execute the SOP investigation steps using the available monitoring and "
            "infrastructure tools. Follow the SOP step order: run each tool call, "
            "interpret the result, and decide the next action based on what you find. "
            "At the end produce a clear outcome — RESOLVED, ESCALATED, or "
            "NEEDS_REVIEW — with a structured evidence summary and step-by-step "
            "findings so the resolver can take the correct ServiceNow action."
        ),
        backstory=(
            "You are a skilled infrastructure engineer with hands-on experience in "
            "Dynatrace, Splunk, IBM MQ, Autosys, and mainframe operations. You know "
            "that a 92% memory reading in Dynatrace combined with OOM events in "
            "Splunk means escalation, while a 75% CPU spike that self-corrected means "
            "you can resolve. You call every relevant tool, interpret results critically, "
            "and never claim resolution without evidence."
        ),
        tools=[
            dynatrace_vm_health,
            dynatrace_metrics,
            splunk_search,
            mq_check,
            autosys_status,
            file_check,
            web_ui_check,
            mainframe_check,
        ],
        llm=llm,
        verbose=crewai_settings.verbose,
        max_iter=crewai_settings.max_iter,
    )

    resolver_agent = Agent(
        role="ServiceNow Resolution Specialist",
        goal=(
            "Take the final action in ServiceNow based on the investigation outcome. "
            "If RESOLVED: compose a detailed work note with all evidence and mark the "
            "incident resolved. If ESCALATED: compose findings, escalation reason, and "
            "recommended L2 actions, then mark for escalation. Always end your response "
            "with the exact line 'OUTCOME: RESOLVED' or 'OUTCOME: ESCALATED' so the "
            "system can parse your decision."
        ),
        backstory=(
            "You are a ServiceNow power-user responsible for keeping incident records "
            "accurate and actionable. You write clear, factual work notes that L2 "
            "engineers can read and act on immediately. You never resolve an incident "
            "without evidence, and you never escalate without a reason and recommended "
            "next steps."
        ),
        tools=[],
        llm=llm,
        verbose=crewai_settings.verbose,
        max_iter=crewai_settings.max_iter,
    )

    return {
        "triage": triage_agent,
        "review": review_agent,
        "resolution": resolution_agent,
        "resolver": resolver_agent,
    }
