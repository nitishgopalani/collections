#!/usr/bin/env python3
"""Outbound SOT live call to 9810587857 via Mohali Stasis + go-server + brain."""
from __future__ import annotations

import os
import sys

import paramiko

NUMBER = "9810587857"
CLI = "1725617001"
TENANT = "salary-on-time"
AUDIOSOCKET = "127.0.0.1:9092"
MEDIA_SECRET = "ms_loadtest_mohali_a1b2c3d4e5f6789012345678abcdef01"
MEDIA_URL = "wss://voice-api.fonada.ai:18444/stream"
HOLD_SEC = 120

REMOTE = r"""#!/bin/bash
set -uo pipefail
NUMBER='__NUMBER__'
CLI='__CLI__'
TENANT='__TENANT__'
AS='__AS__'
MEDIA_URL='__MEDIA_URL__'
MEDIA_SECRET='__MEDIA_SECRET__'
HOLD=__HOLD_SEC__
LOG=/var/log/asterisk/full
ENV=/etc/asterisk-connector/asterisk-connector.env
NGINX=/etc/nginx/conf.d/go-stream-wss-18444.conf
PUBLIC=103.132.145.55

echo '========== SOT OUTBOUND LIVE =========='
date

echo '--- ensure WSS proxy :18444/stream -> go-server :8080 ---'
if [[ ! -f "$NGINX" ]]; then
  cat > "$NGINX" <<'NGX'
server {
    listen 18444 ssl http2;
    listen [::]:18444 ssl http2;
    server_name voice-api.fonada.ai;

    ssl_certificate     /etc/ssl/certs/Newfonada/fullchain.pem;
    ssl_certificate_key /etc/ssl/certs/Newfonada/fonada_ai.key;
    ssl_session_cache   shared:SSL:10m;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location /stream {
        proxy_pass http://127.0.0.1:8080/stream;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
NGX
  nginx -t && systemctl reload nginx
fi
ss -tlnp | grep ':18444' || echo 'WARN no listener 18444'

iptables -t nat -C OUTPUT -d ${PUBLIC}/32 -p tcp --dport 18444 -j DNAT --to-destination 127.0.0.1:18444 2>/dev/null \
  || iptables -t nat -A OUTPUT -d ${PUBLIC}/32 -p tcp --dport 18444 -j DNAT --to-destination 127.0.0.1:18444

echo '--- stack health ---'
systemctl is-active asterisk asterisk-connector ari-orchestrator nginx | paste -sd,
docker inspect fonada-voice-brain-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo no_brain
docker inspect fonada-voice-go-server-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo no_go
curl -sf http://127.0.0.1:8080/healthz || echo go_health_fail

echo '--- connector WS_READY=45 ---'
grep -q '^WS_READY_TIMEOUT_SECONDS=' "$ENV" && \
  sed -i 's/^WS_READY_TIMEOUT_SECONDS=.*/WS_READY_TIMEOUT_SECONDS=45/' "$ENV" || \
  echo 'WS_READY_TIMEOUT_SECONDS=45' >> "$ENV"
systemctl restart asterisk-connector
sleep 3

echo '--- register SOT media (go-server via WSS) ---'
ADMIN=$(cat /etc/ari-orchestrator/orch_admin_api_key)
python3 - <<PY
import json
open('/tmp/sot_media_reg.json','w').write(json.dumps({
  'tenant':'__TENANT__',
  'media_ws_url':'__MEDIA_URL__',
  'media_secret':'__MEDIA_SECRET__',
  'sku':'g711'})+'\n')
PY
curl -sS -m 15 -X PUT http://172.18.0.1:8095/admin/v1/media-stream \
  -H "Authorization: Admin $ADMIN" -H 'Content-Type: application/json' \
  -d @/tmp/sot_media_reg.json
echo
curl -sS -m 8 "http://172.18.0.1:8095/admin/v1/media-stream?tenant=__TENANT__" \
  -H "Authorization: Admin $ADMIN" | python3 -c "import sys,json; d=json.load(sys.stdin); print('registered', d.get('media_ws_url',''))"

echo '--- ng_trunk ---'
asterisk -rx 'pjsip show endpoint ng_trunk' 2>/dev/null | grep -E 'Contact|Avail|Unavail' | head -3

echo '========== DIALING '"$NUMBER"' =========='
MARK=$(wc -l < "$LOG")
BRAIN_MARK=$(docker logs fonada-voice-brain-1 2>&1 | wc -l)
GO_MARK=$(docker logs fonada-voice-go-server-1 2>&1 | wc -l)
T_DIAL=$(date +%s)

PWARI=$(grep -m1 '^ARI_PASSWORD=' /etc/ari-orchestrator/ari-orchestrator.env | cut -d= -f2-)
ARIUSER=$(grep -m1 '^ARI_USERNAME=' /etc/ari-orchestrator/ari-orchestrator.env | cut -d= -f2-)
ARIUSER=${ARIUSER:-orchestrator}
ENC_EP=$(python3 -c "import urllib.parse; print(urllib.parse.quote('PJSIP/${NUMBER}@ng_trunk', safe=''))")
ENC_ARGS=$(python3 -c "import urllib.parse; print(urllib.parse.quote('inbound,${TENANT},${AS}', safe=''))")
ENC_CLI=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${CLI}', safe=''))")
# Stamp CUSTOMER_PHONE on the channel so media-meta → connector session_start
# carries the real ANI (not empty constants / stream-SID fallback).
RESP=$(curl -sS -m 25 -u "${ARIUSER}:${PWARI}" -X POST \
  "http://127.0.0.1:8088/ari/channels?endpoint=${ENC_EP}&app=fonada-orchestrator&appArgs=${ENC_ARGS}&callerId=${ENC_CLI}" \
  -H 'Content-Type: application/json' \
  -d "{\"variables\":{\"CUSTOMER_PHONE\":\"${NUMBER}\",\"BUSINESS_PHONE\":\"${CLI}\"}}")
echo "ari_originate=$RESP"
CH=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)
echo "channel_id=$CH"

ANS=0
T_ANS=0
for i in $(seq 1 60); do
  [[ -z "$CH" ]] && break
  ST=$(curl -sS -m 3 -u "${ARIUSER}:${PWARI}" "http://127.0.0.1:8088/ari/channels/$CH" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',''))" 2>/dev/null || true)
  echo "poll_${i} state=$ST"
  if [[ "$ST" == "Up" && "$ANS" == "0" ]]; then ANS=1; T_ANS=$(date +%s); echo ANSWERED; fi
  if [[ "$ANS" == "1" && $(( $(date +%s) - T_ANS )) -ge $HOLD ]]; then echo HOLD_DONE; break; fi
  sleep 2
done

echo '--- connector (since dial) ---'
journalctl -u asterisk-connector --since "@$T_DIAL" --no-pager -o cat 2>/dev/null \
  | grep -iE 'audiosocket|BYO|session_start|websocket|error|fail|ws ready' | tail -15

echo '--- orchestrator ---'
journalctl -u ari-orchestrator --since "@$T_DIAL" --no-pager -o cat 2>/dev/null \
  | grep -iE 'inbound|assembling|assembled|error|fail' | tail -10

echo '--- go-server (new since dial) ---'
docker logs fonada-voice-go-server-1 2>&1 | tail -n +"$((GO_MARK+1))" \
  | grep -iE 'session_start|brain|asr|tts|error|fail|stream_sid' | grep -viE 'kind=audio' | tail -20

echo '--- brain / SOT flow ---'
docker logs fonada-voice-brain-1 2>&1 | tail -n +"$((BRAIN_MARK+1))" \
  | grep -iE 'session_start|tenant_id|sot_|active_flow|turn_decision|reply_id|error' | tail -25

[[ -n "$CH" ]] && curl -sS -m 10 -u "${ARIUSER}:${PWARI}" -X DELETE \
  "http://127.0.0.1:8088/ari/channels/$CH" >/dev/null && echo hangup_sent

echo '--- SIP trace ---'
tail -n +"$((MARK+1))" "$LOG" | grep -E "$NUMBER|$CLI|ng_trunk|Stasis|AudioSocket|180|200|403|486|503" | head -25

echo "RESULT answered=$ANS duration=$(( $(date +%s) - T_DIAL ))s"
echo SOT_OUTBOUND_DONE
"""


