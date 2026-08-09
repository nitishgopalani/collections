# PIPELINE_AUDIT — Fonada Voice Collections Stack

**Date:** 2026-08-08  
**Mode:** AUDIT ONLY (no code changes, no deploys, no live calls)  
**Auditor:** Cursor (implementer) · Review: Claude · Sign-off: Nitish  
**Host:** Mohali UAT `Nitish-Moh` (SSH read-only probes + local tree / pytest)

Evidence artefacts (local, untracked): `Collection/scripts/_pipeline_audit_uat*.txt`, `_audit_golden_run.txt`, `_audit_p6_debt.txt`.

---

## PART A — PIPELINE (Asterisk → brain)

### A1. Deploy truth table

Probed 2026-08-08 ~14:22–14:30 IST. Secrets redacted in this doc.

| Service | Running claim | Git checkout claims | Clean build? | `/version` | Notes |
|---|---|---|---|---|---|
| **Asterisk** | Asterisk **18.26.4** (host systemd) | dialplan under `/etc/asterisk` | n/a (distro) | **none** (HTTP 404 on :8088) | PJSIP trunk live |
| **ari-orchestrator** | systemd `ari-orchestrator.service` pid 94384; `HTTP_LISTEN_ADDR=172.18.0.1:8095` | `/opt/fonada/ari-orchestrator` detached **HEAD = `2408ce2`** (“Originate binds CUSTOMER_PHONE…”) | binary `/usr/local/bin/ari-orchestrator` — **no git stamp in `/version`** | `:8095/version` → **401 unauthorized** (auth-gated, not a build stamp). `/health` → `status=ok` | **Lacks public version stamp.** Detached HEAD (not on `feature/phase-b-orchestrator-core` name) |
| **asterisk-connector** | systemd; AudioSocket `:9092`; HTTP `:9091` | branch `feature/async-ws-audio-writes` **`dbc5732`** | `/version` reports same sha (no `+dirty`) | **`http://127.0.0.1:9091/version` → `{"git_branch":"feature/async-ws-audio-writes","git_sha":"dbc57320…"}`** | Matches checkout tip (includes C-D phone + clear + drop-newest) |
| **go-server** | docker `fonada-voice-go-server-1` image `fonada/voice-go-server:latest` sha256:`5d427dfc…` created ~14:01 IST | checkout `/opt/fonada/Websocket` **`852c792`** but **`852c792-dirty`** | `/version` → `{"git_branch":"release/uat-voice-stack","git_sha":"852c792"}` (clean stamp) | **yes** on host `:8080` | Image stamp clean; **working tree dirty** (compose patch — see live-only) |
| **brain** | docker `fonada-voice-brain-1` image `fonada/voice-brain:latest` sha256:`18ead36e…` | `/opt/fonada/Collection` **`feature/tier23-engine-upgrade` @ `2d35999`** (R2-DB sentinel) | no image label with git sha; container env has no `GIT_SHA` | **no `/version`** (only `/health` inside container net; **8000 not published to host**) | Handover cited image build `9f29ccf`; live checkout is **newer** `2d35999` |
| **postgres (voice)** | `fonada-voice-postgres-1` `postgres:16-alpine`; publish **`172.18.0.1:5432→5432`** | compose from Websocket deploy | n/a | n/a | DBs: `collections` + `orchestrator` |
| **semantic-turn** | `fonada-voice-semantic-turn-1` | image Jun 30 | n/a | none probed | healthy |

#### Live-only / dirty patches

1. **`/opt/fonada/Websocket/deploy/docker-compose.yml` modified vs `852c792`** — adds postgres host publish:
   - `+ "172.18.0.1:5432:5432"` (exactly the known live-only postgres-port class; previously reverted on checkout).
   - Evidence: `git -C /opt/fonada/Websocket status` → `M deploy/docker-compose.yml`; `describe` → `852c792-dirty`.
2. Untracked `deploy/secrets/gcp-sa.json` on Websocket checkout.
3. Brain/go-server containers inherit **shared compose `.env`** including `TEST_MODE=true`, `TEST_SOT_SCENARIO=pre`, `TEST_TENANT_ID=paisalo` (see A2) — behavioural live config, not a binary patch.

#### Services lacking `/version`

