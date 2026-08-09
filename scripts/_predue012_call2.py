"""PREDUE-012 FINAL CALL 2 (DNC): originate to 9810587857, hold 45s (bot should end itself).

Script for Nitish (1 line):
  - When bot speaks, say: "dobara call mat karna"
  -> expect dnc_requested, non-committal ack, bot ends.

PASS = dnc_requested disposition, policy_stop_calls_reply (no suppression promise), bot ends.
"""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''
ARI_USER=$(grep ^ARI_USER /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)
ARI_PASS=$(grep ^ARI_PASSWORD /etc/ari-orchestrator/ari-orchestrator.env 2>/dev/null | cut -d= -f2)

echo '=== PREDUE-012 FINAL CALL 2 (DNC): 9810587857 (paisalo, 45s, bot-ends) ==='
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
echo "NITISH SCRIPT (1 line DNC): say -> dobara call mat karna"
echo "  expect: dnc_requested, non-committal ack, bot ends itself."
echo "HOLDING 45s (or until bot ends)..."
for i in $(seq 1 9); do
  sleep 5
  ELAPSED=$(($(date +%s)-START))
  echo "t=${ELAPSED}s"
done
ELAPSED=$(($(date +%s)-START))
echo "elapsed=${ELAPSED}s"

echo; echo '--- force hangup if still up ---'
if [[ -n "$CHAN_ID" ]]; then
  curl -sS -m 10 -u "$ARI_USER:$ARI_PASS" -X DELETE "http://127.0.0.1:8088/ari/channels/${CHAN_ID}" 2>/dev/null || true
fi
sleep 3

echo; echo '########## BRAIN: session + turns + dnc + dispositions ##########'
docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) \
  | grep -iE 'brain ws (tenant resolved|session_start|session_end)|turn_decision|turn_latency|dnc|disposition|policy_stop|end_call|sot_call_closed|outcome|reply_empty' | head -40

echo; echo '########## CONNECTOR: client_id + source + end_of_call ##########'
journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n +$((CONN_MARK+1)) \
  | grep -iE 'client_id resolved|client_id_source|send session_start|end_of_call|audiosocket call (started|ended)' | head -15

echo; echo '########## GO-SERVER: session_ready + TTS WS + turn timing + dnc ##########'
docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) \
  | grep -iE 'session_ready|apology_voice|sarvam tts ws (pre-opened|session opened)|turn timing complete|disposition|end_call|dnc|policy_stop|end_of_call|session closed' | head -25

echo; echo '########## CALL 2 VERDICT ##########'
BRAIN_TD=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -i 'turn_decision' | tail -5)
BRAIN_DNC=$(docker logs fonada-voice-brain-1 2>&1 | tail -n +$((BRAIN_MARK+1)) | grep -iE 'dnc_requested|policy_stop|dnc_preempt' | tail -5)
GO_TT=$(docker logs fonada-voice-go-server-1 2>&1 | tail -n +$((GO_MARK+1)) | grep -i 'turn timing complete' | tail -3)
CONN_END=$(journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n +$((CONN_MARK+1)) | grep -iE 'end_of_call|audiosocket call ended' | tail -3)

echo "--- turn_decisions (last 5) ---"
echo "$BRAIN_TD"
echo "--- dnc (brain) ---"
echo "$BRAIN_DNC"
echo "--- go-server turn timing ---"
echo "$GO_TT"
echo "--- connector end ---"
echo "$CONN_END"

echo PREDUE012_CALL2_DONE
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
    with sftp.file("/tmp/_predue012_call2.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_predue012_call2.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_predue012_call2.sh", timeout=120)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_predue012_call2.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    if err.strip():
        print("STDERR", err[:1500])
    print(f"\nWrote {out}")


main()
