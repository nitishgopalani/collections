from functools import lru_cache
from typing import Any
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

    # --- Phase C: multi-tenancy routing defaults ------------------------------
    # Per-tenant defaults used to fill session_start fields the caller omitted.
    # Explicit session_start values always win; these only fill gaps.
    default_pack_id: str = ""
    default_agent_id: str = ""
    default_locale: str = ""
    # Per-tenant concurrency cap enforced at session_start. 0 == unlimited
    # (the default, so single-tenant behaviour is unchanged).
    max_concurrent_sessions: int = 0

    # --- Prompt mode (booking-confirm bot) -------------------------------------
    # agent_mode selects how a turn is handled for this tenant:
    #   "flow"   — the existing flow-engine pipeline (default, unchanged).
    #   "prompt" — ASR text goes straight to the LLM with a per-tenant system
    #              prompt; the reply goes straight to TTS (app/engine/prompt_agent).
    agent_mode: str = "flow"
    # Named system prompts for prompt-mode tenants (persona name -> prompt text).
    # The session's agent_id selects the persona; default_persona fills the gap.
    prompt_personas: dict[str, str] = Field(default_factory=dict)
    default_persona: str = ""
    # Stream prompt-mode LLM replies sentence-by-sentence to TTS (lower first-
    # audio latency). Only meaningful when agent_mode == "prompt" AND the LLM
    # client implements stream(); the flow-engine (SOT) path never streams.
    streaming_llm: bool = False


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

    # Live WhatsApp sender (app.fonada.ai whatsapp_campaign_creator). Default stub until
    # configured; flip WHATSAPP_MODE=live + set URL/key/template to send real messages.
    whatsapp_mode: str = "stub"  # stub | live
    whatsapp_endpoint_url: str = ""
    whatsapp_api_key: str = ""  # sent as Authorization: Bearer <key>
    whatsapp_campaign_name: str = "emi_campaign"
    whatsapp_template_name: str = ""
    whatsapp_timeout_s: float = 10.0

    # CALM-style digression (salary_on_time). When on, the borrower can jump to any
    # sub-flow mid-script (e.g. "how do I pay / send the link") and the engine resumes
    # the interrupted flow afterwards. Instead of hiding objection flows while on-rails,
    # we keep the full retrieved SOT catalog available and rely on the active collect
    # step's awaited-slot hint to keep plain answers mapping to set_slot (not a false
    # digression). Off by default; flip SOT_DIGRESSION=true to enable + A/B.
    sot_digression_enabled: bool = Field(default=False, validation_alias="SOT_DIGRESSION")
    # Layer 0 — always-include critical flows as start_flow candidates even when KB
    # retrieval misses them. Dense retrieval has poor recall and cannot handle negation
    # (NevIR), so "kaise pay karun" can fail to surface sot_obj_link_request while a
    # near/opposite-intent flow (sot_obj_no_link_pref) ranks higher. Pinning guarantees
    # these are always on the menu. CSV of flow names; applied only when digression is
    # on and the call is open. See sot_pinned_flow_list.
    sot_pinned_flows: str = Field(
        default="sot_obj_link_request,sot_obj_diff_number_link,sot_obj_wrong_amount,sot_obj_already_paid_q",
        validation_alias="SOT_PINNED_FLOWS",
    )
    # Layer 3 — retrieval-confidence floor. While the borrower is answering a scripted
    # collect question, a start_flow whose backing KB score is below this floor is
    # treated as a weak/false digression and suppressed (re-ask/keep the answer instead
    # of guessing a wrong objection). Pinned flows and deterministically-coerced flows
    # (which carry no KB score) are exempt. Set 0 to disable the floor.
    sot_flow_confidence_floor: float = Field(default=0.6, validation_alias="SOT_FLOW_FLOOR")
    # High-stakes dispute flows (RBI-relevant legitimate exits). These are (a) exempt
    # from the confidence floor via the cross-turn evidence accumulator and (b) the only
    # themes the accumulator tracks. A single strong hit routes normally; weak-but-
    # repeated evidence (the "0.56 three turns, never crosses 0.6" failure) is caught by
    # accumulating across turns. CSV of flow names.
    sot_dispute_flows: str = Field(
        default="sot_obj_never_loan,sot_obj_wrong_amount,sot_obj_death,sot_obj_frozen_account",
        validation_alias="SOT_DISPUTE_FLOWS",
    )
    # Cross-turn evidence bar: how many turns must show evidence for the same dispute
    # theme before we force-route it even when each single turn scored below the floor.
    # Conservative default (2) fixes the repeated-genuine-dispute case without letting a
    # single weak false positive route. Set 0 to disable the accumulator.
    sot_dispute_evidence_bar: int = Field(default=2, validation_alias="SOT_DISPUTE_BAR")
    # Frustration guard: consecutive med/high anger|frustration turns before a graceful
    # callback escalation (mirrors the repair-layer hand-off). Set 0 to disable.
    sot_frustration_escalate_turns: int = Field(
        default=3, validation_alias="SOT_FRUSTRATION_TURNS"
    )
    # --- Label Transition Layer (LTL) ---------------------------------------------
    # Master switch. Off by default — the whole layer is inert (no detection, no
    # logging, no command rewriting) until this is true.
    label_transition_enabled: bool = Field(
        default=False, validation_alias="LABEL_TRANSITION_ENABLED"
    )
    # "shadow" = detect + record labels/decisions only (commands never rewritten).
    # "enforce" = may rewrite commands, but only for enforce-capable providers (SOT).
    label_transition_mode: str = Field(
        default="shadow", validation_alias="LABEL_TRANSITION_MODE"
    )
    # "supported" = run only for providers with an enforce adapter (salary_on_time).
    # "all" = also run generic shadow observation for other tenants.
    label_transition_scope: str = Field(
        default="supported", validation_alias="LABEL_TRANSITION_SCOPE"
    )
    # When true, an unresolved high-risk label (dispute/identity/legal/etc.) can
    # block/clarify a transition into a money path. Set false to observe only.
    label_high_risk_block: bool = Field(
        default=True, validation_alias="LABEL_HIGH_RISK_BLOCK"
    )
    # Emit a "label_transition" Event onto conversation state each turn (audit trail).
    label_transition_log_enabled: bool = Field(
        default=True, validation_alias="LABEL_TRANSITION_LOG"
    )
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
    def sot_pinned_flow_list(self) -> list[str]:
        """Parsed list of pinned (always-include) SOT flow names (Layer 0)."""
        return [name.strip() for name in self.sot_pinned_flows.split(",") if name.strip()]

    @property
    def sot_dispute_flow_list(self) -> list[str]:
        """Parsed list of high-stakes dispute flow names (accumulator + floor-exempt)."""
        return [name.strip() for name in self.sot_dispute_flows.split(",") if name.strip()]

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