| Component | Status |
|---|---|
| Asterisk | no |
| ari-orchestrator | no usable build stamp (`/version` auth-gated error JSON) |
| brain | no `/version` endpoint / no host port |
| postgres / redis / semantic-turn | no |

---

### A2. Config / env matrix (behaviour-changing)

UAT values from `docker inspect` / `docker exec printenv` / connector environ (2026-08-08). Code defaults from repo.

| Var | UAT value | Code default | If unset / silent behaviour | Flag |
|---|---|---|---|---|
| `TEST_MODE` | **`true`** (brain+compose) | `false` (`config.py`) | Bare session fill; **pins tenant via `TEST_TENANT_ID` ignoring connector `client_id`** for flow tenants | **FINDING** — UAT depends on this |
| `TEST_TENANT_ID` | **`paisalo`** | `salary_on_time` | Wrong tenant pin if mismatched | **FINDING** |
| `TEST_PLO_SCENARIO` | **empty** | `""` | DB/dpd wins (good) | ok |
| `TEST_SOT_SCENARIO` | **`pre`** | `""` (locked empty after FIX-B) | Forces SOT fixture scenario when SOT path used; **violates “fixture only when explicitly needed” intent on a shared UAT box** | **FINDING** |
| `TEST_SECTION` | `pre_closure` | `pre_closure` | test routing section | P2 |
| `STUB_MODE` / `LLM_STUB` / `KB_STUB` | `false` / `false` / `false` | all `true` in code | Live LLM/KB when false | ok on UAT |
| `TARGET_SAMPLE_RATE` | **`8000`** | DefaultConfig 8000; if unset + `CARRIER=asterisk` → **16000** | Silent 16k on asterisk | **FINDING** (mitigated by compose pin) |
| `CARRIER` | `asterisk` | `fonada` | Wrong carrier profile | — |
| `SARVAM_TTS_STREAMING` | `true` | **code default false** (`must be 1/true`) | Falls back to REST-primary if unset | **FINDING** (docs/examples disagree with code) |
| `SARVAM_TTS_MODEL` | `bulbul:v3` | `bulbul:v3` | — | ok |
| `SARVAM_TTS_PACE` | `1.1` | omit / nil | Sarvam default pace; per-turn brain pace still overrides when set | silent if unset |
| `SARVAM_TTS_SPEAKER` | `amit` | `amit` | Mid-call risk if hold inheritance broken | mitigated by R3 tests |
| `TTS_CACHE` | `1` | on (unless 0/false) | Cache on by default | silent default |
| `TTS_ENABLED` / `ASR_ENABLED` | `true` | **false** | **Noop = silence / deaf** | **FINDING** |
| `ASR_STREAMING` | `true` | true | — | ok |
| `ASR_LANGUAGE` | `hi-IN` | `unknown` | May omit language-code | P1 |
| `FALLBACK_NO_AUDIO_MS` | `5000` | 5000 | Holding-line arm delay | silent |
| `HOLDING_LINE` | (default) | `"ek minute"` | — | silent |
| `EGRESS_JITTER_MS` | `200` | 200 | edge budget in clear logs | — |
| `DEFAULT_CLIENT_ID` | (connector; listener stamps `salary-on-time`) | empty | Empty client_id | see A3 |
| `DEFAULT_TENANT_ID` | `default` (compose) | `default` | Shadow tenant | **FINDING** w/ A4 row |
| `BRAIN_WS_ENABLED` / `BRAIN_WS_URL` | true / `ws://brain:8000/ws/brain` | false / empty | No conversation if off | — |
| `WS_ASYNC_AUDIO_WRITES` | unset → **0** | 0 sync + drop-newest `toWS` | — | drop-newest landed in code path used when async off |
| `SCRIPTED_CATALOG_ROUTING` | (brain default true) | true | RAG demoted for scripted tenants | — |

**Silent defaults that can change call behaviour (summary):** `TEST_*` left on UAT; `TARGET_SAMPLE_RATE` asterisk fallback 16k; `SARVAM_TTS_STREAMING` code-false; `TTS_ENABLED`/`ASR_ENABLED` code-false; `DEFAULT_TENANT_ID=default`; empty phones → `phone_fallback_used` path; compliance `silent_reply` outside call window.