def main() -> int:
    pw = os.environ.get("MOH_PW", "")
    if not pw:
        from pathlib import Path

        secret = Path(__file__).resolve().parents[2] / ".deploy_secret"
        if secret.exists():
            for line in secret.read_text(encoding="utf-8").splitlines():
                if line.startswith("SSH_PASSWORD="):
                    pw = line.split("=", 1)[1].strip()
                    break
    if not pw:
        print("Set MOH_PW env var or populate .deploy_secret", file=sys.stderr)
        return 1

    script = (
        REMOTE.replace("__NUMBER__", NUMBER)
        .replace("__CLI__", CLI)
        .replace("__TENANT__", TENANT)
        .replace("__AS__", AUDIOSOCKET)
        .replace("__MEDIA_URL__", MEDIA_URL)
        .replace("__MEDIA_SECRET__", MEDIA_SECRET)
        .replace("__HOLD_SEC__", str(HOLD_SEC))
    )

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        "103.132.145.55",
        9156,
        "root",
        pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    sftp = c.open_sftp()
    with sftp.file("/tmp/sot_outbound_live.sh", "w") as f:
        f.write(script.replace("\r\n", "\n"))
    sftp.chmod("/tmp/sot_outbound_live.sh", 0o755)
    sftp.close()

    print(f"Placing SOT outbound call to {NUMBER} — answer your phone and talk in Hindi.")
    print(f"Hold window: {HOLD_SEC}s after pickup.\n")

    _, stdout, stderr = c.exec_command("bash /tmp/sot_outbound_live.sh", timeout=HOLD_SEC + 180)
    raw = stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")
    c.close()

    redacted = raw.replace(MEDIA_SECRET, "[REDACTED]").replace(pw, "[REDACTED]")
    sys.stdout.write(redacted)

    if "ANSWERED" in raw and "session_start" in raw.lower():
        return 0
    if "ANSWERED" in raw:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
