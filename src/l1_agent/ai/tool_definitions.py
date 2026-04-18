"""Tool definitions for LLM function calling.

These definitions describe the available tools that the LLM can invoke
during incident analysis and SOP execution. Each tool maps to an adapter
in the system (Splunk, IR360/MQ, Autosys, Windows share, ServiceNow).
"""

from __future__ import annotations

from typing import Any, Dict, List

# ── Tool definitions in OpenAI function-calling format ────────────────

SPLUNK_SEARCH_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "splunk_search",
        "description": (
            "Execute a Splunk search query to find application logs, errors, "
            "or events. Use this to investigate log-based evidence for incidents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Splunk SPL search query. Example: "
                        "'index=app_logs sourcetype=log4j \"PaymentService\" ERROR'"
                    ),
                },
                "time_range": {
                    "type": "object",
                    "description": "Time range for the search.",
                    "properties": {
                        "earliest": {
                            "type": "string",
                            "description": "Earliest time (e.g. '-2h', '-1d').",
                        },
                        "latest": {
                            "type": "string",
                            "description": "Latest time (e.g. 'now').",
                        },
                    },
                },
            },
            "required": ["query"],
        },
    },
}

MQ_CHECK_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mq_check",
        "description": (
            "Check IBM MQ queue status via IR360. Supports checking queue depth, "
            "queue status, and browsing messages. Use this for MQ-related incidents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queue_manager": {
                    "type": "string",
                    "description": "Name of the queue manager (e.g. 'QMPROD01').",
                },
                "queue": {
                    "type": "string",
                    "description": "Name of the queue (e.g. 'PAYMENT.REQUEST').",
                },
                "action": {
                    "type": "string",
                    "enum": ["depth", "status", "browse"],
                    "description": "Action to perform: depth (check queue depth), status (check queue status), browse (peek at messages).",
                },
            },
            "required": ["queue_manager", "queue", "action"],
        },
    },
}

FILE_CHECK_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "file_check",
        "description": (
            "Read log files from Windows shared folders (read-only). "
            "Use this to inspect application or system log files on network shares."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "unc_path": {
                    "type": "string",
                    "description": "UNC path to the file (e.g. '//fileserver/logs/app.log').",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to filter log lines (e.g. 'ERROR|FATAL').",
                },
                "last_n_lines": {
                    "type": "integer",
                    "description": "Number of most recent lines to return.",
                },
            },
            "required": ["unc_path"],
        },
    },
}

AUTOSYS_STATUS_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "autosys_status",
        "description": (
            "Check Autosys job status (read-only). Use this to check if "
            "batch jobs are running, failed, or succeeded."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_name": {
                    "type": "string",
                    "description": "Name of the Autosys job or box (e.g. 'BATCH_PAYMENT_PROCESS').",
                },
                "query_type": {
                    "type": "string",
                    "enum": ["status", "dependencies"],
                    "description": "Type of query: status (current job status) or dependencies (job dependency chain).",
                },
            },
            "required": ["job_name"],
        },
    },
}

POST_WORK_NOTE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "post_work_note",
        "description": (
            "Post a work note to the ServiceNow incident. Use this to update "
            "the incident with findings, status updates, or evidence summaries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "The work note text to post to the incident.",
                },
            },
            "required": ["note"],
        },
    },
}

ESCALATE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "escalate_to_l2",
        "description": (
            "Escalate the incident to L2 support. Use this when: the issue "
            "requires write/change actions, confidence is low, access is denied, "
            "results are ambiguous, or the SOP requires approval. Include a "
            "summary of all checks performed and why escalation is needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Clear reason for escalation.",
                },
                "findings_summary": {
                    "type": "string",
                    "description": "Summary of all checks performed and their results.",
                },
                "recommended_actions": {
                    "type": "string",
                    "description": "Recommended next steps for L2.",
                },
            },
            "required": ["reason", "findings_summary"],
        },
    },
}

DYNATRACE_VM_CHECK_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "dynatrace_vm_check",
        "description": (
            "Check VM/host health using Dynatrace. Queries the Dynatrace "
            "Entities API and Problems API to verify that a VM is healthy, "
            "running, and has no active problems."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Name of the host/VM to check (e.g. 'app-server-01').",
                },
                "host_group": {
                    "type": "string",
                    "description": "Optional host group filter.",
                },
            },
            "required": ["host_name"],
        },
    },
}