---

### A3. Origination paths

#### Path 1 — Dialplan AudioSocket (DIDs `172561700[4-9]`, lab Gosub)

- Dialplan sets **`CHANNEL(audioreadformat/writeformat)=slin16`**, then `AudioSocket(uuid, connector)`.
- Connector stamps **`client_id` from listener / `AUDIOSOCKET_TENANT_MAP`** (observed: `salary-on-time`); phones often empty; **no media-meta**.
- Rates: connector default / dialplan → **slin16 / 16000**.

**Traced `session_start` (empty-phone / 16k path):**
```json
{"type":"session_start","session_id":"3294ecb7…","client_id":"salary-on-time",
 "customer_phone":"","business_phone":"",
 "audio":{"codec":"slin16","input_sample_rate":16000,"output_sample_rate":16000,"channels":1},
 "metadata":{}}
```
Evidence: `journalctl -u asterisk-connector` 13:27–13:48 IST 2026-08-08.

**Silent degrade:** empty phones → go-server phone fallback / wrong borrower; 16k on 8k trunk → half-speed/crack class.

#### Path 2 — ARI / Stasis originate (SOT DID `1725617001` + PaisaLo outbound scripts)

- Stasis → orchestrator binds `CUSTOMER_PHONE` / `BUSINESS_PHONE` → media-meta → connector BYO.
- AI leg format forced **`slin` (8 kHz)** in orchestrator.
- Media-meta supplies phones + `client_id` in **metadata**; connector **does not overwrite** top-level `client_id` if listener already set it (`wsclient.go` only fills `client_id` from metadata when empty).

**Traced `session_start` (good PaisaLo PREDUE `d501f625`):**
```json
{"type":"session_start","session_id":"d501f625…","client_id":"salary-on-time",
 "customer_phone":"9810587857","business_phone":"1725617007",
 "audio":{"codec":"slin","input_sample_rate":8000,"output_sample_rate":8000,"channels":1},
 "metadata":{"business_phone":"1725617007","client_id":"paisalo","customer_phone":"9810587857"}}
```

| Field | Source | Silent degrade |
|---|---|---|
| `customer_phone` | media-meta / channel vars | empty → fallback WARN |
| `business_phone` | media-meta | empty |
| `client_id` (top-level) | **listener tenant `salary-on-time`**, NOT metadata | **metadata `paisalo` ignored when top-level set** |
| tenant at brain | UAT: `TEST_MODE`+`TEST_TENANT_ID=paisalo` pins PaisaLo | **Without TEST_MODE, brain would take `client_id=salary-on-time`** |
| codec/rates | media-meta SKU / G711 | wrong `media_streams` row → 16k |

**FINDING (P0):** PaisaLo ARI calls still advertise top-level `client_id=salary-on-time`. UAT “works” because brain `TEST_MODE` forces `paisalo`. Production-like `TEST_MODE=false` would mis-route.

---

### A4. `media_streams` / tenant DB audit

#### `orchestrator.media_streams` (via `ORCH_DATABASE_URL` → `172.18.0.1:5432/orchestrator`)

| tenant | sku | codec | in/out rate | media_ws_url | Flags |
|---|---|---|---|---|---|
| `aa_2967d4…` (loadtest) | **g722** | **slin16** | **16000/16000** | loadtest wss | **16k row still present** |
| `salary-on-time` | g711 | slin | 8000/8000 | `wss://voice-api.fonada.ai:18444/stream` | OK for 8k trunks |
| `paisalo` | **`plo`** (nonstandard SKU) | slin | 8000/8000 | `ws://172.18.0.1:8080/stream` | private URL; `allow_private_urls=t`; **same `secret_hash`/hint `ef01` as SOT** (dummy/shared secret smell) |

#### `collections.borrowers`

| id | name | phone | tenant_id | dpd | product | npa_flag |
|---|---|---|---|---|---|---|
| **`unknown`** | unknown | *(empty)* | **`default`** | null | | f |
| `PLO_RAMESH_PREDUE` | Ramesh | `+919810587857` | `paisalo` | -5 | ABF | f |

