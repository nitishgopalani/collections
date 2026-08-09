"""PREDUE-012 final silent smoke: originate to 9810587857, hold 25s, hangup.

Verify the 9 criteria from WORKLOG #012 §6. The 4 NEW ones (DEBT-034/035):
  [1] "sarvam tts ws pre-opened at session_ready speaker=simran" BEFORE any speak
  [2] t1 log: llm_calls=0, command_gen ~= 0ms
  [3] zero "dropping oldest audio frame" lines (DEBT-035 buffer)
  [4] prewarm log: warm_ms for opener+apology lines (wired; requires TTS_PREWARM_LINES env)
Plus the 5 existing (F1/F2/F3/Z1/Z2):
  [5] tools_client=simulate (F1)
  [6] source=client_id client_id=paisalo (F2)
  [7] apology_voice_id=simran (F3 + Z2)
  [8] sarvam tts ws session opened speaker=simran (Z2)
  [9] opener reply text: identity ask + NO loan facts (no Rs/Rupee/kisht) (Z1)

Nitish: answer 9810587857 PROMPTLY when it rings and STAY SILENT for ~25s (no probes).
"""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''
ARI_USER=$(grep ^ARI_USER /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)
ARI_PASS=$(grep ^ARI_PASSWORD /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)

echo '=== PREDUE-012 FINAL SILENT SMOKE: 9810587857 (paisalo, 25s hold) ==='
date '+%Y-%m-%d %H:%M:%S %Z'

echo '--- pre: hang up lingering channels ---'
for cid in $(asterisk -rx 'core show channels' 2>/dev/null | awk '/PJSIP\/ng_trunk|Local\/s@/{print $1}'); do
  asterisk -rx "channel request hangup $cid" 2>&1
done

echo '--- pre: TTS_PREWARM_LINES env on go-server (for criterion #4) ---'
docker exec -i fonada-voice-go-server-1 sh -c 'echo "TTS_PREWARM_LINES=${TTS_PREWARM_LINES:-<not configured>}"' 2>&1

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
echo "NITISH: answer 9810587857 PROMPTLY when it rings and STAY SILENT for ~25s (smoke only, no probes)"
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

echo; echo '########## GO-SERVER: session_ready + TTS WS pre-open + speaker ##########'
docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) \
  | grep -iE 'session_ready|apology_text|apology_voice|SetApologyLine|sarvam tts ws (pre-opened|session opened)|8000|prewarm' | grep -v 'sarvam ws send' | head -25

echo; echo '########## SMOKE VERDICT (9 criteria) ##########'
BRAIN_SS=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'brain ws session_start' | head -1)
BRAIN_TC=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'tools_client' | head -1)
CONN_SRC=$(journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n +$((CONN_MARK+1)) | grep -i 'client_id_source' | head -1)
GO_APV=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'apology_voice_id' | head -1)
GO_SPK_OPEN=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'sarvam tts ws pre-opened' | head -1)
GO_SPK=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'sarvam tts ws session opened' | head -1)
GO_PREWARM=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'tts prewarm' | head -1)
GO_BP=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -iE 'dropping oldest audio frame|setup_buffer_drops|asr drained setup buffer' | head -5)
BRAIN_LLM=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -iE 'llm_calls=0|command_gen' | head -3)
OPENER=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'turn_decision' | grep -ioE 'reply_text="[^"]*"' | head -1)

echo "[5] brain_session_start: $BRAIN_SS"
echo "[5] brain_tools_client: $BRAIN_TC"
echo "[6] connector_client_id_source: $CONN_SRC"
echo "[7] go_server_apology_voice: $GO_APV"
echo "[8] go_server_speaker: $GO_SPK"
echo "[1] go_server_tts_ws_preopen: $GO_SPK_OPEN"
echo "[2] brain_llm_calls/command_gen: $BRAIN_LLM"
echo "[3] go_server_backpressure: $GO_BP"
echo "[4] go_server_prewarm: $GO_PREWARM"
echo "[9] opener_reply: $OPENER"

echo; echo '--- verdicts ---'
echo "$BRAIN_TC" | grep -qi 'tools_client=simulate' && echo "[5] F1_TOOLS_CLIENT_SIMULATE (PASS)" || echo "[5] F1_TOOLS_CLIENT_CHECK_FAIL: $BRAIN_TC"
echo "$CONN_SRC" | grep -qi 'source=client_id' && echo "[6] F2_SOURCE_CLIENT_ID (PASS)" || echo "[6] F2_SOURCE_CHECK_FAIL: $CONN_SRC"
echo "$GO_APV" | grep -qi 'apology_voice_id=simran' && echo "[7] F3_Z2_APOLOGY_VOICE_SIMRAN (PASS)" || echo "[7] F3_Z2_APOLOGY_VOICE_CHECK_FAIL: $GO_APV"
echo "$GO_SPK" | grep -qi 'speaker=simran' && echo "[8] Z2_SPEAKER_SIMRAN (PASS)" || echo "[8] Z2_SPEAKER_CHECK_FAIL: $GO_SPK"
echo "$GO_SPK_OPEN" | grep -qi 'pre-opened' && echo "[1] DEBT034_ITEM1_TTS_WS_PREOPEN (PASS)" || echo "[1] DEBT034_ITEM1_PREOPEN_CHECK_FAIL: $GO_SPK_OPEN"
echo "$BRAIN_LLM" | grep -qi 'llm_calls=0' && echo "[2] DEBT034_ITEM2_LLM_SKIP (PASS)" || echo "[2] DEBT034_ITEM2_LLM_SKIP_CHECK_FAIL: $BRAIN_LLM"
if [[ -z "$GO_BP" || "$GO_BP" == *"setup_buffer_drops=0"* ]]; then echo "[3] DEBT035_ZERO_BACKPRESSURE (PASS)" ; else echo "[3] DEBT035_BACKPRESSURE_CHECK_FAIL: $GO_BP"; fi
if [[ -n "$GO_PREWARM" ]]; then echo "[4] DEBT034_ITEM3_PREWARM (PASS): $GO_PREWARM"; else echo "[4] DEBT034_ITEM3_PREWARM_NOT_CONFIGURED (wired, needs TTS_PREWARM_LINES env)"; fi
echo "$OPENER" | grep -qiE 'Rs|₹|kisht|किश्त|किस्त' && echo "[9] Z1_FACT_TOKENS_PRESENT (FAIL): $OPENER" || echo "[9] Z1_NO_FACT_TOKENS (PASS)"

echo PREDUE012_FINAL_SMOKE_DONE
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
    with sftp.file("/tmp/_predue012_final_smoke.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_predue012_final_smoke.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_predue012_final_smoke.sh", timeout=120)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_predue012_final_smoke.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    if err.strip():
        print("STDERR", err[:1500])
    print(f"\nWrote {out}")


main()
