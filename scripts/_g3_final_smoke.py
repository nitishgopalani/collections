"""G3 final W1 silent smoke: originate to 9810587857, hold 25s, hangup.

Verify the deploy + DEBT-039 (preempt sim shows spoken close) + DEBT-040
(drain-ready gate + zero ingress drops). Criteria:
  [1] sarvam tts ws pre-opened at session_ready speaker=simran (DEBT-034)
  [2] t1 log: llm_calls=0, command_gen ~= 0ms (DEBT-034)
  [3] zero "dropping oldest audio frame" lines (DEBT-035 + DEBT-040 enlarge)
  [4] egress drain-ready gate released (on first ingress frame OR timeout) (DEBT-040)
  [5] tools_client=simulate (F1)
  [6] source=client_id client_id=paisalo (F2)
  [7] apology_voice_id=simran (F3 + Z2)
  [8] sarvam tts ws session opened speaker=simran (Z2)
  [9] opener reply text: identity ask + NO loan facts (Z1)
  [10] DEBT-039 preempt sim: DNC cue -> on_gated_reply called with dnc_ack text (spoken close)

Nitish: answer 9810587857 PROMPTLY when it rings and STAY SILENT for ~25s.
"""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''
ARI_USER=$(grep ^ARI_USER /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)
ARI_PASS=$(grep ^ARI_PASSWORD /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)

echo '=== G3 FINAL W1 SILENT SMOKE: 9810587857 (paisalo, 25s hold) ==='
date '+%Y-%m-%d %H:%M:%S %Z'

echo '--- pre: hang up lingering channels ---'
for cid in $(asterisk -rx 'core show channels' 2>/dev/null | awk '/PJSIP\/ng_trunk|Local\/s@/{print $1}'); do
  asterisk -rx "channel request hangup $cid" 2>&1
done

echo '--- pre: DRAIN_READY_TIMEOUT_MS + AUDIO_BUFFER_SIZE env on go-server (DEBT-040) ---'
docker exec -i fonada-voice-go-server-1 sh -c 'echo "AUDIO_BUFFER_SIZE=${AUDIO_BUFFER_SIZE:-<default 64>}"' 2>&1
docker exec -i fonada-voice-go-server-1 sh -c 'echo "DRAIN_READY_TIMEOUT_MS=${DRAIN_READY_TIMEOUT_MS:-<default 2000>}"' 2>&1

echo; echo '########## DEBT-039 PREEMPT SIM (DNC cue -> spoken close) ##########'
echo '--- run a simulated DNC preempt inside the deployed brain ---'
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
import asyncio, inspect
from app.engine.turn import handle_turn
from app.engine.tracker import new_conversation_state
from app.clients.tools_sim import FakeToolClient
from app.memory.store import InMemoryMemoryStore

class _EmptyKB:
    is_stub = False
    async def ping(self): return True
    async def retrieve(self, text, tenant_id, k=6): return []

class _NoLLM:
    is_stub = False
    call_count = 0
    async def ping(self): return True
    async def complete(self, system, user, *, json_only=True, **kw):
        _NoLLM.call_count += 1
        return "[]"

class _Recorder:
    def __init__(self): self.calls = []
    async def __call__(self, reply_text, *, voice_id=None, tts_model=None, tts_pace=None):
        self.calls.append({"text": reply_text, "voice_id": voice_id})