- **Rishabh-type stale row:** not present by name; **sentinel row `id=unknown` / `tenant_id=default` still exists**.
- **R2 query-proof:** code + tests exist:
  - `turn.py` / `borrower_resolve.py`: never `load_borrower("unknown"|"")`; phone-lookup ignores sentinel ids.
  - Locking tests: `test_malicious_id_unknown_phone_lookup_row_is_ignored`, `test_unseeded_ani_no_env_unknown_borrower_not_ramesh` — **PASS** on current tree (2026-08-08).
- Residual risk: empty-phone + `tenant=default` row can still confuse humans/ops; code path guarded.

---

### A5. Audio path invariants

**Good call `d501f625` rate log:**
```
msg=audio rates session_rate=8000 sarvam_rate=8000 asr_rate=8000
```

| Hop | Expected (ARI 8k path) | Independent config site? |
|---|---|---|
| PJSIP trunk | ulaw/alaw @ 8 kHz | `pjsip.conf` |
| Dialplan AudioSocket path | **slin16** (transcode) | `extensions.conf` — **diverges** |
| ARI AI leg | `slin` 8 kHz | `inbound.go` `audioSocketFormat` |
| Connector DefaultAudioSpec | slin16/16k | `wsclient.go` |
| Connector G711AudioSpec / BYO | slin/8k | media-meta driven |
| AudioSocket wire | always 8 kHz frames | `audiosocket/server.go` |
| `media_streams` | per-tenant 8k or **16k** | DB |
| go-server `TARGET_SAMPLE_RATE` | 8000 (UAT) | env; asterisk unset→16k |
| Carrier asterisk profile | in 16k / egress 24k defaults | `carrier.go` |
| Sarvam TTS/ASR | session rate | WS/query params |
| Egress framing | output_sample_rate | session |

**Count of independent rate/codec sites: ~23** (see explore evidence). **No single source of truth** — convergence today is operational discipline (`TARGET_SAMPLE_RATE=8000` + correct `media_streams` + ARI path).

---

### A6. TTS subsystem state (deployed go-server `852c792`)

| Behaviour | Present on `852c792`? | Locking test | CI / pre-deploy? |
|---|---|---|---|
| Deferred WS open | yes (`sarvam_tts_ws.go`) | `TestSarvamWSStream_ZeroSpeaksNoConnection`, `…FirstSpeakVoiceOverrideNoReconnect` | **Local `go test` only** — Websocket has **no** `.github/workflows` |
| First-Speak voice honour | yes | `TestSarvamWSStream_FirstSpeakVoiceOverrideNoReconnect` | local only |
| Hold-turn inheritance (parent) | yes (`ce583f3`) | `TestSarvamWSStream_HoldTurnInheritsParentVoice` | local only |
| Hold-turn inheritance (lastVoice) | yes (`852c792`) | `TestSarvamWSStream_HoldTurnInheritsSessionLastVoice` | local only |
| Cache serialization / multi-Speak | yes | `TestCachingTTSStream_MultiChunkChunk1Cached`, `…NotRecorded`, `tts_multi_producer` guard | local only |
| Cancel/clear on barge | yes | `TestTTSReplyConsumerCancelSuppressesAudio`, `TestCarrierEgressClearPlaybackDropsPending` | local only |
| REST fallback on WS error | yes | `TestSarvamWSStream_ErrorFrameTriggersRESTFallbackAndAudio` | local only |
| Pace clamp [0.5–2.0] | yes | `TestBuildSarvamTTSRequest_v3PaceClampAndOmitWhenUnset` | local only |
| v2 speaker remap | yes | `TestRemapSpeakerV2`, v3→v2 retry test | local only |
| Finalize after first audio | yes | `TestTTSReplyConsumer_DelayedFirstAudioNoEarlyFinalize` | local only |

Collection CI (`.github/workflows/ci.yml`) runs **pytest only** on Collection — does **not** gate Websocket TTS tests.

---

### A7. ASR subsystem state

