from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.compliance_defaults import default_compliance_policy


class TenantConfig(BaseModel):
    """Per-tenant configuration. v1 uses a single default tenant."""

    tenant_id: str
    languages: list[str] = Field(default_factory=lambda: ["hi", "en", "hinglish"])
    # DECISION NEEDED: languages in scope for v1 — default Hindi + English + Hinglish
    call_window_start: str = "08:00"
    call_window_end: str = "19:00"
    call_window_timezone: str = "Asia/Kolkata"
    max_attempts_per_day: int = 3
    max_attempts_per_week: int = 7
    # DECISION NEEDED: Compliance sign-off on call-window hours and attempt caps.
    prohibited_outbound_phrases: list[str] = Field(
        default_factory=lambda: list(default_compliance_policy()["prohibited_outbound_phrases"])
    )
    collection_pressure_phrases: list[str] = Field(
        default_factory=lambda: list(default_compliance_policy()["collection_pressure_phrases"])
    )
    vulnerability_signals: list[str] = Field(
        default_factory=lambda: list(default_compliance_policy()["vulnerability_signals"])
    )
    distress_signals: list[str] = Field(
        default_factory=lambda: list(default_compliance_policy()["distress_signals"])
    )
    critical_inbound_phrases: list[str] = Field(
        default_factory=lambda: list(default_compliance_policy()["critical_inbound_phrases"])
    )
    safe_fallback_reply: str = default_compliance_policy()["safe_fallback_reply"]
    care_first_reply: str = default_compliance_policy()["care_first_reply"]
    opt_out_ack_reply: str = default_compliance_policy()["opt_out_ack_reply"]
    silent_reply: str = default_compliance_policy()["silent_reply"]
    clarify_reply: str = default_compliance_policy()["clarify_reply"]
    collect_slot_prompts: dict[str, str] = Field(
        default_factory=lambda: dict(default_compliance_policy()["collect_slot_prompts"])
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    stub_mode: bool = True
    default_tenant_id: str = "default"
    override_fixtures: bool | None = None

    tools_url: str = "http://localhost:8002"
    tools_mode: str = "stub"  # live | simulate | stub
    tools_stub: bool = True  # legacy; ignored when tools_mode is set explicitly
    kb_base_url: str = "https://api.fonada.ai"
    kb_api_key: str = ""
    kb_search_path: str = "/search"
    kb_user_agent: str = "Supabase-Function/1.0"
    kb_health_api_key: str = "health-check"
    kb_verify_ssl: bool = True
    kb_top_k: int = 10
    kb_stub: bool = True

    llm_stub: bool = True

    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    # DECISION NEEDED: confirm GCP_REGION with quota (default us-central1).
    gemini_model_id: str = "gemini-2.5-flash"
    # DECISION NEEDED: confirm provisioned GEMINI_MODEL_ID with Fonada eng.
    google_application_credentials: str = ""

    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""

    state_ttl_seconds: int = 14400  # ~4 hours live call state TTL

    ws_turn_deadline_ms: int = 7000
    ws_enabled: bool = True

    call_window_start: str = "08:00"
    call_window_end: str = "19:00"
    call_window_timezone: str = "Asia/Kolkata"
    max_attempts_per_day: int = 3
    max_attempts_per_week: int = 7

    @property
    def memory_stub_mode(self) -> bool:
        """When True, use in-memory store (CI / no Upstash creds)."""
        if self.stub_mode:
            return True
        if not self.upstash_redis_rest_url or not self.upstash_redis_rest_token:
            return True
        return False

    @property
    def kb_stub_mode(self) -> bool:
        return self.kb_stub

    @property
    def tools_stub_mode(self) -> bool:
        return self.tools_mode == "stub"

    @property
    def tools_client_mode(self) -> str:
        """Health display: stub | simulate | live."""
        mode = (self.tools_mode or "stub").lower()
        if mode in {"live", "simulate", "stub"}:
            return mode
        return "stub"

    @property
    def llm_stub_mode(self) -> bool:
        return self.llm_stub

    @property
    def effective_stub_mode(self) -> bool:
        """Legacy alias: reflects global STUB_MODE for health display."""
        return self.stub_mode

    @property
    def override_fixtures_mode(self) -> bool:
        """When True, load in-memory brand override packs (stub/dev)."""
        if self.override_fixtures is not None:
            return self.override_fixtures
        return self.stub_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()


_TEST_TENANT_OVERRIDES: dict[str, dict[str, int]] = {
    # Isolated tenant for simple_ptp_test flow sim + live pipeline validation.
    "test-simple-ptp": {"max_attempts_per_day": 10},
}


def tenant_config(tenant_id: str) -> TenantConfig:
    """Resolve tenant configuration. v1: single tenant with env-backed defaults."""
    settings = get_settings()
    defaults = default_compliance_policy()
    tenant_overrides = _TEST_TENANT_OVERRIDES.get(tenant_id, {})
    return TenantConfig(
        tenant_id=tenant_id,
        call_window_start=settings.call_window_start,
        call_window_end=settings.call_window_end,
        call_window_timezone=settings.call_window_timezone,
        max_attempts_per_day=tenant_overrides.get(
            "max_attempts_per_day", settings.max_attempts_per_day
        ),
        max_attempts_per_week=settings.max_attempts_per_week,
        prohibited_outbound_phrases=list(defaults["prohibited_outbound_phrases"]),
        collection_pressure_phrases=list(defaults["collection_pressure_phrases"]),
        vulnerability_signals=list(defaults["vulnerability_signals"]),
        distress_signals=list(defaults["distress_signals"]),
        critical_inbound_phrases=list(defaults["critical_inbound_phrases"]),
        safe_fallback_reply=str(defaults["safe_fallback_reply"]),
        care_first_reply=str(defaults["care_first_reply"]),
        opt_out_ack_reply=str(defaults["opt_out_ack_reply"]),
        silent_reply=str(defaults["silent_reply"]),
        clarify_reply=str(defaults["clarify_reply"]),
        collect_slot_prompts=dict(defaults["collect_slot_prompts"]),
    )