# --- booking-confirm tenant: prompt-mode system prompts ------------------------
# These live in config (not in engine code) so a persona rewrite is a config
# change. Two personas share one tenant: the CUSTOMER leg (inbound caller asking
# about their booking) and the PROPERTY leg (outbound consult call to the hotel
# owner). The <consult ...> / <consult_result ...> markers are the structured
# hand-off contract parsed by app/engine/prompt_agent.py — they are never spoken.
_BOOKING_PERSONA_CUSTOMER = (
    "You are an OYO customer-support voice agent handling booking-confirmation "
    "queries on a phone call. Speak natural Hinglish (Hindi in Latin script mixed "
    "with English), warm and professional. Keep every reply SHORT — one or two "
    "sentences, this is a voice call. Never use lists, emojis, or markdown.\n"
    "\n"
    "Your job: collect the caller's booking ID, hotel name, and guest name, then "
    "verify the booking with the property. Ask for ONE missing detail at a time.\n"
    "\n"
    "Once you have booking ID, hotel name, and guest name, tell the caller "
    '"main property se confirm karke batata hoon, please line par bane rahiye" '
    "and append this marker at the VERY END of that reply (exact format, one "
    "line): <consult booking_id=... hotel=... guest=...>\n"
    "Do not mention the marker or read it out — it is machine-parsed.\n"
    "\n"
    "When a system message like [CONSULT RESULT: confirmed=yes/no, note=...] "
    "appears, relay the outcome naturally: if confirmed=yes, tell the caller the "
    "booking is confirmed; if confirmed=no, apologise and share the reason; if "
    'confirmed=unknown, say "main abhi property se contact nahin kar paya, hum '
    'aapko thodi der mein update karenge" and offer a callback.\n'
    "\n"
    "You have NO tools and NO booking database — never invent booking details. "
    "Do not discuss anything unrelated to OYO bookings."
)
_BOOKING_PERSONA_PROPERTY = (
    "You are Amit calling from OYO. You are on an outbound phone call to a hotel "
    "PROPERTY OWNER to verify one guest booking. Speak polite, brief Hinglish. "
    "One or two short sentences per reply; no lists, emojis, or markdown.\n"
    "\n"
    "Open by introducing yourself (Amit, OYO se) and state the booking you are "
    "verifying: booking ID, guest name, and check-in date (these are given to you "
    "in a system message at the start of the call). Then ask a single clear "
    'question: "kya aap is booking ko confirm karte hain?"\n'
    "\n"
    "If the owner says yes: thank them, and append this marker at the VERY END of "
    "your reply (exact format, one line): "
    "<consult_result booking_id=... confirmed=yes note=...>\n"
    "If the owner says no or cannot honour the booking: ask briefly for the "
    "reason, then thank them and append: "
    "<consult_result booking_id=... confirmed=no note=...> with the reason in "
    "note.\n"
    "Do not mention the marker or read it out — it is machine-parsed.\n"
    "\n"
    "Stay strictly on this one booking; politely decline unrelated topics."
)