async def run():
    memory = InMemoryMemoryStore()
    state = new_conversation_state("smoke-debt039-dnc", "paisalo", "smoke_borrower")
    state.slots["customer_name"] = "Ramesh"
    state.slots["identity_ok"] = True
    memory._states["smoke-debt039-dnc"] = state.model_copy(deep=True)
    rec = _Recorder()
    from app.schemas.api import TurnRequest
    req = TurnRequest(call_id="smoke-debt039-dnc", borrower_id="smoke_borrower",
                      tenant_id="paisalo", channel="voice", locale="hi-IN",
                      transcript="dobara call mat karna",
                      turn_meta={"force_flow": "plo_opener", "call_date": "2026-08-09"})
    resp = await handle_turn(req, memory=memory, llm=_NoLLM(),
                             tools=FakeToolClient(), kb=_EmptyKB(),
                             on_gated_reply=rec)
    print("debt039_sim_disposition:", resp.disposition)
    print("debt039_sim_end_call:", resp.end_call)
    print("debt039_sim_reply_text_len:", len(resp.reply_text or ""))
    print("debt039_sim_on_gated_reply_calls:", len(rec.calls))
    if rec.calls:
        print("debt039_sim_close_voice_id:", rec.calls[0]["voice_id"])
        print("debt039_sim_close_has_darj:", "darj" in resp.reply_text.lower() or "दर्ज" in resp.reply_text)
        print("debt039_sim_close_no_ruppee:", "\u20b9" not in resp.reply_text)
        print("debt039_sim_close_no_kisht:", "\u0915\u093f\u0936\u094d\u0924" not in resp.reply_text)

asyncio.run(run())
PY

CONN_MARK=$(journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | wc -l)
GO_MARK=$(docker logs fonada-voice-go-server-1 2>&1 | wc -l)
BRAIN_MARK=$(docker logs fonada-voice-brain-1 2>&1 | wc -l)
echo "marks: conn=$CONN_MARK go=$GO_MARK brain=$BRAIN_MARK"

echo; echo '--- ORIGINATE PJSIP/9810587857@ng_trunk ---'
START=$(date +%s)
CHAN=$(curl -sS -m 25 -u "$ARI_USER:$ARI_PASS" -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:8088/ari/channels?endpoint=PJSIP/9810587857@ng_trunk&app=fonada-orchestrator&appArgs=inbound,paisalo,127.0.0.1:9092&callerId=1725617001&timeout=30" \
  -d '{"variables":{"CUSTOMER_PHONE":"+919810587857","BUSINESS_PHONE":"1725617001"}}')
echo "originate_resp=$CHAN" | head -c 400; echo
CHAN_ID=$(echo "$CHAN" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)
echo "channel_id=$CHAN_ID"
echo "NITISH: answer 9810587857 PROMPTLY when it rings and STAY SILENT for ~25s"
echo "HOLDING 25s..."
sleep 25
ELAPSED=$(($(date +%s)-START))
echo "elapsed=${ELAPSED}s"

echo; echo '--- hangup via ARI ---'
if [[ -n "$CHAN_ID" ]]; then
  curl -sS -m 10 -u "$ARI_USER:$ARI_PASS" -X DELETE "http://127.0.0.1:8088/ari/channels/${CHAN_ID}" 2>/dev/null || true
fi
sleep 3

echo; echo '########## BRAIN: session_start + opener turn_decision ##########'
docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) \
  | grep -iE 'brain ws (tenant resolved|session_start)|turn_decision|tools_client|llm_calls|command_gen' | head -25

echo; echo '--- opener reply text (Z1: identity ask, NO Rs/Rupee/kisht) ---'
docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) \
  | grep -iE 'turn_decision' | grep -ioE 'reply_text="[^"]*"' | head -3

echo; echo '########## CONNECTOR: client_id + source (F2) ##########'
journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n +$((CONN_MARK+1)) \
  | grep -iE 'client_id resolved|client_id_source|send session_start|audio rates|rate=' | head -15

echo; echo '########## GO-SERVER: session_ready + TTS WS pre-open + speaker + drain-ready gate ##########'
docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) \
  | grep -iE 'session_ready|apology_text|apology_voice|SetApologyLine|sarvam tts ws (pre-opened|session opened)|8000|prewarm|drain-ready|dropping oldest audio frame' | grep -v 'sarvam ws send' | head -30

echo; echo '########## SMOKE VERDICT (10 criteria) ##########'
BRAIN_SS=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'brain ws session_start' | head -1)
BRAIN_TC=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'tools_client' | head -1)
CONN_SRC=$(journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n +$((CONN_MARK+1)) | grep -i 'client_id_source' | head -1)
GO_APV=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'apology_voice_id' | head -1)
GO_SPK_OPEN=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'sarvam tts ws pre-opened' | head -1)
GO_SPK=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'sarvam tts ws session opened' | head -1)
GO_BP=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'dropping oldest audio frame' | head -5)
GO_DRAIN=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'drain-ready gate' | head -3)
BRAIN_LLM=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -iE 'llm_calls=0|command_gen' | head -3)
OPENER=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'turn_decision' | grep -ioE 'reply_text="[^"]*"' | head -1)

