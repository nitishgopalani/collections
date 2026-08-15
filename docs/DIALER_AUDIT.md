# DIALER AUDIT — W4-1

_15 Aug 2026 · brain `77b6282` (post UI-1/3) · orchestrator lives in `ari-orchestrator/`_

There is **no campaign scheduler** in-repo. Outbound collection dials today are
operator-fired. This note maps every originate path, what it knows about the
borrower, and where W4-1 controls sit.

## 1. Who actually dials

| Path | Who calls it | Destination | Borrower identity | DNC / cadence / lock (pre-W4-1) |
|---|---|---|---|---|
| **A. Asterisk ARI (live UAT)** | `Collection/scripts/_predue_*.py`, `_final_call*.py`, `_h1_*`, `_r_redial.py` via SSH + `curl` | `POST http://127.0.0.1:8088/ari/channels?endpoint=PJSIP/{num}@ng_trunk&app=fonada-orchestrator&appArgs=inbound,{tenant},{connector}` | `CUSTOMER_PHONE` channel var only. No `borrower_id`. | **None.** Any number can be redialed. |
| **B. Orchestrator HTTP** | Rare for campaign. Brain uses it for **mid-call** consult / transfer / CF1 join (`app/clients/orchestrator.py`). `POST /v1/originate` exists (`ari-orchestrator/internal/api/originate.go`) but UAT scripts do not use it. | `to` + `caller_id` → `PJSIP/{to}` or raw `TECH/...` | Phone only. No borrower, no tenant, no attempt count. | Auth + sandbox kill-switch only. **No DNC.** |
| **C. Inbound DID** | Asterisk dialplan → Stasis `fonada-orchestrator` → connector → go-server → brain | Customer called us | Phone → `lookup_borrower_by_phone` | N/A (not an originate). |
| **D. Brain obligation files** | W3-3 `exports/dispositions_*.jsonl`, `callbacks_*.jsonl`, `worklist_*.jsonl` | Nobody consumes them for dials | `borrower_id`, tenant, disposition, flags | `dnc_requested` is **audit + worklist only**. C2 explicitly did not set `dunning_suppressed` (would have been a lie). |

**Conclusion:** the “campaign layer” is path A (human + ARI). Path B is call-control, not a dialer. Path D is the feed W4-1 must consume.

## 2. What W1-C / W3 already produce

- In-call DNC cue → `disposition=dnc_requested`, graceful END, worklist row. **Does not stop the next originate.**
- `call_history.hydrate_call_history` sets `attempts_today` **after** the call has already been answered (session_start). Too late to refuse the dial.
- `TenantConfig.max_attempts_per_day` (default 3, PaisaLo floored to 200 in goldens) is an **in-call turn/attempt gate**, not a campaign cadence cap.
- Callbacks land in `callbacks_YYYYMMDD.jsonl`. Nothing re-queues them onto a trunk.

## 3. Controls landing in W4-1

Brain owns policy (exports + call history are already there). New surface:

`POST /dialer/v0/originate` → DNC → active-call lock → cadence (default 2/day) → then (optional) orchestrator `/v1/originate`.

- DNC set = `exports/dnc.jsonl` plus any `dnc_requested` row in dispositions/worklist. Match `borrower_id` **or** normalized phone. Refuse + log `dnc_suppressed`.
- Cadence = count of `exports/dials_YYYYMMDD.jsonl` for that borrower/phone today. Default cap `DIALER_MAX_ATTEMPTS_PER_DAY=2`.
- Duplicate lock = in-process `active_call` keyed by borrower_id / phone. Released on `POST /dialer/v0/complete`.
- Callback consume = `POST /dialer/v0/callbacks/consume` reads `callbacks_*.jsonl`, runs the same gate, returns `{due, skipped}`.

**Mid-call** consult / transfer / conference-join stays on the raw orchestrator client. Those are not campaign originates.

## 4. Bypass hardeners (W4-1 B1 / B2, landed CP-W42)

### B1 — ARI credential fence

Live ARI password is **not** in the world-readable host env
(`/etc/ari-orchestrator/ari-orchestrator.env`). Orchestrator loads it from
`ARI_PASSWORD_FILE` (default `/etc/ari-orchestrator/ari.secret`, mode 0640).
`ARI_REQUIRE_SECRET_FILE=true` on UAT. Rotate with
`ari-orchestrator/scripts/rotate_ari_password.sh` (root on UAT; does not print
the secret). After rotation, `grep ARI_PASSWORD` + `curl -u` from the box
fails loudly (401).

**The one legit manual-originate path:**

```
python Collection/scripts/dialer_originate.py \
  --borrower-id plo_test_borrower --phone 9810587857 --tenant-id paisalo
```

That hits `POST /dialer/v0/originate` (DNC / cadence / lock). Do not add a
fallback ARI curl if the gate refuses.

### B2 — bypass tripwire (no enforcement)

On brain `session_start`, if the session is **not** inbound and there is no
matching `dials_YYYYMMDD.jsonl` row for borrower_id/phone, log
`dialer_bypass_detected channel_id=… session_id=…`. Inbound DID is skipped.
A rogue host script shows up in brain logs within the first turn, not after
a complaint.

## 4b. Residual bypass

A process that already has the secret file can still ARI-curl. That is the
orchestrator/container user only. Host SSH users should not.

## 5. Config

| Env | Default | Meaning |
|---|---|---|
| `DIALER_MAX_ATTEMPTS_PER_DAY` | `2` | Campaign cadence cap (not the in-call turn cap). |
| `DIALER_GATE_ENABLED` | `true` | Fail-closed when true. |
| `EXPORTS_DIR` | `exports` | Shared with W3-3. |
| `ORCHESTRATOR_BASE_URL` | empty | If unset, originate is dry (gate + dials file only). |