# Phase C: per-tenant routing defaults + isolation knobs. This is the config-
# backed tenant registry (no DB dependency). A tenant maps to its default
# pack/agent/locale (used only to fill session_start gaps — explicit values win)
# and an optional per-tenant concurrency cap (0 == unlimited).
#
# The example non-default tenants below demonstrate multi-tenant routing with
# distinct pack/locale defaults; extend this map as real tenants onboard.
_TENANT_ROUTING_DEFAULTS: dict[str, dict[str, Any]] = {
    "salary_on_time": {"default_locale": "hi-IN"},
    "acme_collections": {
        "default_pack_id": "acme_default_pack",
        "default_agent_id": "acme-agent",
        "default_locale": "en-IN",
        "max_concurrent_sessions": 0,
    },
    "globex_recoveries": {
        "default_pack_id": "globex_default_pack",
        "default_agent_id": "globex-agent",
        "default_locale": "ta-IN",
        "max_concurrent_sessions": 0,
    },
    # Small pilot tenant with a hard concurrency cap (exercises the C3 guard).
    "smallco_pilot": {
        "default_locale": "hi-IN",
        "max_concurrent_sessions": 1,
    },
    # Booking-confirmation bot (OYO pilot). client_id "booking-confirm" from the
    # connector resolves straight to this tenant (Phase C client_id routing);
    # agent_mode=prompt routes turns to app/engine/prompt_agent.py instead of the
    # flow engine. agent_id selects the persona (default: persona_customer).
    "booking-confirm": {
        "default_locale": "hi-IN",
        "agent_mode": "prompt",
        "streaming_llm": True,
        "default_persona": "persona_customer",
        "prompt_personas": {
            "persona_customer": _BOOKING_PERSONA_CUSTOMER,
            "persona_property": _BOOKING_PERSONA_PROPERTY,
        },
    },
}


def _apply_tenant_routing_defaults(cfg: "TenantConfig") -> "TenantConfig":
    """Fill a TenantConfig's Phase-C routing fields from the tenant registry."""
    routing = _TENANT_ROUTING_DEFAULTS.get(cfg.tenant_id, {})
    cfg.default_pack_id = str(routing.get("default_pack_id", ""))
    cfg.default_agent_id = str(routing.get("default_agent_id", ""))
    cfg.default_locale = str(routing.get("default_locale", ""))
    cfg.max_concurrent_sessions = int(routing.get("max_concurrent_sessions", 0))
    cfg.agent_mode = str(routing.get("agent_mode", "flow"))
    cfg.prompt_personas = dict(routing.get("prompt_personas", {}))
    cfg.default_persona = str(routing.get("default_persona", ""))
    cfg.streaming_llm = bool(routing.get("streaming_llm", False))
    return cfg


def tenant_config(tenant_id: str) -> TenantConfig:
    """Resolve tenant configuration. v1: single tenant with env-backed defaults."""
    settings = get_settings()
    defaults = default_compliance_policy()
    tenant_overrides = _TEST_TENANT_OVERRIDES.get(tenant_id, {})
    if tenant_id == "salary_on_time":
        return _apply_tenant_routing_defaults(TenantConfig(
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
        ))
    return _apply_tenant_routing_defaults(TenantConfig(
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
    ))
