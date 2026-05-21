"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _csv(val: str) -> List[str]:
    return [v.strip() for v in val.split(",") if v.strip()]


@dataclass(frozen=True)
class ServiceNowSettings:
    base_url: str = ""
    username: str = ""
    password: str = ""
    poll_interval_seconds: int = 30
    assignment_group: str = ""
    sop_table: str = "kb_knowledge"
    incident_table: str = "incident"


@dataclass(frozen=True)
class SplunkSettings:
    base_url: str = ""
    token: str = ""
    verify_ssl: bool = True
    search_timeout_seconds: int = 120


@dataclass(frozen=True)
class IR360Settings:
    base_url: str = ""
    api_key: str = ""
    cli_path: str = ""
    use_cli: bool = False


@dataclass(frozen=True)
class AutosysSettings:
    cli_path: str = "autorep"
    allowed_commands: tuple = ("autorep",)


@dataclass(frozen=True)
class WindowsShareSettings:
    allowed_unc_prefixes: List[str] = field(default_factory=list)
    smb_username: str = ""
    smb_password: str = ""
    smb_domain: str = ""


@dataclass(frozen=True)
class DynatraceSettings:
    """Dynatrace API configuration for VM health and metrics checks."""

    base_url: str = ""
    api_token: str = ""  # loaded from secret manager in prod
    verify_ssl: bool = True
    timeout_seconds: int = 30


@dataclass(frozen=True)
class WebUISettings:
    """Web UI scraper configuration for application URL and GCAS checks."""

    page_timeout_seconds: int = 30
    allowed_domains: List[str] = field(default_factory=list)
    chrome_binary_path: str = ""


@dataclass(frozen=True)
class MainframeSettings:
    """Mainframe TN3270 configuration for BEIM ASYNC status checks."""

    host: str = ""
    port: int = 23
    allowed_transactions: List[str] = field(default_factory=list)
    default_navigation: List[dict] = field(default_factory=list)
    timeout_seconds: int = 30


@dataclass(frozen=True)
class LLMSettings:
    """LLM / AI endpoint configuration.

    The API key should be stored in a secrets vault in production.
    Set LLM_API_KEY via environment variable or mount it from your
    vault provider (e.g. AWS Secrets Manager, HashiCorp Vault).
    """

    endpoint: str = ""
    api_key: str = ""  # loaded from secret manager in prod
    model: str = "gpt-4"
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout_seconds: int = 60
    enabled: bool = True


@dataclass(frozen=True)
class SecretsSettings:
    """Secrets backend configuration.

    SECRETS_BACKEND: 'env' (default), 'vault', or 'aws'
    """

    backend: str = "env"
    vault_addr: str = ""
    vault_token: str = ""
    vault_role_id: str = ""
    vault_secret_id: str = ""
    vault_mount: str = "secret"
    vault_path_prefix: str = "l1-agent"
    aws_region: str = "us-east-1"
    aws_ssm_prefix: str = "/l1-agent"


@dataclass(frozen=True)
class SmtpSettings:
    """SMTP configuration for escalation email notifications."""

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    from_address: str = ""


@dataclass(frozen=True)
class AgentSettings:
    sop_confidence_threshold: float = 0.6
    max_concurrent_incidents: int = 5
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 1.0
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: int = 60
    demo_mode: bool = False
    log_level: str = "INFO"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080


@dataclass(frozen=True)
class CrewAISettings:
    """CrewAI multi-agent framework configuration.

    Set CREWAI_ENABLED=true to route incidents through the four-agent pipeline
    (TriageAgent → ReviewAgent → ResolutionAgent → ResolverAgent) instead of
    the existing rule-based / AI-executor path.
    """

    enabled: bool = False
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    verbose: bool = False
    max_iter: int = 15