| Item | Evidence |
|---|---|
| Streaming params | Query string: model `saaras:v3`, mode `transcribe`, `language-code`, `sample_rate`, `pcm_s16le`, `vad_signals`, `high_vad_sensitivity` (`sarvam_asr.go`) |
| Rate | Session wire else default 8000 |
| VAD | both high_vad + vad_signals **default true** |
| Endpoint latency (d501f625) | t2 `asr_endpoint_ms=126`; t3 `643`; t4 `171` (from `turn timing complete`) |
| Mouth-to-ear | t2 **1690 ms** (WARN vs target 1200) |
| Backpressure `toWS` | **drop-newest** landed (`bridge.go` / commit `254a84d`) |
| ASR reconnect buffer | drop-oldest when full (cap 8) |
| ASR event channel full | drop newest event |
| WS drop mid-call | reconnect up to MaxReconnects(5); then `"continuing without transcripts"` / `"asr send failed; continuing call"` — **hearing dies quietly** |

---

### A8. Turn lifecycle (good call `d501f625`, PREDUE)

Observed sequence (go-server logs, UTC morning = IST ~14:01):

1. `session_start` 8k + phones (A3) → `audio rates` 8000/8000/8000  
2. Opener t1: `engine_ms=994`, `tts_first_audio_ms=1051`, `tts_path=ws`, `asr_path=ws`  
3. t2: ASR final → brain → TTS → egress; mouth-to-ear 1690  
4. t3/t4: `disposition=superseded` (barge/overlap) — watermark advanced; prior frames dropped  
5. Session close: `asr_errors=0`, denoise fallbacks=0, brain WS read ended on hangup  

| Invariant | Status on this call |
|---|---|
| Watermark monotonic | yes (supersede dispositions; egress code `egress_carrier.go`) |
| Finalize after first audio | armed by design (`DelayedFirstAudioNoEarlyFinalize` test); opener showed first audio before complete |
| Holding-line | `FALLBACK_NO_AUDIO_MS=5000`; not observed firing on this call |
| Barge / clear | connector supports `{"type":"clear"}`; go-server Clear emits frame (W-A) |
| Hangup | orch `inbound call: torn down` + go-server `session closed` |

#### Silent failure / dead-air points (every known)

| # | Failure | Result |
|---|---|---|
| 1 | `TTS_ENABLED=false` / Noop TTS | silence |
| 2 | `tts speak failed` warn-only | silence |
| 3 | Empty reply text early-return | silence until holding-line timer |
| 4 | Brain gate outside window / attempt cap → `silent_reply=""` | silence (intentional compliance) |
| 5 | Brain connect / opener fail | warn + return — no opener audio |
| 6 | `ASR_ENABLED=false` | permanent deaf |
| 7 | ASR reconnect exhausted | continue without transcripts |
| 8 | Holding-line only after `FALLBACK_NO_AUDIO_MS` | dead air window |
| 9 | AMD machine / detecting gate | ingress held |
| 10 | Wrong sample rate | crack/half-speed (not silence, but call-kill) |

---

### A9. Observability gaps

| Log / signal | Present? | Notes |
|---|---|---|
| `audio rates` (session/sarvam/asr) | **yes** | d501f625 |
| `phone_fallback_used` | **yes** (WARN) | **0 hits in last 72h** on go-server |
| `tts_ws_fallback` | **yes** | code + tests |
| `tts_multi_producer` | **yes** | cache guard |
| voice-change | partial | `"sarvam ws voice changed, reopening connection"` — no `voice_change_count` summary field |
| clear / `pending_dropped` | **yes** | egress clear logs |
| `log_turn_decision` | **yes** (brain) | `turn_decision_log.py` |
| `allowlisted: true` | **yes** | gate warnings |
| rate mismatch WARN | **yes** | `audio rate mismatch` |
| brain `/version` | **missing** | |
| orch build stamp | **missing** | |
| single per-call triage line | **missing** | propose below |

**Proposed per-call summary line (do not build now):**
```
call_summary session_id=… tenant=… borrower_id=… scenario=… phone_fallback=0|1
  rates=session/sarvam/asr voice_id=… voice_changes=N tts_path=ws|rest tts_fallbacks=N
  asr_errors=N superseded_turns=N mouth_to_ear_p50=… gate_allowlisted=N gate_blocks=N
  disposition=… end_call=bool
```
Emit once on `session closed` from go-server (join brain decision counts via session_id).

---

## PART B — PAISALO FLOW vs PDF

Sources: `scripts/_paisalo_pdf_fixed.txt`, `app/flows/paisalo/*.yml`, `app/tenants/paisalo.yml`, goldens.

