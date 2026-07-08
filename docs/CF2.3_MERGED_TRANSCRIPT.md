# CF2.3 — merged per-speaker conference transcript

## Store design

- **Module:** `app/ws/conference_transcript.py`
- **Key:** `parent_session_uuid` (canonical: lowercase, dashes stripped)
- **Record:** list of `{speaker, text, ts, turn_id}` from tap-only turns
- **Timestamp:** `server_receive_ms` — wall-clock at append in brain (v1; not ASR capture time)
- **Concurrency:** `threading.Lock` on all store mutations
- **Lifecycle:** finalize on main conference `session_end`; TTL default 3600s (`conference_transcript_ttl_s`)

## Retrieval

`GET /v1/conference/{parent_session_uuid}/transcript` → JSON with `entries[]` + `formatted` plain text.

## Production follow-up

Durable DB/Redis storage is a later step; in-memory is per-process only.