DYNATRACE_METRICS_CHECK_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "dynatrace_metrics_check",
        "description": (
            "Query Dynatrace metrics (CPU, memory, etc.) to verify they are "
            "within normal thresholds. Use this to check if resource utilisation "
            "has returned to normal after an incident."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host_name": {
                    "type": "string",
                    "description": "Host name to filter metrics for.",
                },
                "metric_selector": {
                    "type": "string",
                    "description": (
                        "Dynatrace metric selector. Default: "
                        "'builtin:host.cpu.usage,builtin:host.mem.usage'."
                    ),
                },
                "time_range": {
                    "type": "string",
                    "description": "Relative time range (e.g. 'now-1h', 'now-2h').",
                },
            },
            "required": ["host_name"],
        },
    },
}

WEB_UI_CHECK_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_ui_check",
        "description": (
            "Check a web application URL by navigating to it and optionally "
            "performing click-path steps. Verifies the page loads correctly, "
            "expected elements/text are present, and no errors are detected. "
            "Use this for Application URL checks and GCAS Launcher checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to navigate to and check.",
                },
                "check_type": {
                    "type": "string",
                    "enum": ["page_load", "click_path", "element_check"],
                    "description": (
                        "Type of check: page_load (verify page loads), "
                        "click_path (execute click steps), "
                        "element_check (verify specific element exists)."
                    ),
                },
                "expected_text": {
                    "type": "string",
                    "description": "Text expected to appear on the page.",
                },
                "click_steps": {
                    "type": "array",
                    "description": "List of click-path steps for click_path check.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["click", "wait", "assert_text", "assert_element"],
                            },
                            "selector": {
                                "type": "string",
                                "description": "CSS selector for the element.",
                            },
                            "value": {
                                "type": "string",
                                "description": "Value for wait (seconds) or assert_text.",
                            },
                            "name": {
                                "type": "string",
                                "description": "Human-readable step name.",
                            },
                        },
                    },
                },
            },
            "required": ["url"],
        },
    },
}

MAINFRAME_ASYNC_CHECK_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "mainframe_async_check",
        "description": (
            "Check mainframe MF BEIM ASYNC job status via TN3270 terminal "
            "emulator. Connects to the mainframe, navigates to the BEIM status "
            "screen, and checks if jobs show the expected status (typically "
            "'inact ok' for successful completion)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_name": {
                    "type": "string",
                    "description": "Name of the mainframe job to check.",
                },
                "expected_status": {
                    "type": "string",
                    "description": "Expected job status (default: 'inact ok').",
                },
                "action": {
                    "type": "string",
                    "enum": ["async_status", "job_status", "screen_check"],
                    "description": (
                        "Action: async_status (check BEIM ASYNC jobs), "
                        "job_status (check specific job), "
                        "screen_check (capture screen content)."
                    ),
                },
            },
            "required": ["job_name"],
        },
    },
}

RESOLVE_INCIDENT_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "resolve_incident",
        "description": (
            "Mark the incident as resolved with a resolution summary. "
            "Only use this when the SOP has been fully executed and the "
            "issue is confirmed resolved based on evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "resolution_summary": {
                    "type": "string",
                    "description": "Summary of what was found and how the issue was resolved.",
                },
                "evidence_summary": {
                    "type": "string",
                    "description": "Key evidence collected during investigation.",
                },
            },
            "required": ["resolution_summary"],
        },
    },
}


def get_investigation_tools() -> List[Dict[str, Any]]:
    """Return all tool definitions available during SOP execution."""
    return [
        SPLUNK_SEARCH_TOOL,
        MQ_CHECK_TOOL,
        FILE_CHECK_TOOL,
        AUTOSYS_STATUS_TOOL,
        DYNATRACE_VM_CHECK_TOOL,
        DYNATRACE_METRICS_CHECK_TOOL,
        WEB_UI_CHECK_TOOL,
        MAINFRAME_ASYNC_CHECK_TOOL,
        POST_WORK_NOTE_TOOL,
        ESCALATE_TOOL,
        RESOLVE_INCIDENT_TOOL,
    ]


def get_sop_selection_tools() -> List[Dict[str, Any]]:
    """Return tool definitions for SOP selection phase."""
    return [
        {
            "type": "function",
            "function": {
                "name": "select_sop",
                "description": (
                    "Select the most appropriate SOP for this incident. "
                    "Provide your reasoning for the selection."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "sop_id": {
                            "type": "string",
                            "description": "The ID of the selected SOP.",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Confidence score from 0.0 to 1.0.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Reasoning for why this SOP matches the incident.",
                        },
                    },
                    "required": ["sop_id", "confidence", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "escalate_no_sop",
                "description": (
                    "Escalate when no suitable SOP matches the incident."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why no SOP matches.",
                        },
                    },
                    "required": ["reason"],
                },
            },
        },
    ]
