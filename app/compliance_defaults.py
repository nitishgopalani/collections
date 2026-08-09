"""RBI-sensible compliance defaults — DECISION NEEDED: compliance sign-off before production."""

from typing import Any

# Outbound phrases that must never reach the borrower (substring match, case-insensitive).
PROHIBITED_OUTBOUND_PHRASES: tuple[str, ...] = (
    "police aa",
    "police aayegi",
    "jail",
    "kidnap",
    "property seize",
    "dabba band",
    "sharam karo",
    "bezati",
    "character kharab",
    "family ko phone",
    "wife ko bata",
    "neighbour ko",
    "last warning aaj hi",
    "aaj hi payment warna",
    "threaten",
    "harass",
)

COLLECTION_PRESSURE_PHRASES: tuple[str, ...] = (
    "jama karna",
    "payment kar",
    # "emi" removed (CP5 F1): standalone token falsely blocked legitimate EMI
    # product copy (PaisaLo + platform clarify lines). Pressure detection for
    # Hindi legal/NPA remains below; Latin payment cues remain above.
    "amount due",
    "paisa de",
    "pay now",
    "due date",
    "kal payment",
    # P5.4 — Hindi legal / NPA family (tenant-agnostic). Matching is
    # nukta-insensitive via compliance_rules.normalize (़ stripped), so
    # सख़्त↔सख्त and डिफ़ॉल्ट↔डिफॉल्ट collide; OCR spelling variants listed
    # explicitly where the grapheme sequence differs beyond nukta.
    "कानूनी कार्रवाई",
    "कानूनी कार्यवाही",
    "कानूनी कारवाही",
    "सख़्त कार्रवाई",
    "सख्त कार्रवाई",
    "सख़्त कार्यवाही",
    "कानूनी परिणाम",
    "कानूनी प्रक्रिया",
    "NPA घोषित",
    "खाता NPA",
    "डिफॉल्ट हो सकता",
    "डिफ़ॉल्ट का सामना",
)

VULNERABILITY_SIGNALS: tuple[str, ...] = (
    "suicide",
    "mar jana",
    "mar jaunga",
    "mar jaungi",
    "zinda nahi",
    "hospital",
    "depression",
    "mental health",
    "can't cope",
    "cannot cope",
    "bikhar gaye",
    "bikhar gaya",
    "no money to eat",
    "khane ke paise",
    "job loss",
    "naukri chali gayi",
    "accident",
    "death in family",
    "parivar mein death",
    "crisis",
    "distress",
    "helpless",
    "hopeless",
)

DISTRESS_SIGNALS: tuple[str, ...] = (
    "bahut tension",
    "stress",
    "anxiety",
    "cry",
    "ro raha",
    "ro rahi",
)

# W1-C C2 (DNC/opt-out capture, policy interrupt): cues by which the caller
# asks us to stop calling. Fires BEFORE Tier-1, preempts, speaks the
# non-committal policy_stop_calls wording (request recorded; final
# confirmation will come from the brand — does NOT promise dialer
# suppression until W4 dialer work lands), tags disposition=dnc_requested,
# and graceful END. High-recall: false negative = a regulator complaint.
DNC_SIGNALS: tuple[str, ...] = (
    "dobara call mat karna",
    "dobara call mat karo",
    "dobara mat call karna",
    "call mat karo",
    "call mat karna",
    "pareshan mat karo",
    "pareshan mat karna",
    "pareshan na karo",
    "baar baar mat call karo",
    "baar baar call mat karo",
    "ab se call mat karna",
    "phir se call mat karna",
    "do not call",
    "stop calling",
    "dont call me",
    "don't call",
    "remove my number",
    "दोबारा कॉल मत करना",
    "कॉल मत करो",
    "परेशान मत करो",
    "बार बार मत करो",
)

SAFE_FALLBACK_REPLY_HI: str = (
    "Main aapki baat samajh sakta hoon. Main abhi aapko hamari care team se connect kar raha hoon."
)

CARE_FIRST_REPLY_HI: str = (
    "Main sun raha hoon. Aapki safety important hai — "
    "main abhi ek specialist se connect kar raha hoon."
)

# W1-C C2: non-committal DNC ack. Records the request WITHOUT promising dialer
# suppression (that lands in W4). Matches the policy_stop_calls fragment in
# PAISALO_FRAGMENT_LIBRARY_V1.md §F.
POLICY_STOP_CALLS_REPLY_HI: str = (
    "Aapki yeh request darj ho gayi hai — is vishay ki antim pushti aapko "
    "PaisaLo se mil jayegi. Aapke samay ke liye dhanyavaad."
)

# W1-C C3 (call-window close-out, policy interrupt): scripted polite close
# spoken when an ANSWERED call crosses the configured window boundary
# mid-conversation. Distinct from the gate's silent ``outside_call_window``
# block (which correctly refuses to answer a fresh call outside the window).
# Mid-call we never go silent — we thank the caller, note the request, and
# graceful END (outcome 7) with disposition=call_window_closed.
CALL_WINDOW_CLOSE_REPLY_HI: str = (
    "Aapka samay dhanyavaad. Ab humein is call ko samapt karna hoga — "
    "hamari team aapse yogy samay par dobaara sampark karegi. "
    "Aapke saath baat karke accha laga. Dhanyavaad."
)

