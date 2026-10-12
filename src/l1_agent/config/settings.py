"""Application settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Mapping, Optional


class ConfigError(ValueError):
    """The configuration is unsafe or inconsistent; the service must not start."""


def _action_settings(get) -> "ActionSettings":
    max_tier = get("ACTIONS_MAX_TIER", "T2").upper()
    max_autonomy = get("ACTIONS_MAX_AUTONOMY", "automatic").lower()
    if max_tier not in ("T0", "T1", "T2"):
        raise ConfigError("ACTIONS_MAX_TIER must be T0, T1 or T2")
    if max_autonomy not in ("shadow", "recommend", "approval", "automatic"):
        raise ConfigError("ACTIONS_MAX_AUTONOMY must be shadow, recommend, approval or automatic")
    return ActionSettings(
        max_tier=max_tier,
        max_autonomy=max_autonomy,
        kill_switch_file=get("ACTIONS_KILL_SWITCH_FILE", ""),
        servicenow_poll_seconds=float(get("ACTIONS_SERVICENOW_POLL_SECONDS", "30")),
    )


# The adapters LIVE_ADAPTERS may name. Only Splunk has run against a real system.
LIVE_ADAPTER_NAMES = ("splunk",)


def _splunk_backend(value: str) -> str:
    value = value.strip().lower()
    if value not in ("", "mcp", "rest", "mock"):
        raise ConfigError("SPLUNK_BACKEND must be mcp, rest or mock")
    return value


def _live_adapters(value: str, demo_mode: bool) -> tuple:
    names = tuple(name.lower() for name in _csv(value))
    if names and not demo_mode:
        raise ConfigError(
            "LIVE_ADAPTERS is for demo mode. Outside it every connector is live already"
        )
    unknown = [name for name in names if name not in LIVE_ADAPTER_NAMES]
    if unknown:
        raise ConfigError(f"LIVE_ADAPTERS may name: {', '.join(LIVE_ADAPTER_NAMES)}")
    return names


def _csv(val: str) -> List[str]:
    return [v.strip() for v in val.split(",") if v.strip()]


def _runner_settings(get, demo_mode: bool, demo_only_off) -> "RunnerSettings":
    mode = get("RUNNER_MODE", "embedded").lower()
    if mode not in ("embedded", "remote"):
        raise ConfigError("RUNNER_MODE must be embedded or remote")
    max_tier = get("RUNNER_MAX_TIER", "T0").upper()
    if max_tier not in ("T0", "T1", "T2"):
        raise ConfigError("RUNNER_MAX_TIER must be T0, T1 or T2")

    def public_key(name: str) -> str:
        """A public key given directly, or in a file named by <NAME>_FILE."""
        path = get(f"{name}_FILE", "")
        if not path:
            return get(name, "").strip()
        try:
            with open(path, encoding="ascii") as handle:
                return handle.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{name}_FILE could not be read: {type(exc).__name__}") from None

    mock = get("RUNNER_MOCK_CONNECTORS", "false").lower() == "true"
    if mock and not demo_mode:
        raise ConfigError("RUNNER_MOCK_CONNECTORS=true is only allowed when AGENT_DEMO_MODE=true")
    return RunnerSettings(
        mode=mode,
        dispatch_key=get("RUNNER_DISPATCH_KEY", ""),
        grant_key=get("ACTIONS_GRANT_KEY", ""),
        control_plane_url=get("RUNNER_CONTROL_PLANE_URL", "").rstrip("/"),
        identity=get("RUNNER_ID", "").strip(),
        private_key=get("RUNNER_PRIVATE_KEY", ""),
        dispatch_public_key=public_key("RUNNER_DISPATCH_PUBLIC_KEY"),
        grant_public_key=public_key("RUNNER_GRANT_PUBLIC_KEY"),
        max_tier=max_tier,
        capabilities=tuple(_csv(get("RUNNER_CAPABILITIES", "*"))),
        targets_file=get("RUNNER_TARGETS_FILE", ""),
        connections_file=get("RUNNER_CONNECTIONS_FILE", ""),
        secret_ref_prefixes=tuple(_csv(get("RUNNER_SECRET_REF_PREFIXES", ""))),
        kill_switch_file=get("RUNNER_KILL_SWITCH_FILE", ""),
        concurrency=max(1, int(get("RUNNER_CONCURRENCY", "4"))),
        mock_connectors=mock,
        verify_ssl=demo_only_off("RUNNER_VERIFY_SSL"),
        ca_file=get("RUNNER_CA_FILE", ""),
        client_cert_file=get("RUNNER_CLIENT_CERT_FILE", ""),
        client_key_file=get("RUNNER_CLIENT_KEY_FILE", ""),
    )


@dataclass(frozen=True)
class ServiceNowSettings:
    base_url: str = ""
    username: str = ""
    password: str = ""
    poll_interval_seconds: int = 30
    assignment_group: str = ""
    sop_table: str = "kb_knowledge"
    incident_table: str = "incident"
    timeout_seconds: int = 30
    # Resolution code sent when an incident is resolved. Must be a value your
    # instance accepts; a stock instance rejects Resolved without one.
    close_code: str = ""
    # The group a change the agent opens is assigned to. A stock instance will not
    # move a change out of New without one; leave empty when the template sets it.
    change_assignment_group: str = ""
    # Demo mode normally uses an in-memory ServiceNow. Set this to connect demo
    # mode (mock tool adapters, sample SOPs) to a real instance for testing.
    live_in_demo: bool = False
    # "basic", or "oauth" (client credentials; the password grant when a password is set too).
    auth: str = "basic"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""  # default: <base_url>/oauth_token.do
    # The user the agent assigns a ticket to when it claims it. Defaults to `username`.
    agent_user: str = ""
    claim: bool = True
    page_size: int = 100
    max_pages: int = 10
    verify_ssl: bool = True


@dataclass(frozen=True)
class KnowledgeSettings:
    """Where SOP source articles are pulled from. Everything pulled becomes a draft."""

    sync_interval_seconds: int = 0  # 0: only when asked (`l1-agent knowledge sync`)
    servicenow_kb: bool = False
    servicenow_kb_query: str = "workflow_state=published"
    git_url: str = ""
    git_ref: str = "main"
    git_path: str = ""
    git_cache_dir: str = ""


@dataclass(frozen=True)
class SplunkSettings:
    base_url: str = ""
    token: str = ""
    verify_ssl: bool = True
    search_timeout_seconds: int = 120
    # mcp (the Splunk MCP Server app), rest (the search API) or mock. Empty: rest when
    # SPLUNK_BASE_URL is set, otherwise mcp when SPLUNK_MCP_URL is.
    backend: str = ""
    mcp_url: str = ""
    mcp_search_tool: str = "splunk_run_query"
    # The MCP token is never held here: it is read through the reference to SPLUNK_MCP_AUTH.
    ca_bundle: str = ""
    # What the guard allows. No index listed means no search may be run.
    allowed_indexes: tuple = ()
    max_lookback: str = "24h"
    max_results: int = 100


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
    sendevent_path: str = "sendevent"
    # Jobs the force start may be used on: exact names or patterns. Empty means none.
    force_start_jobs: tuple = ()


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
    # Which kind of endpoint this is: "openai_compatible" or "anthropic" (ADR 0009).
    provider: str = "openai_compatible"
    # What the operator states about the endpoint. A tenant's model policy is checked
    # against these; the product cannot verify them with the provider.
    region: str = ""
    retention_days: Optional[int] = None  # None: not stated
    # Prices in US dollars per million tokens, for cost budgets. Zero means not priced.
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0


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
    # Demo mode only: adapters that talk to the real system while the rest stay mocks.
    live_adapters: tuple = ()
    # Demo mode starts with sample SOPs published and the tenant set to automatic.
    # False leaves the demo tenant empty, to walk through set-up from the beginning.
    demo_seed: bool = True
    log_level: str = "INFO"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    # Interim access control (src/l1_agent/api/security.py)
    webhook_secret: str = ""
    webhook_max_skew_seconds: int = 300
    cors_allowed_origins: tuple = ()
    # Tenant-supplied L2 routing file; empty uses the packaged example.
    l2_routing_path: str = ""
    # A certificate and key for the listener itself. Without them it serves plain HTTP
    # and TLS is expected to end in front of it (an ingress or a mesh).
    tls_cert_file: str = ""
    tls_key_file: str = ""


@dataclass(frozen=True)
class DatabaseSettings:
    # The application role. Row level security applies to it; it cannot migrate.
    url: str = ""
    # The role that owns the schema. Used only by `l1-agent migrate` and tenant creation.
    migration_url: str = ""
    app_role: str = "l1_app"
    # The tenant this deployment takes incidents for (ADR 0003, shape A).
    tenant_slug: str = "default"


@dataclass(frozen=True)
class AuthSettings:
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_groups_claim: str = "groups"
    saml_idp_metadata_path: str = ""
    saml_sp_entity_id: str = ""
    saml_acs_url: str = ""
    saml_groups_attribute: str = "groups"
    session_hours: int = 8
    session_idle_minutes: int = 30
    # Cookies are Secure unless demo mode says otherwise.
    cookie_secure: bool = True
    # scrypt hash of the break-glass password. Empty disables the account.
    break_glass_password_hash: str = ""
    audit_checkpoint_key: str = ""
    rate_limit_per_minute: int = 600
    login_rate_limit_per_minute: int = 20


@dataclass(frozen=True)
class WorkerSettings:
    poll_interval_seconds: float = 1.0
    visibility_timeout_seconds: int = 300
    max_attempts: int = 3
    # Run the worker inside the API process. Separate processes are the production shape.
    embedded: bool = True


@dataclass(frozen=True)
class ActionSettings:
    """What this deployment allows at most, whatever a tenant's policy says (ADR 0008)."""

    max_tier: str = "T2"  # T0 (no writes), T1 or T2
    max_autonomy: str = "automatic"  # shadow, recommend, approval or automatic
    # While this file exists, no write is made by this process.
    kill_switch_file: str = ""
    servicenow_poll_seconds: float = 30.0


