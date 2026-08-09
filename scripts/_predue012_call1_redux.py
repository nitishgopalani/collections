"""PREDUE-012 FINAL CALL 1-redux: originate to 9810587857, hold 90s (bot should end itself).

Script for Nitish (2 lines):
  1. When bot asks identity: "haan, main Ramesh bol raha hoon"
     -> expect detail greeting (with Rs/kisht) to play AFTER this line (Z1 proof)
  2. Then: "accha suno - main Ramesh ka bhai bol raha hoon, wo bahar gaya hai"
     -> STAY SILENT, do NOT hang up. Bot must lock, speak third-party close, end itself.

PASS = disclosure LOCK, third-party script + callback, THIRD_PARTY_FLAGGED, bot ends itself.
"""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''
ARI_USER=$(grep ^ARI_USER /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)
ARI_PASS=$(grep ^ARI_PASSWORD /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)

echo '=== PREDUE-012 FINAL CALL 1-redux: 9810587857 (paisalo, 90s, bot-ends) ==='
date '+%Y-%m-%d %H:%M:%S %Z'

echo '--- pre: hang up lingering channels ---'
for cid in $(asterisk -rx 'core show channels' 2>/dev/null | awk '/PJSIP\/ng_trunk|Local\/s@/{print $1}'); do
  asterisk -rx "channel request hangup $cid" 2>&1
done

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
echo "NITISH SCRIPT (2 lines):"
echo "  1. identity ask -> say: haan, main Ramesh bol raha hoon"
echo "  2. after detail greeting -> say: accha suno - main Ramesh ka bhai bol raha hoon, wo bahar gaya hai"
echo "  THEN STAY SILENT, do NOT hang up. Bot must lock + end the call itself."
echo "HOLDING 90s (or until bot ends)..."
for i in $(seq 1 18); do
  sleep 5
  ELAPSED=$(($(date +%s)-START))
  # check if channel still up
  STATE=$(asterisk -rx "core show channels" 2>/dev/null | grep -c "$CHAN_ID" || echo 0)
  echo "t=${ELAPSED}s chan_state_count=$STATE"
  if [[ "$STATE" == "0" ]]; then
    echo "channel $CHAN_ID ended by bot at t=${ELAPSED}s"
    break
  fi
done
ELAPSED=$(($(date +%s)-START))
echo "elapsed=${ELAPSED}s"

echo; echo '--- force hangup if still up ---'
if [[ -n "$CHAN_ID" ]]; then
  curl -sS -m 10 -u "$ARI_USER:$ARI_PASS" -X DELETE "http://127.0.0.1:8088/ari/channels/${CHAN_ID}" 2>/dev/null || true
fi
sleep 3

echo; echo '########## BRAIN: session + turns + dispositions ##########'
docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) \
  | grep -iE 'brain ws (tenant resolved|session_start)|turn_decision|turn_latency|disposition|THIRD_PARTY|disclosure|policy_preempt|dnc|call_window|end_call|sot_call_closed' | head -60

echo; echo '########## CONNECTOR: client_id + source + session ##########'
journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n +$((CONN_MARK+1)) \
  | grep -iE 'client_id resolved|client_id_source|send session_start|audio rates|rate=' | head -15

echo; echo '########## GO-SERVER: session_ready + TTS WS + ASR + preempts ##########'
docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) \
  | grep -iE 'session_ready|apology_voice|sarvam tts ws (pre-opened|session opened|voice changed)|asr drained setup buffer|setup_buffer_drops|dropping oldest audio frame|asr ws close|asr reconnect|third_party|dnc|call_window|preempt' | head -40

echo; echo '########## CALL 1-redux VERDICT ##########'
BRAIN_TD=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'turn_decision' | tail -8)
BRAIN_DISP=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -iE 'disposition=' | tail -5)
BRAIN_TP=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -iE 'THIRD_PARTY_FLAGGED|third_party_flip' | tail -5)
BRAIN_END=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -iE 'end_call|sot_call_closed|outcome=' | tail -5)
GO_PREOPEN=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -i 'sarvam tts ws pre-opened' | head -1)
GO_BP=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -ic 'dropping oldest audio frame' || echo 0)
GO_INGRESS=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -i 'asr setup buffer full' | head -3)

echo "--- turn_decisions (last 8) ---"
echo "$BRAIN_TD"
echo "--- dispositions ---"
echo "$BRAIN_DISP"
echo "--- third_party ---"
echo "$BRAIN_TP"
echo "--- end_call / outcome ---"
echo "$BRAIN_END"
echo "--- tts ws pre-open ---"
echo "$GO_PREOPEN"
echo "--- egress backpressure drop count: $GO_BP ---"
echo "--- ingress asr setup-buffer-full drops ---"
echo "$GO_INGRESS"

echo PREDUE012_CALL1_REDUX_DONE
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
    with sftp.file("/tmp/_predue012_call1_redux.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_predue012_call1_redux.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_predue012_call1_redux.sh", timeout=180)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_predue012_call1_redux.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    if err.strip():
        print("STDERR", err[:1500])
    print(f"\nWrote {out}")


main()