@dataclass(frozen=True)
class Settings:
    servicenow: ServiceNowSettings = field(default_factory=ServiceNowSettings)
    splunk: SplunkSettings = field(default_factory=SplunkSettings)
    ir360: IR360Settings = field(default_factory=IR360Settings)
    autosys: AutosysSettings = field(default_factory=AutosysSettings)
    windows_share: WindowsShareSettings = field(default_factory=WindowsShareSettings)
    dynatrace: DynatraceSettings = field(default_factory=DynatraceSettings)
    webui: WebUISettings = field(default_factory=WebUISettings)
    mainframe: MainframeSettings = field(default_factory=MainframeSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    secrets: SecretsSettings = field(default_factory=SecretsSettings)
    smtp: SmtpSettings = field(default_factory=SmtpSettings)
    crewai: CrewAISettings = field(default_factory=CrewAISettings)

    @classmethod
    def from_env(cls) -> "Settings":
        from dotenv import load_dotenv
        load_dotenv()
        return cls(
            servicenow=ServiceNowSettings(
                base_url=os.getenv("SERVICENOW_BASE_URL", ""),
                username=os.getenv("SERVICENOW_USERNAME", ""),
                password=os.getenv("SERVICENOW_PASSWORD", ""),
                poll_interval_seconds=int(os.getenv("SERVICENOW_POLL_INTERVAL", "30")),
                assignment_group=os.getenv("SERVICENOW_ASSIGNMENT_GROUP", ""),
                sop_table=os.getenv("SERVICENOW_SOP_TABLE", "kb_knowledge"),
                incident_table=os.getenv("SERVICENOW_INCIDENT_TABLE", "incident"),
            ),
            splunk=SplunkSettings(
                base_url=os.getenv("SPLUNK_BASE_URL", ""),
                token=os.getenv("SPLUNK_TOKEN", ""),
                verify_ssl=os.getenv("SPLUNK_VERIFY_SSL", "true").lower() == "true",
                search_timeout_seconds=int(os.getenv("SPLUNK_SEARCH_TIMEOUT", "120")),
            ),
            ir360=IR360Settings(
                base_url=os.getenv("IR360_BASE_URL", ""),
                api_key=os.getenv("IR360_API_KEY", ""),
                cli_path=os.getenv("IR360_CLI_PATH", ""),
                use_cli=os.getenv("IR360_USE_CLI", "false").lower() == "true",
            ),
            autosys=AutosysSettings(
                cli_path=os.getenv("AUTOSYS_CLI_PATH", "autorep"),
                allowed_commands=tuple(
                    _csv(os.getenv("AUTOSYS_ALLOWED_COMMANDS", "autorep"))
                ),
            ),
            windows_share=WindowsShareSettings(
                allowed_unc_prefixes=_csv(
                    os.getenv("WINDOWS_SHARE_ALLOWED_PREFIXES", "")
                ),
                smb_username=os.getenv("SMB_USERNAME", ""),
                smb_password=os.getenv("SMB_PASSWORD", ""),
                smb_domain=os.getenv("SMB_DOMAIN", ""),
            ),
            dynatrace=DynatraceSettings(
                base_url=os.getenv("DYNATRACE_BASE_URL", ""),
                api_token=os.getenv("DYNATRACE_API_TOKEN", ""),
                verify_ssl=os.getenv("DYNATRACE_VERIFY_SSL", "true").lower() == "true",
                timeout_seconds=int(os.getenv("DYNATRACE_TIMEOUT", "30")),
            ),
            webui=WebUISettings(
                page_timeout_seconds=int(os.getenv("WEBUI_PAGE_TIMEOUT", "30")),
                allowed_domains=_csv(os.getenv("WEBUI_ALLOWED_DOMAINS", "")),
                chrome_binary_path=os.getenv("WEBUI_CHROME_BINARY", ""),
            ),
            mainframe=MainframeSettings(
                host=os.getenv("MAINFRAME_HOST", ""),
                port=int(os.getenv("MAINFRAME_PORT", "23")),
                allowed_transactions=_csv(
                    os.getenv("MAINFRAME_ALLOWED_TRANSACTIONS", "")
                ),
                timeout_seconds=int(os.getenv("MAINFRAME_TIMEOUT", "30")),
            ),
            llm=LLMSettings(
                endpoint=os.getenv("LLM_ENDPOINT", ""),
                api_key=os.getenv("LLM_API_KEY", ""),
                model=os.getenv("LLM_MODEL", "gpt-4"),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
                timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
                enabled=os.getenv("LLM_ENABLED", "true").lower() == "true",
            ),
            agent=AgentSettings(
                sop_confidence_threshold=float(
                    os.getenv("AGENT_SOP_CONFIDENCE_THRESHOLD", "0.6")
                ),
                max_concurrent_incidents=int(
                    os.getenv("AGENT_MAX_CONCURRENT_INCIDENTS", "5")
                ),
                retry_max_attempts=int(os.getenv("AGENT_RETRY_MAX_ATTEMPTS", "3")),
                retry_base_delay_seconds=float(
                    os.getenv("AGENT_RETRY_BASE_DELAY", "1.0")
                ),
                circuit_breaker_failure_threshold=int(
                    os.getenv("AGENT_CB_FAILURE_THRESHOLD", "5")
                ),
                circuit_breaker_reset_seconds=int(
                    os.getenv("AGENT_CB_RESET_SECONDS", "60")
                ),
                demo_mode=os.getenv("AGENT_DEMO_MODE", "false").lower() == "true",
                log_level=os.getenv("AGENT_LOG_LEVEL", "INFO"),
                webhook_host=os.getenv("AGENT_WEBHOOK_HOST", "0.0.0.0"),
                webhook_port=int(os.getenv("AGENT_WEBHOOK_PORT", "8080")),
            ),
            secrets=SecretsSettings(
                backend=os.getenv("SECRETS_BACKEND", "env"),
                vault_addr=os.getenv("VAULT_ADDR", "http://127.0.0.1:8200"),
                vault_token=os.getenv("VAULT_TOKEN", ""),
                vault_role_id=os.getenv("VAULT_ROLE_ID", ""),
                vault_secret_id=os.getenv("VAULT_SECRET_ID", ""),
                vault_mount=os.getenv("VAULT_MOUNT", "secret"),
                vault_path_prefix=os.getenv("VAULT_PATH_PREFIX", "l1-agent"),
                aws_region=os.getenv("AWS_REGION", "us-east-1"),
                aws_ssm_prefix=os.getenv("AWS_SSM_PREFIX", "/l1-agent"),
            ),
            smtp=SmtpSettings(
                host=os.getenv("SMTP_HOST", ""),
                port=int(os.getenv("SMTP_PORT", "587")),
                user=os.getenv("SMTP_USER", ""),
                password=os.getenv("SMTP_PASSWORD", ""),
                from_address=os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")),
            ),
            crewai=CrewAISettings(
                enabled=os.getenv("CREWAI_ENABLED", "false").lower() == "true",
                model=os.getenv("CREWAI_MODEL", "claude-sonnet-4-6"),
                api_key=os.getenv("CREWAI_API_KEY", os.getenv("ANTHROPIC_API_KEY", "")),
                verbose=os.getenv("CREWAI_VERBOSE", "false").lower() == "true",
                max_iter=int(os.getenv("CREWAI_MAX_ITER", "15")),
            ),
        )