@dataclass(frozen=True)
class RunnerSettings:
    """The runner split (ADR 0010).

    The first three are the control plane's. The rest configure the runner
    process (`l1-agent runner run`) and are read nowhere else.
    """

    # "embedded": connector calls are made in the worker. "remote": by a runner.
    mode: str = "embedded"
    # Private keys, as PEM. In a deployment give RUNNER_DISPATCH_KEY_REF and
    # ACTIONS_GRANT_KEY_REF instead, and give the grant key to workers only.
    dispatch_key: str = ""
    grant_key: str = ""

    control_plane_url: str = ""
    # RUNNER_ID, as shown when the runner was enrolled. Empty: the runner names itself
    # by its key, which is enough where the control plane serves one tenant.
    identity: str = ""
    private_key: str = ""  # or RUNNER_PRIVATE_KEY_REF
    dispatch_public_key: str = ""
    grant_public_key: str = ""  # empty: this runner makes no writes
    max_tier: str = "T0"
    capabilities: tuple = ("*",)
    targets_file: str = ""
    connections_file: str = ""
    secret_ref_prefixes: tuple = ()
    kill_switch_file: str = ""
    concurrency: int = 4
    # Mock connectors in place of real ones. Demo mode only.
    mock_connectors: bool = False
    verify_ssl: bool = True
    ca_file: str = ""  # a private CA for the control plane's certificate
    # A client certificate, for an ingress that requires mutual TLS.
    client_cert_file: str = ""
    client_key_file: str = ""


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
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    knowledge: KnowledgeSettings = field(default_factory=KnowledgeSettings)
    actions: ActionSettings = field(default_factory=ActionSettings)
    runner: RunnerSettings = field(default_factory=RunnerSettings)
    # Credential references given as <NAME>_REF, for example
    # SPLUNK_TOKEN_REF=file:/run/secrets/splunk-token. They take the place of <NAME>.
    secret_refs: Mapping[str, str] = field(default_factory=dict)

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
                timeout_seconds=int(get("SERVICENOW_TIMEOUT", "30")),
                close_code=get("SERVICENOW_CLOSE_CODE", ""),
                change_assignment_group=get("SERVICENOW_CHANGE_ASSIGNMENT_GROUP", "").strip(),
                live_in_demo=get("SERVICENOW_LIVE_IN_DEMO", "false").lower() == "true",
                auth=get("SERVICENOW_AUTH", "basic").lower(),
                oauth_client_id=get("SERVICENOW_OAUTH_CLIENT_ID", ""),
                oauth_client_secret=get("SERVICENOW_OAUTH_CLIENT_SECRET", ""),
                oauth_token_url=get("SERVICENOW_OAUTH_TOKEN_URL", ""),
                agent_user=get("SERVICENOW_AGENT_USER", ""),
                claim=get("SERVICENOW_CLAIM", "true").lower() == "true",
                page_size=int(get("SERVICENOW_PAGE_SIZE", "100")),
                max_pages=int(get("SERVICENOW_MAX_PAGES", "10")),
                verify_ssl=demo_only_off("SERVICENOW_VERIFY_SSL"),
            ),
            knowledge=KnowledgeSettings(
                sync_interval_seconds=int(get("KNOWLEDGE_SYNC_INTERVAL", "0")),
                servicenow_kb=get("KNOWLEDGE_SERVICENOW_KB", "false").lower() == "true",
                servicenow_kb_query=get(
                    "KNOWLEDGE_SERVICENOW_KB_QUERY", "workflow_state=published"
                ),
                git_url=get("KNOWLEDGE_GIT_URL", ""),
                git_ref=get("KNOWLEDGE_GIT_REF", "main"),
                git_path=get("KNOWLEDGE_GIT_PATH", ""),
                git_cache_dir=get("KNOWLEDGE_GIT_CACHE_DIR", ""),
            ),
            secret_refs={
                name[: -len("_REF")]: value
                for name, value in os.environ.items()
                if name.endswith("_REF") and name.isupper() and ":" in value
            },
            splunk=SplunkSettings(
                base_url=get("SPLUNK_BASE_URL", ""),
                token=get("SPLUNK_TOKEN", ""),
                verify_ssl=demo_only_off("SPLUNK_VERIFY_SSL"),
                search_timeout_seconds=int(
                    get("SPLUNK_TIMEOUT_SECONDS") or get("SPLUNK_SEARCH_TIMEOUT", "120")
                ),
                backend=_splunk_backend(get("SPLUNK_BACKEND", "")),
                mcp_url=get("SPLUNK_MCP_URL", ""),
                mcp_search_tool=get("SPLUNK_MCP_SEARCH_TOOL", "splunk_run_query"),
                ca_bundle=get("SPLUNK_CA_BUNDLE", ""),
                allowed_indexes=tuple(_csv(get("SPLUNK_ALLOWED_INDEXES", ""))),
                max_lookback=get("SPLUNK_MAX_LOOKBACK", "24h"),
                max_results=int(get("SPLUNK_MAX_RESULTS", "100")),
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
                sendevent_path=get("AUTOSYS_SENDEVENT_PATH", "sendevent"),
                force_start_jobs=tuple(_csv(get("AUTOSYS_FORCE_START_JOBS", ""))),
            ),
            actions=_action_settings(get),
            runner=_runner_settings(get, demo_mode, demo_only_off),
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
                provider=get("LLM_PROVIDER", "openai_compatible").strip().lower(),
                region=get("LLM_REGION", "").strip().lower(),
                retention_days=(
                    int(get("LLM_RETENTION_DAYS")) if get("LLM_RETENTION_DAYS") else None
                ),
                input_price_per_mtok=float(get("LLM_INPUT_PRICE_PER_MTOK", "0")),
                output_price_per_mtok=float(get("LLM_OUTPUT_PRICE_PER_MTOK", "0")),
            ),
            agent=AgentSettings(
                sop_confidence_threshold=float(get("AGENT_SOP_CONFIDENCE_THRESHOLD", "0.6")),
                max_concurrent_incidents=int(get("AGENT_MAX_CONCURRENT_INCIDENTS", "5")),
                retry_max_attempts=int(get("AGENT_RETRY_MAX_ATTEMPTS", "3")),
                retry_base_delay_seconds=float(get("AGENT_RETRY_BASE_DELAY", "1.0")),
                circuit_breaker_failure_threshold=int(get("AGENT_CB_FAILURE_THRESHOLD", "5")),
                circuit_breaker_reset_seconds=int(get("AGENT_CB_RESET_SECONDS", "60")),
                demo_mode=demo_mode,
                live_adapters=_live_adapters(get("LIVE_ADAPTERS", ""), demo_mode),
                demo_seed=get("AGENT_DEMO_SEED", "true").lower() == "true",
                log_level=get("AGENT_LOG_LEVEL", "INFO"),
                webhook_host=get("AGENT_WEBHOOK_HOST", "127.0.0.1"),
                webhook_port=int(get("AGENT_WEBHOOK_PORT", "8080")),
                webhook_secret=get("AGENT_WEBHOOK_SECRET", ""),
                webhook_max_skew_seconds=int(get("AGENT_WEBHOOK_MAX_SKEW_SECONDS", "300")),
                cors_allowed_origins=tuple(_csv(get("AGENT_CORS_ORIGINS", ""))),
                l2_routing_path=get("L2_ROUTING_PATH", ""),
                tls_cert_file=get("AGENT_TLS_CERT_FILE", ""),
                tls_key_file=get("AGENT_TLS_KEY_FILE", ""),
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
            database=DatabaseSettings(
                url=get("DATABASE_URL", ""),
                migration_url=get("DATABASE_MIGRATION_URL", ""),
                app_role=get("DATABASE_APP_ROLE", "l1_app"),
                tenant_slug=get("TENANT_SLUG", "demo" if demo_mode else "default"),
            ),
            auth=AuthSettings(
                oidc_issuer=get("OIDC_ISSUER", "").rstrip("/"),
                oidc_client_id=get("OIDC_CLIENT_ID", ""),
                oidc_client_secret=get("OIDC_CLIENT_SECRET", ""),
                oidc_redirect_uri=get("OIDC_REDIRECT_URI", ""),
                oidc_scopes=get("OIDC_SCOPES", "openid profile email"),
                oidc_groups_claim=get("OIDC_GROUPS_CLAIM", "groups"),
                saml_idp_metadata_path=get("SAML_IDP_METADATA_PATH", ""),
                saml_sp_entity_id=get("SAML_SP_ENTITY_ID", ""),
                saml_acs_url=get("SAML_ACS_URL", ""),
                saml_groups_attribute=get("SAML_GROUPS_ATTRIBUTE", "groups"),
                session_hours=int(get("SESSION_HOURS", "8")),
                session_idle_minutes=int(get("SESSION_IDLE_MINUTES", "30")),
                cookie_secure=demo_only_off("SESSION_COOKIE_SECURE"),
                break_glass_password_hash=get("BREAK_GLASS_PASSWORD_HASH", ""),
                audit_checkpoint_key=get("AUDIT_CHECKPOINT_KEY", ""),
                rate_limit_per_minute=int(get("RATE_LIMIT_PER_MINUTE", "600")),
                login_rate_limit_per_minute=int(get("LOGIN_RATE_LIMIT_PER_MINUTE", "20")),
            ),
            worker=WorkerSettings(
                poll_interval_seconds=float(get("WORKER_POLL_INTERVAL", "1.0")),
                visibility_timeout_seconds=int(get("WORKER_VISIBILITY_TIMEOUT", "300")),
                max_attempts=int(get("WORKER_MAX_ATTEMPTS", "3")),
                embedded=get("WORKER_EMBEDDED", "true").lower() == "true",
            ),
        )
