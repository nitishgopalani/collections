from functools import lru_cache
from urllib.parse import quote_plus

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
    enforce_compliance_gate: bool = True
    enforce_safety_gate: bool = True
    # Conversation repair (F1): how many times the SAME question may be re-asked
    # before we stop looping and hand off gracefully.
    max_slot_retries: int = 2
    # Spoken when the retry cap is hit — we log a callback for manual follow-up
    # (no live transfer yet) and end the call politely.
    escalation_reply: str = (
        "माफ़ कीजिए, मैं आपकी बात ठीक से समझ नहीं पा रहा हूँ। "
        "मैं आपका नंबर नोट कर रहा हूँ, हमारी टीम आपको जल्दी वापस कॉल करेगी। "
        "आपके समय के लिए धन्यवाद।"
    )
    # Conversation repair (F6): when two candidate flows score within
    # ``flow_ambiguity_delta`` of each other, ask the caller to clarify instead of
    # guessing which one to start. OFF by default and opt-in per tenant: KB score
    # ties are a weak uncertainty signal (the LLM still makes the final pick and is
    # often right about the lower-scored candidate), so enabling it globally can
    # override correct picks. Turn on only for tenants that want the extra guardrail.
    clarify_on_ambiguous_flow: bool = False
    flow_ambiguity_delta: float = 0.04


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    stub_mode: bool = True
    test_mode: bool = False
    test_tenant_id: str = "salary_on_time"
    test_section: str = "pre_closure"
    # Which SOT sub-script the bare TEST_MODE line exercises: pre | on_due | post_due.
    # Only changes the hardcoded test borrower's due_date; production derives the
    # scenario from the real borrower's due_date via select_sot_scenario.
    test_sot_scenario: str = "pre"
    default_tenant_id: str = "default"
    override_fixtures: bool | None = None

    tools_url: str = "http://localhost:8002"
    tools_mode: str = "stub"  # live | simulate | stub
    tools_stub: bool = True  # legacy; ignored when tools_mode is set explicitly

    # Live call transfer (Model A). Default stub until the telephony endpoint exists;
    # flip TRANSFER_MODE=live + set the URL/auth to enable a real bridge — no code change.
    transfer_mode: str = "stub"  # stub | live
    transfer_endpoint_url: str = ""
    transfer_auth_token: str = ""  # legacy Bearer; voip endpoint uses X-API-Key below
    transfer_api_key: str = ""  # sent as X-API-Key header to the voip endpoint
    transfer_default_target: str = ""  # transferring_number (human agent) if slot unset
    transfer_timeout_s: float = 10.0
    # voip.ivrobd.com /v1/transfer request-body fields (defaults match the contract).
    transfer_context: str = "transfer-gen"
    transfer_priority: int = 1
    transfer_delay_ms: int = 4000
    transfer_environment: str = "prod"
    transfer_call_type: str = "outbound"
    # Client-side hold before firing the transfer POST, so the "connecting you to a
    # senior" line finishes playing before the carrier bridges the human (otherwise the
    # bridge cuts the audio mid-sentence). Fired in the background so the reply/TTS is
    # NOT delayed — only the endpoint call is held. Tune per handoff-line length.
    transfer_hold_ms: int = 4500
    kb_base_url: str = "https://api.fonada.ai"
    kb_api_key: str = ""
    kb_search_path: str = "/search"
    kb_user_agent: str = "Supabase-Function/1.0"
    kb_health_api_key: str = "health-check"
    kb_verify_ssl: bool = True
    kb_top_k: int = 10
    kb_stub: bool = True

    llm_stub: bool = True

    # LLM provider: "vertex" (Gemini) or "groq" (fast LPU, OpenAI-compatible).
    llm_provider: str = "vertex"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    # DECISION NEEDED: confirm GCP_REGION with quota (default us-central1).
    gemini_model_id: str = "gemini-2.5-flash"
    # DECISION NEEDED: confirm provisioned GEMINI_MODEL_ID with Fonada eng.
    google_application_credentials: str = ""

    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""

    # Local Postgres for borrower lookup (test stack — NOT Supabase/managed DB).
    borrower_database_url: str = ""
    database_url: str = ""
    postgres_user: str = Field(default="collections", validation_alias="POSTGRES_USER")
    postgres_password: str = Field(default="", validation_alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="collections", validation_alias="POSTGRES_DB")
    postgres_host: str = Field(default="postgres", validation_alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")

    state_ttl_seconds: int = 14400  # ~4 hours live call state TTL

    ws_turn_deadline_ms: int = 12000
    ws_enabled: bool = True

    call_window_start: str = "08:00"
    call_window_end: str = "19:00"
    call_window_timezone: str = "Asia/Kolkata"
    max_attempts_per_day: int = 3
    max_attempts_per_week: int = 7

    @property
    def effective_borrower_database_url(self) -> str:
        """Postgres URL for borrower table reads (local docker service only)."""
        if self.postgres_password.strip():
            user = quote_plus(self.postgres_user or "collections")
            password = quote_plus(self.postgres_password)
            host = self.postgres_host or "postgres"
            db = quote_plus(self.postgres_db or "collections")
            return f"postgresql://{user}:{password}@{host}:{self.postgres_port}/{db}"
        return (self.borrower_database_url or self.database_url).strip()

    @property
    def borrower_db_enabled(self) -> bool:
        return bool(self.effective_borrower_database_url)

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
    # Isolated tenants for pipeline validation flows (higher attempt cap for multi-turn sims).
    "test-simple-ptp": {"max_attempts_per_day": 10},
    "test-name-identity": {"max_attempts_per_day": 10},
}


def tenant_config(tenant_id: str) -> TenantConfig:
    """Resolve tenant configuration. v1: single tenant with env-backed defaults."""
    settings = get_settings()
    defaults = default_compliance_policy()
    tenant_overrides = _TEST_TENANT_OVERRIDES.get(tenant_id, {})
    if tenant_id == "salary_on_time":
        return TenantConfig(
            tenant_id=tenant_id,
            call_window_start=settings.call_window_start,
            call_window_end=settings.call_window_end,
            call_window_timezone=settings.call_window_timezone,
            # Attempts increment per TURN; this tenant runs multi-turn scripted
            # conversations, so floor the caps high so the gate never silences mid-call.
            max_attempts_per_day=max(
                tenant_overrides.get("max_attempts_per_day", settings.max_attempts_per_day), 200
            ),
            max_attempts_per_week=max(settings.max_attempts_per_week, 2000),
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
            enforce_compliance_gate=False,
            enforce_safety_gate=True,
            # SOT already constrains candidates (sot_* only, objections suppressed
            # on-rails); an extra "did you mean?" turn would only lengthen the script.
            clarify_on_ambiguous_flow=False,
        )
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