# W1-C C4 (third-party / speaker-flip guard, policy interrupt): cues by which
# a different speaker joins or takes over the call mid-conversation. Fires
# BEFORE Tier-1; revokes identity_current; locks disclosure (strict) or
# downgrades to generic-only facts (relaxed); speaks the third-party script +
# callback capture; tags disposition=THIRD_PARTY_FLAGGED. Strict mode ENDs
# (outcome 7); relaxed mode may continue. ALWAYS-ON: third_party_suspected +
# identity_current transition logged regardless of mode.
THIRD_PARTY_FLIP_SIGNALS: tuple[str, ...] = (
    "main uski",
    "main uska",
    "main uske",
    "main ramesh ka",
    "main ramesh ki",
    "main uska bhai",
    "main uski bahan",
    "main uski behen",
    "main uska beta",
    "main uski beti",
    "main uska pita",
    "main uski maa",
    "main uske papa",
    "main uske ghar wala",
    "wo bahar hai",
    "wo bahar hain",
    "wo nahi hai",
    "wo nahi hain",
    "wo gaye",
    "wo gayi",
    "wo so rahe hain",
    "wo so rahi hain",
    "wo kam par gaye",
    "wo kam par gayi",
    "main uske liye bol raha",
    "main uske liye bol rahi",
    "main uski taraf se bol raha",
    "main uski taraf se bol rahi",
    "main uska taraf se",
    "main uski taraf se",
    "main uske bete bol raha",
    "main uske bete bol rahi",
    "uska bhai",
    "uski bahan",
    "uski behen",
    "uska beta",
    "uski beti",
    "uske liye bol raha",
    "uske liye bol rahi",
    "uski taraf se bol raha",
    "uski taraf se bol rahi",
    "uska taraf se",
    "uski taraf se",
    "i am his brother",
    "i am her brother",
    "i am his sister",
    "i am her sister",
    "i am his father",
    "i am her father",
    "i am his mother",
    "i am his son",
    "i am her son",
    "i am his daughter",
    "i am her daughter",
    "he is not here",
    "she is not here",
    "he is outside",
    "she is outside",
    "मैं उसकी",
    "मैं उसका",
    "मैं उसके",
    "वो बाहर है",
    "वो नहीं है",
    "मैं उसके लिए बोल रहा",
    "मैं उसके लिए बोल रही",
    "मैं उसकी तरफ से",
    "मैं उसका भाई",
    "मैं उसकी बहन",
    "उसका भाई",
    "उसकी बहन",
    "उसकी बेहन",
    "उसका बेटा",
    "उसकी बेटी",
    "उसके लिए बोल रहा",
    "उसके लिए बोल रही",
    "उसकी तरफ से बोल रहा",
    "उसकी तरफ से बोल रही",
    "उसका तरफ से",
    "उसकी तरफ से",
)

# Strict mode: third-party script + callback capture + END. No debt phrases
# (the gate's debt-disclosure check would otherwise block this reply under
# third_party_active — policy-lane copy is pre-approved and must not be
# second-guessed by the gate).
THIRD_PARTY_FLIP_REPLY_STRICT_HI: str = (
    "Main samajh gaya. Kya aap Ramesh ji ke parivar se hain? "
    "Jankari sirf account holder ko hi di ja sakti hai. "
    "Kripya Ramesh ji ko humara number de dijiye — "
    "hamari team unse yogy samay par sampark karegi. "
    "Aapke samay ke liye dhanyavaad."
)

# Relaxed mode: identity revoked, generic-only facts (no amounts/dates/PII),
# conversation may continue. Caller is told we can't share specifics but
# can take a message for the account holder.
THIRD_PARTY_FLIP_REPLY_RELAXED_HI: str = (
    "Main samajh gaya ki aap Ramesh ji ke parivar se hain. "
    "Vistrit jankari sirf account holder ko hi di ja sakti hai — "
    "kripya Ramesh ji ko humara number de dijiye. "
    "Kya aap koi sandesh unke liye chhodna chahenge?"
)

OPT_OUT_ACK_REPLY_HI: str = "Theek hai. Aapke request par hum aage contact nahi karenge."

CLARIFY_REPLY_HI: str = "Main sun raha hoon. EMI kab pay kar sakte hain, ya koi sawal hai?"

COLLECT_SLOT_PROMPTS: dict[str, str] = {
    "ptp_date": "Aap kab payment kar sakte hain? Kal, parso ya koi date batayein.",
    "dispute_reason": "Kya issue hai — galat amount, pehle payment, ya kuch aur?",
}

CRITICAL_INBOUND_PHRASES: tuple[str, ...] = (
    "harassment",
    "police complaint",
    "rbi complaint",
    "fraud",
    "scam",
    "galat fraud",
    "threaten",
    "threat",
)


def default_compliance_policy() -> dict[str, Any]:
    return {
        "prohibited_outbound_phrases": list(PROHIBITED_OUTBOUND_PHRASES),
        "collection_pressure_phrases": list(COLLECTION_PRESSURE_PHRASES),
        "vulnerability_signals": list(VULNERABILITY_SIGNALS),
        "distress_signals": list(DISTRESS_SIGNALS),
        "dnc_signals": list(DNC_SIGNALS),
        "third_party_flip_signals": list(THIRD_PARTY_FLIP_SIGNALS),
        "third_party_flip_reply_strict": THIRD_PARTY_FLIP_REPLY_STRICT_HI,
        "third_party_flip_reply_relaxed": THIRD_PARTY_FLIP_REPLY_RELAXED_HI,
        "critical_inbound_phrases": list(CRITICAL_INBOUND_PHRASES),
        "safe_fallback_reply": SAFE_FALLBACK_REPLY_HI,
        "care_first_reply": CARE_FIRST_REPLY_HI,
        "policy_stop_calls_reply": POLICY_STOP_CALLS_REPLY_HI,
        "call_window_close_reply": CALL_WINDOW_CLOSE_REPLY_HI,
        "opt_out_ack_reply": OPT_OUT_ACK_REPLY_HI,
        "silent_reply": "",
        "clarify_reply": CLARIFY_REPLY_HI,
        "collect_slot_prompts": dict(COLLECT_SLOT_PROMPTS),
    }