### B1. Coverage matrix

#### Scenario routing

| PDF scenario | Flow | Entry after opener identity | Selector (`select_plo_scenario`) |
|---|---|---|---|
| PREDUE | `plo_predue` | `wait_intent` | `dpd < 0` |
| ONDUE | `plo_ondue` | `wait_intent` | `dpd == 0` |
| POSTDUE1 | `plo_postdue1` | `greet_ask` | 1–30 |
| POSTDUE2 | `plo_postdue2` | `greet_ask` | 31–60 |
| POSTDUE3 | `plo_postdue3` | `greet_ask` | 61+ |
| NPA | `plo_npa` | `greet` (consent) | `npa_flag` |

**ABF vs MFI:** PDF differs (PD1/2 dealer warning line; NPA “paid to dealer” absent on MFI extract). Engine hydrates `product` but **never branches copy on it** — **silent ABF-biased copy for all**. Voice is scenario-keyed (priya/neha/kabir/amit), not product.

#### Objections (shared postdue catalog ≈ ABF Neha forms)

| PDF item | Flow / reply | Attempt 1st/2nd | Verbatim? |
|---|---|---|---|
| Will not pay | `plo_obj_will_not_pay` / pd*_refuse | **yes** | near; family hits allowlist |
| Where to pay / Lost QR | `plo_obj_where_to_pay` / `lost_qr` | no | covered |
| Denies loan | `plo_obj_deny_loan_pd` | no | **PD3 PDF Arjun legal copy not implemented** (shared Neha) |
| Political waiver / multi / which EMI / will you pay / assurance / already paid / callback / official / personal / death / new loan | matching `plo_obj_*_pd` | dealer_pay **yes**; else mostly no | already-paid **dropped “खाता NPA हो चुका है” clause** vs PDF |
| Paid to dealer | `plo_obj_dealer_pay_pd` | **yes** | always present (MFI PDF gap) |
| PREDUE/ONDUE objections | n/a in PDF (IVR) | — | soft push only in flows |

NPA objections: matching `plo_obj_npa_*` / `plo_obj_deny_loan` / dealer / etc. — broadly covered; see B3.

---

### B2. Opener restructure regression

**FIX-A structure** (`flows/paisalo/opener.yml`): `select_plo_scenario` → per-scenario greet → identity → `plo_chain_*`.

| Chain action | Target | First step |
|---|---|---|
| `plo_chain_predue/ondue` | `plo_predue` / `plo_ondue` | `wait_intent` |
| `plo_chain_postdue{1,2,3}` | `plo_postdue*` | `greet_ask` |
| `plo_chain_npa` | `plo_npa` | `greet` (consent) |

**Golden re-run (current tree `2d35999`, 2026-08-08):**
```
pytest tests/golden/test_paisalo_scenarios.py tests/unit/test_paisalo_hydration.py tests/unit/test_gate_allowlist.py
→ 17 passed, 1 failed
```
- **FAIL:** `test_paisalo_npa_out_of_context_question` (P6-debt — see B6)  
- **PASS:** 6 scenario happy paths + NPA refuse×2 escalate + 5 hydration/FIX-A/R2 tests + gate allowlist unit tests  

CP5 goldens largely still green post-FIX-A; the OOC golden was already broken at blank-transcript belt, not by FIX-A itself.

**NPA order drift vs PDF:** opener speaks identity first (`plo_npa_opener_identity`); PDF AI order is closer to consent greeting → identity → disclosure. Functionally covered; sequence differs.

---

### B3. NPA ladder vs PDF

| PDF step | Status | Flow / reply refs |
|---|---|---|
| Identity | exists (order drifted) | opener `wait_identity` / `plo_npa_opener_identity` |
| Consent 2 min | exists | `plo_npa.greet` / `plo_npa_greeting` |
| NPA disclosure + timeline | exists | `disclosure` / `plo_npa_disclosure` |
| Refuse ×2 → branch referral END_CALL | exists + golden | `refuse_loop` → `plo_npa_escalate_branch` → hangup |
| Willing today / date | exists | `plo_npa_assurance_today` / `_date` |
| Callback → END_CALL | exists (consent-no path) | `callback_ask` → `callback_ack` → hangup |
| Death → condolence → END | exists | `plo_obj_npa_death` |
| Third-party | exists; **no callback capture before END** (PDF wants capture) | `plo_obj_npa_third_party` |
| Wrong number | exists + END | opener + `plo_obj_npa_wrong_number` |
| Angry / frustrated | exists | `plo_obj_npa_angry` |
| Out-of-context (answer-then-return) | **partial** (Tier-3 `respond` + `unknown_info_reply`); golden broken | no dedicated flow |