echo "[5] brain_tools_client: $BRAIN_TC"
echo "[6] connector_client_id_source: $CONN_SRC"
echo "[7] go_server_apology_voice: $GO_APV"
echo "[8] go_server_speaker: $GO_SPK"
echo "[1] go_server_tts_ws_preopen: $GO_SPK_OPEN"
echo "[2] brain_llm_calls/command_gen: $BRAIN_LLM"
echo "[3] go_server_backpressure_drops: $GO_BP"
echo "[4] go_server_drain_ready_gate: $GO_DRAIN"
echo "[9] opener_reply: $OPENER"

echo; echo '--- verdicts ---'
echo "$BRAIN_TC" | grep -qi 'tools_client=simulate' && echo "[5] F1_TOOLS_CLIENT_SIMULATE (PASS)" || echo "[5] F1_TOOLS_CLIENT_CHECK_FAIL: $BRAIN_TC"
echo "$CONN_SRC" | grep -qi 'source=client_id' && echo "[6] F2_SOURCE_CLIENT_ID (PASS)" || echo "[6] F2_SOURCE_CHECK_FAIL: $CONN_SRC"
echo "$GO_APV" | grep -qi 'apology_voice_id=simran' && echo "[7] F3_Z2_APOLOGY_VOICE_SIMRAN (PASS)" || echo "[7] F3_Z2_APOLOGY_VOICE_CHECK_FAIL: $GO_APV"
echo "$GO_SPK" | grep -qi 'speaker=simran' && echo "[8] Z2_SPEAKER_SIMRAN (PASS)" || echo "[8] Z2_SPEAKER_CHECK_FAIL: $GO_SPK"
echo "$GO_SPK_OPEN" | grep -qi 'pre-opened' && echo "[1] DEBT034_ITEM1_TTS_WS_PREOPEN (PASS)" || echo "[1] DEBT034_ITEM1_PREOPEN_CHECK_FAIL: $GO_SPK_OPEN"
echo "$BRAIN_LLM" | grep -qi 'llm_calls=0' && echo "[2] DEBT034_ITEM2_LLM_SKIP (PASS)" || echo "[2] DEBT034_ITEM2_LLM_SKIP_CHECK_FAIL: $BRAIN_LLM"
if [[ -z "$GO_BP" ]]; then echo "[3] DEBT035_040_ZERO_BACKPRESSURE (PASS)"; else echo "[3] DEBT035_040_BACKPRESSURE_CHECK_FAIL: $GO_BP"; fi
if [[ -n "$GO_DRAIN" ]]; then echo "[4] DEBT040_DRAIN_READY_GATE (PASS): $GO_DRAIN"; else echo "[4] DEBT040_DRAIN_READY_GATE_CHECK_FAIL: no drain-ready log"; fi
echo "$OPENER" | grep -qiE 'Rs|₹|kisht|किश्त|किस्त' && echo "[9] Z1_FACT_TOKENS_PRESENT (FAIL): $OPENER" || echo "[9] Z1_NO_FACT_TOKENS (PASS)"

echo G3_FINAL_W1_SMOKE_DONE
'''


def main():
    cfg = {}
    sec = ROOT / ".deploy_secret"
    for line in sec.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    script = REMOTE.replace("\r\n", "\n")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["SSH_HOST"], port=int(cfg["SSH_PORT"]),
               username=cfg["SSH_USER"], password=cfg["SSH_PASSWORD"],
               timeout=45, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.file("/tmp/_g3_final_smoke.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_g3_final_smoke.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_g3_final_smoke.sh", timeout=120)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_g3_final_smoke.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    if err.strip():
        print("STDERR", err[:1500])
    print(f"\nWrote {out}")


main()
