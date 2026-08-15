# OOF mining — weekly promotion

_OOF-STACK L2. Promote llm-layer hits into L0 packs; turn related_miss into fragment queries._

## Weekly L1 → L0 promotion

1. Pull turns where `oof_layer=llm` and `related=false` (guards / turn_decision).
2. Cluster `ack_text` + transcript tokens (Devanagari-aware, same matcher as L0).
3. A cluster graduates to `app/tenants/{tenant}_irrelevant_topics.yml` when:
   - ≥5 distinct sessions in 7 days, and
   - approved ack already in the "आप शायद … के बारे में पूछ रहे हैं" register, and
   - cue tokens do not collide with fragment `trigger_synonyms` / `answers[]`.
4. Add the subclass pack (or extend an existing one). Do not invent answers.

## related_miss → fragment queries

1. Pull turns where `related_miss=true`.
2. These are loan-adjacent questions the index could not recover.
3. For each cluster, write a fragment query: proposed `id`, `answers[]`, `trigger_synonyms`, slot list, PENDING-CLIENT wording.
4. Do not auto-author fragment text. Ship only after client approval.

## Counters (do not change)

Diversion ladder `_redirect_count` and repair counters stay independent. Mining must not rewrite those rules.