---

### B4. Variables

| PDF var | Slot | Hydration | Seeded on `PLO_RAMESH_PREDUE`? | Gap |
|---|---|---|---|---|
| customer_name | name / customer_name | DB | yes (Ramesh) | ok |
| repay_amount / amount | repay_amount | DB | 4500 | ok |
| days_past_due | days_past_due / dpd | DB | -5 | ok |
| **days_past_due_words** | **none** | — | — | PDF NPA which-EMI uses words; flow uses `{days_past_due}` numeric |
| committed_date | committed_date | **not in `_HYDRATION_LOAN_KEYS`** | no | runtime LLM/set_slot only; tenant comment lists it |
| branch / branch_address | yes | DB | Kanpur City / 12 MG Road | ok |
| last_date_paid | yes | DB | 2026-07-13 | ok |
| loan_amount / disbursal_date | yes | DB | yes | ok |
| product | product | DB | ABF | **unused for copy** |
| New-loan phone digit-by-digit | hard-coded `+918035317323` | none | n/a | **not spoken as Hindi digits**; TTS will read symbol form |

---

### B5. Gate / allowlist live-readiness

**8 phrases** (`paisalo.yml`):  
`कानूनी कार्रवाई`, `सख़्त कार्रवाई`, `सख्त कार्रवाई`, `कानूनी परिणाम`, `कानूनी प्रक्रिया`, `NPA घोषित`, `खाता NPA`, `डिफॉल्ट हो सकता`

| Check | Evidence |
|---|---|
| Which replies contain them | `scripts/_p5_gate_hits.txt` — pd1/2/3 refuse, will_not_pay, deny_loan_pd, pd3 greeting, npa refuse/callback, dealer_pay_pd, waiver_political_pd, … |
| `allowlisted:true` logging | `test_gate_allowlist.py::test_allowlisted_phrase_passes_and_logs` **PASS**; dry-run 16 allowlisted warnings, 0 residual blocks |
| predue/ondue zero hits | dry-run section: no pressure hits in those YAML files |
| Live postdue3/NPA | **not re-proven on a live call this audit** (no live calls); unit + dry-run ready |

---

### B6. Known debt reverify

| Test | Classification still holds? | Effort if fixing later |
|---|---|---|
| `test_paisalo_npa_out_of_context_question` | **YES** — still fails; fixture uses `text=` but `TurnRequest` only has `transcript=` → blank transcript → respond stripped (`cfc5ed2` belt). Confirmed 2026-08-08 fail asserting reask timeline instead of unknown_info/helpline | **~5 min:** rename `text=`→`transcript=` in `_turn` helper |
| `test_on_due_willing_goes_to_commit_today` | **YES** — fails `reply_id == 'sotod_offer'` vs expected `sot_confirm_today` (FIRST-utter semantics from B2/`a038528`) | **~5 min:** assert `utter_chain[-1]` or first/last explicitly |

Production WS path uses `transcript=` — OOC production path OK; golden is fixture-wrong.

---

## Gap Register

