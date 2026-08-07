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

SAFE_FALLBACK_REPLY_HI: str = (
    "Main aapki baat samajh sakta hoon. Main abhi aapko hamari care team se connect kar raha hoon."
)

CARE_FIRST_REPLY_HI: str = (
    "Main sun raha hoon. Aapki safety important hai — "
    "main abhi ek specialist se connect kar raha hoon."
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
        "critical_inbound_phrases": list(CRITICAL_INBOUND_PHRASES),
        "safe_fallback_reply": SAFE_FALLBACK_REPLY_HI,
        "care_first_reply": CARE_FIRST_REPLY_HI,
        "opt_out_ack_reply": OPT_OUT_ACK_REPLY_HI,
        "silent_reply": "",
        "clarify_reply": CLARIFY_REPLY_HI,
        "collect_slot_prompts": dict(COLLECT_SLOT_PROMPTS),
    }
