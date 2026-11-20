"""Environment configuration. Secrets only ever come from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./data/jobs.db"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))
    timezone: str = field(default_factory=lambda: _env("APP_TIMEZONE", "Asia/Karachi"))

    # Claude
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: _env("ANALYSIS_EFFORT", "low"))
    # USD per million tokens, used only for the cost estimate on the health page.
    claude_input_price: float = field(default_factory=lambda: _float("CLAUDE_INPUT_PRICE_PER_MTOK", 4.0))
    claude_output_price: float = field(default_factory=lambda: _float("CLAUDE_OUTPUT_PRICE_PER_MTOK", 20.0))
    max_analyses_per_run: int = field(default_factory=lambda: _int("MAX_ANALYSES_PER_RUN", 40))

    # Search providers
    serper_api_key: str = field(default_factory=lambda: _env("SERPER_API_KEY"))
    serper_cost_per_query: float = field(default_factory=lambda: _float("SERPER_COST_PER_QUERY", 0.001))
    brave_api_key: str = field(default_factory=lambda: _env("BRAVE_API_KEY"))
    brave_cost_per_query: float = field(default_factory=lambda: _float("BRAVE_COST_PER_QUERY", 0.005))
    serpapi_api_key: str = field(default_factory=lambda: _env("SERPAPI_API_KEY"))
    serpapi_cost_per_query: float = field(default_factory=lambda: _float("SERPAPI_COST_PER_QUERY", 0.015))
    x_bearer_token: str = field(default_factory=lambda: _env("X_BEARER_TOKEN"))
    x_cost_per_request: float = field(default_factory=lambda: _float("X_COST_PER_REQUEST", 0.0))
    max_search_queries_per_run: int = field(default_factory=lambda: _int("MAX_SEARCH_QUERIES_PER_RUN", 18))
    max_enrich_fetches_per_run: int = field(default_factory=lambda: _int("MAX_ENRICH_FETCHES_PER_RUN", 25))

    # Email
    email_provider: str = field(default_factory=lambda: _env("EMAIL_PROVIDER", "resend").lower())
    resend_api_key: str = field(default_factory=lambda: _env("RESEND_API_KEY"))
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM"))
    email_to: str = field(default_factory=lambda: _env("EMAIL_TO"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_username: str = field(default_factory=lambda: _env("SMTP_USERNAME"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_use_tls: bool = field(default_factory=lambda: _env("SMTP_USE_TLS", "true").lower() != "false")

    # Reliability
    healthcheck_ping_url: str = field(default_factory=lambda: _env("HEALTHCHECK_PING_URL"))
    user_agent: str = field(default_factory=lambda: _env("HTTP_USER_AGENT", "GHLJobMonitor/1.0 (personal job alert tool)"))

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def email_enabled(self) -> bool:
        if not self.email_from:
            return False
        if self.email_provider == "smtp":
            return bool(self.smtp_host)
        return bool(self.resend_api_key)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Used by tests after changing environment variables."""
    global _config
    _config = None