| ID | Layer | Finding | Evidence | Severity | Suggested owner |
|---|---|---|---|---|---|
| G-A1-01 | Deploy | Websocket checkout **dirty**: live-only postgres port publish `172.18.0.1:5432` | `git status` `M deploy/docker-compose.yml`; `852c792-dirty` | P1 | Platform / Nitish |
| G-A1-02 | Deploy | Brain & orchestrator **lack usable `/version` build stamps** | brain no endpoint; orch `/version` → 401 | P2 | go-server done; brain+orch |
| G-A1-03 | Deploy | Brain host port unpublished — hard to verify health from host | `docker ps` shows `8000/tcp` only | P2 | Platform |
| G-A2-01 | Config | UAT **`TEST_MODE=true` + `TEST_TENANT_ID=paisalo`** pins tenant; masks client_id bugs | container env; `handler.py` test_mode branch | **P0** | Brain / Nitish |
| G-A2-02 | Config | UAT **`TEST_SOT_SCENARIO=pre` set** on shared stack (should be empty unless intentional) | container env vs code default `""` | P1 | Brain / Nitish |
| G-A2-03 | Config | `SARVAM_TTS_STREAMING` **code default false** vs UAT/docs true | `sarvam_tts_ws.go` | P1 | go-server |
| G-A2-04 | Config | `TTS_ENABLED`/`ASR_ENABLED` default false → silence/deaf if env forgotten | `tts.go` / `asr.go` | P1 | go-server |
| G-A2-05 | Config | `TARGET_SAMPLE_RATE` unset under asterisk → **16000** | `cmd/server/main.go` | P1 | go-server |
| G-A3-01 | Originate | PaisaLo ARI `session_start.client_id` stays **`salary-on-time`**; metadata `paisalo` does not override | connector journal `d501f625` payload; `wsclient.go` fill-if-empty | **P0** | connector / orch |
| G-A3-02 | Originate | Dialplan AudioSocket path still emits **empty phones + slin16/16k** | journal 13:27/13:33/13:48 sessions | P1 | dialplan / connector |
| G-A4-01 | DB | `media_streams` row **`aa_2967…` still 16k/g722** | orch SQL dump | P1 | orch DB |
| G-A4-02 | DB | `borrowers` row **`id=unknown` tenant=default`** still present | collections SQL | P1 | brain DB / ops |
| G-A4-03 | DB | paisalo + SOT share **same media secret_hash/hint `ef01`**; paisalo SKU=`plo` odd | media_streams dump | P2 | orch / security |
| G-A5-01 | Media | **~23 independent rate sites**; dialplan slin16 vs ARI slin8k diverge | code map A5 | P1 | architecture |
| G-A6-01 | TTS/CI | TTS locking tests **not in CI** (no Websocket workflows) | repo has 0 `.github/workflows` under Websocket | P1 | Platform |
| G-A7-01 | ASR | ASR failure → **continue without transcripts** (quiet deaf) | `asr_sink.go` | **P0** (dead-air class) | go-server |
| G-A8-01 | Turn | Multiple **silence** failure modes (TTS noop, empty reply, gate silent_reply, holding delay) | A8 table | **P0**/P1 mix | go-server + brain |
| G-A9-01 | Obs | No single **per-call summary** line | A9 proposal | P2 | go-server |
| G-B1-01 | PaisaLo | **No ABF/MFI copy branching** — silent ABF text to MFI | `product` unused in selector/NLG; PDF diffs | P1 | Content / brain |
| G-B1-02 | PaisaLo | PD3 deny-loan / already-paid **copy drift** vs PDF | objections.yml vs PDF extract | P2 | Content |
| G-B3-01 | PaisaLo | NPA third-party path **missing callback capture before END** | `plo_obj_npa_third_party` | P1 | Flows |
| G-B3-02 | PaisaLo | NPA opener **identity-before-consent** order ≠ PDF | opener.yml vs PDF | P2 | Flows |
| G-B4-01 | PaisaLo | **`days_past_due_words` missing**; numeric substituted | which-EMI reply | P2 | Flows |
| G-B4-02 | PaisaLo | **`committed_date` not hydrated** from DB | tracker keys vs paisalo.yml comment | P1 | brain hydration |
| G-B4-03 | PaisaLo | New-loan phone **not digit-by-digit** for TTS | literal `+918035317323` in YAML | P1 | Content / TTS |
| G-B6-01 | Tests | P6-debt OOC golden still broken (`text=` vs `transcript=`) | pytest fail 2026-08-08 | P2 | Tests (5-min) |
| G-B6-02 | Tests | P6-debt ondue willing `reply_id` FIRST vs LAST | pytest fail `sotod_offer` | P2 | Tests (5-min) |

---

## STOP

Audit complete. No code changes, no deploys, no live calls performed for this deliverable.  
Next actions require Nitish sign-off (priority suggested: **G-A3-01 + G-A2-01** tenant routing, then **G-A7-01/G-A8-01** dead-air, then scenario ladder live tests).
