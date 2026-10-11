"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional


class ConfigError(ValueError):
    """The configuration is unsafe or inconsistent; the service must not start."""


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
    verify_ssl: bool = True


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
    # 992 is TN3270 over TLS. Plain-text sessions are refused outside demo mode.
    port: int = 992
    use_tls: bool = True
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
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    # Interim access control (src/l1_agent/api/security.py)
    api_tokens: tuple = ()
    webhook_secret: str = ""
    webhook_max_skew_seconds: int = 300
    cors_allowed_origins: tuple = ()
    # Tenant-supplied L2 routing file; empty uses the packaged example.
    l2_routing_path: str = ""


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

    @classmethod
    def from_env(cls, secrets: Optional[Mapping[str, str]] = None) -> "Settings":
        """Build settings from the environment.

        ``secrets`` holds values fetched from a secrets backend. They are read
        from this mapping and are never copied into ``os.environ``.
        """
        from dotenv import load_dotenv

        load_dotenv()
        fetched = dict(secrets or {})

        def get(name: str, default: str = "") -> str:
            return os.environ.get(name) or fetched.get(name) or default

        demo_mode = get("AGENT_DEMO_MODE", "false").lower() == "true"

        def demo_only_off(name: str) -> bool:
            """A protection that may be switched off only in demo mode."""
            if get(name, "true").lower() == "true":
                return True
            if not demo_mode:
                raise ConfigError(f"{name}=false is only allowed when AGENT_DEMO_MODE=true")
            return False

        return cls(
            servicenow=ServiceNowSettings(
                base_url=get("SERVICENOW_BASE_URL", ""),
                username=get("SERVICENOW_USERNAME", ""),
                password=get("SERVICENOW_PASSWORD", ""),
                poll_interval_seconds=int(get("SERVICENOW_POLL_INTERVAL", "30")),
                assignment_group=get("SERVICENOW_ASSIGNMENT_GROUP", ""),
                sop_table=get("SERVICENOW_SOP_TABLE", "kb_knowledge"),
                incident_table=get("SERVICENOW_INCIDENT_TABLE", "incident"),
            ),
            splunk=SplunkSettings(
                base_url=get("SPLUNK_BASE_URL", ""),
                token=get("SPLUNK_TOKEN", ""),
                verify_ssl=demo_only_off("SPLUNK_VERIFY_SSL"),
                search_timeout_seconds=int(get("SPLUNK_SEARCH_TIMEOUT", "120")),
            ),
            ir360=IR360Settings(
                base_url=get("IR360_BASE_URL", ""),
                api_key=get("IR360_API_KEY", ""),
                cli_path=get("IR360_CLI_PATH", ""),
                use_cli=get("IR360_USE_CLI", "false").lower() == "true",
                verify_ssl=demo_only_off("IR360_VERIFY_SSL"),
            ),
            autosys=AutosysSettings(
                cli_path=get("AUTOSYS_CLI_PATH", "autorep"),
                allowed_commands=tuple(_csv(get("AUTOSYS_ALLOWED_COMMANDS", "autorep"))),
            ),
            windows_share=WindowsShareSettings(
                allowed_unc_prefixes=_csv(get("WINDOWS_SHARE_ALLOWED_PREFIXES", "")),
                smb_username=get("SMB_USERNAME", ""),
                smb_password=get("SMB_PASSWORD", ""),
                smb_domain=get("SMB_DOMAIN", ""),
            ),
            dynatrace=DynatraceSettings(
                base_url=get("DYNATRACE_BASE_URL", ""),
                api_token=get("DYNATRACE_API_TOKEN", ""),
                verify_ssl=demo_only_off("DYNATRACE_VERIFY_SSL"),
                timeout_seconds=int(get("DYNATRACE_TIMEOUT", "30")),
            ),
            webui=WebUISettings(
                page_timeout_seconds=int(get("WEBUI_PAGE_TIMEOUT", "30")),
                allowed_domains=_csv(get("WEBUI_ALLOWED_DOMAINS", "")),
                chrome_binary_path=get("WEBUI_CHROME_BINARY", ""),
            ),
            mainframe=MainframeSettings(
                host=get("MAINFRAME_HOST", ""),
                port=int(get("MAINFRAME_PORT", "992")),
                use_tls=demo_only_off("MAINFRAME_USE_TLS"),
                allowed_transactions=_csv(get("MAINFRAME_ALLOWED_TRANSACTIONS", "")),
                timeout_seconds=int(get("MAINFRAME_TIMEOUT", "30")),
            ),
            llm=LLMSettings(
                endpoint=get("LLM_ENDPOINT", ""),
                api_key=get("LLM_API_KEY", ""),
                model=get("LLM_MODEL", "gpt-4"),
                temperature=float(get("LLM_TEMPERATURE", "0.2")),
                max_tokens=int(get("LLM_MAX_TOKENS", "4096")),
                timeout_seconds=int(get("LLM_TIMEOUT_SECONDS", "60")),
                enabled=get("LLM_ENABLED", "true").lower() == "true",
            ),
            agent=AgentSettings(
                sop_confidence_threshold=float(get("AGENT_SOP_CONFIDENCE_THRESHOLD", "0.6")),
                max_concurrent_incidents=int(get("AGENT_MAX_CONCURRENT_INCIDENTS", "5")),
                retry_max_attempts=int(get("AGENT_RETRY_MAX_ATTEMPTS", "3")),
                retry_base_delay_seconds=float(get("AGENT_RETRY_BASE_DELAY", "1.0")),
                circuit_breaker_failure_threshold=int(get("AGENT_CB_FAILURE_THRESHOLD", "5")),
                circuit_breaker_reset_seconds=int(get("AGENT_CB_RESET_SECONDS", "60")),
                demo_mode=demo_mode,
                log_level=get("AGENT_LOG_LEVEL", "INFO"),
                webhook_host=get("AGENT_WEBHOOK_HOST", "127.0.0.1"),
                webhook_port=int(get("AGENT_WEBHOOK_PORT", "8080")),
                api_tokens=tuple(_csv(get("AGENT_API_TOKENS", ""))),
                webhook_secret=get("AGENT_WEBHOOK_SECRET", ""),
                webhook_max_skew_seconds=int(get("AGENT_WEBHOOK_MAX_SKEW_SECONDS", "300")),
                cors_allowed_origins=tuple(_csv(get("AGENT_CORS_ORIGINS", ""))),
                l2_routing_path=get("L2_ROUTING_PATH", ""),
            ),
            secrets=SecretsSettings(
                backend=get("SECRETS_BACKEND", "env"),
                vault_addr=get("VAULT_ADDR", "http://127.0.0.1:8200"),
                vault_token=get("VAULT_TOKEN", ""),
                vault_role_id=get("VAULT_ROLE_ID", ""),
                vault_secret_id=get("VAULT_SECRET_ID", ""),
                vault_mount=get("VAULT_MOUNT", "secret"),
                vault_path_prefix=get("VAULT_PATH_PREFIX", "l1-agent"),
                aws_region=get("AWS_REGION", "us-east-1"),
                aws_ssm_prefix=get("AWS_SSM_PREFIX", "/l1-agent"),
            ),
            smtp=SmtpSettings(
                host=get("SMTP_HOST", ""),
                port=int(get("SMTP_PORT", "587")),
                user=get("SMTP_USER", ""),
                password=get("SMTP_PASSWORD", ""),
                from_address=get("SMTP_FROM", get("SMTP_USER", "")),
            ),
        )
