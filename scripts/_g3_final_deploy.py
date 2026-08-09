"""G3 final W1 deploy: brain d8e9cf9 (DEBT-039) + go-server 7f79957 (DEBT-040) to UAT (Nitish-Moh).

Clean build only. NO sed-on-.env. NO docker-cp. NO git clean. Verifies repo
HEAD, image rebuild, container health, /version git_sha, postgres 5432
port-patch intact, and DEBT-039 (brain) + DEBT-040 (go-server) code present
in the deployed images.
"""
from __future__ import annotations
import pathlib, sys, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]
BRAIN_SHA = "d8e9cf9a5e626196412bdafe05c9410e33d6f1d5"
BRAIN_BRANCH = "feature/tier23-engine-upgrade"
GO_SHA = "7f79957e283451630269287d99235adf56753150"
GO_BRANCH = "release/uat-voice-stack"
GO_IMG = "fonada/voice-go-server:latest"

REMOTE = r"""#!/bin/bash
set -uo pipefail
BRAIN_SHA='__BRAIN_SHA__'
BRAIN_BRANCH='__BRAIN_BRANCH__'
GO_SHA='__GO_SHA__'
GO_BRANCH='__GO_BRANCH__'
GO_IMG='__GO_IMG__'
COMPOSE=/opt/fonada/Websocket/deploy/docker-compose.yml

echo '========== G3 FINAL W1 DEPLOY (brain DEBT-039 + go-server DEBT-040) =========='
date

echo '--- pre: in-flight calls (must be 0) ---'
asterisk -rx "core show channels" 2>&1 | grep -E "active (channels|calls)" || echo "asterisk query failed"

echo; echo '########## BRAIN (d8e9cf9 — DEBT-039) ##########'
cd /opt/fonada/Collection
echo '--- fetch ---'
git fetch origin "$BRAIN_BRANCH" 2>&1 | tail -2
echo '--- checkout + reset to target SHA ---'
git checkout -B "$BRAIN_BRANCH" -f "origin/$BRAIN_BRANCH" 2>&1 | tail -2
git reset --hard "$BRAIN_SHA" 2>&1 | tail -1
CUR=$(git rev-parse HEAD)
echo "brain_checked_out=$CUR"
case "$CUR" in
  "$BRAIN_SHA"*) echo BRAIN_SHA_OK;;
  *) echo "BRAIN_SHA_MISMATCH want=$BRAIN_SHA got=$CUR"; exit 1;;
esac

echo '--- rebuild brain IMAGE (fonada/voice-brain:latest) ---'
docker build -t fonada/voice-brain:latest \
  -f /opt/fonada/Websocket/deploy/docker/Dockerfile.brain \
  /opt/fonada/Collection 2>&1 | tail -15
BRAIN_IMG=$(docker inspect fonada/voice-brain:latest --format '{{.Id}}' 2>/dev/null)
echo "brain_image_id=$BRAIN_IMG"

echo '--- recreate brain container ---'
cd /opt/fonada/Websocket/deploy
docker compose -f "$COMPOSE" up -d --force-recreate --no-deps brain 2>&1 | tail -5
BST=missing
for i in $(seq 1 50); do
  BST=$(docker inspect fonada-voice-brain-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)
  [ "$BST" = healthy ] && break
  sleep 3
done
echo "brain_health=$BST"
test "$BST" = healthy
BRAIN_RUN_IMG=$(docker inspect fonada-voice-brain-1 --format '{{.Image}}' 2>/dev/null)
echo "brain_container_image=$BRAIN_RUN_IMG"
[ "$BRAIN_RUN_IMG" = "$BRAIN_IMG" ] && echo BRAIN_IMAGE_MATCH_OK || echo BRAIN_IMAGE_MATCH_FAIL

echo; echo '########## GO-SERVER (7f79957 — DEBT-040) ##########'
cd /opt/fonada/Websocket
echo '--- fetch ---'
git fetch origin "$GO_BRANCH" 2>&1 | tail -2
git checkout "$GO_BRANCH" 2>&1 | tail -2
git reset --hard "$GO_SHA" 2>&1 | tail -1
GCUR=$(git rev-parse HEAD)
echo "go_checked_out=$GCUR"
case "$GCUR" in
  "$GO_SHA"*) echo GO_SHA_OK;;
  *) echo "GO_SHA_MISMATCH want=$GO_SHA got=$GCUR"; exit 1;;
esac

echo '--- rebuild go-server image ---'
docker build \
  -f deploy/docker/Dockerfile.go-server \
  --build-arg GIT_SHA="$GO_SHA" \
  --build-arg GIT_BRANCH="$GO_BRANCH" \
  -t "$GO_IMG" . 2>&1 | tail -12
GO_IMG_ID=$(docker inspect "$GO_IMG" --format '{{.Id}}' 2>/dev/null)
echo "go_image_id=$GO_IMG_ID"

echo '--- recreate go-server container ---'
docker compose -f "$COMPOSE" up -d --force-recreate --no-deps go-server 2>&1 | tail -5
sleep 5
GST=missing
for i in $(seq 1 30); do
  GST=$(docker inspect fonada-voice-go-server-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)
  [ "$GST" = healthy ] && break
  sleep 3
done
echo "go_health=$GST"
GO_RUN_IMG=$(docker inspect fonada-voice-go-server-1 --format '{{.Image}}' 2>/dev/null)
echo "go_container_image=$GO_RUN_IMG"
[ "$GO_RUN_IMG" = "$GO_IMG_ID" ] && echo GO_IMAGE_MATCH_OK || echo GO_IMAGE_MATCH_FAIL

echo '--- post: go-server /version (expect git_sha='"$GO_SHA"') ---'
curl -sS -m 5 http://127.0.0.1:8080/version 2>&1; echo

echo; echo '########## POSTGRES PORT PATCH INTACT CHECK (bit us twice) ##########'
echo '--- postgres service ports block ---'
python3 - <<'PY'
import re
p = '/opt/fonada/Websocket/deploy/docker-compose.yml'
s = open(p, encoding='utf-8', errors='replace').read()
m = re.search(r'(?ms)^  postgres:\s*\n(    .*?\n)(?=^  \S|\Z)', s)
print(m.group(0) if m else 'no postgres service')
PY
echo '--- postgres container port binding ---'
docker inspect fonada-voice-postgres-1 --format '{{json .HostConfig.PortBindings}}' 2>&1; echo
echo '--- verify 172.18.0.1:5432 OPEN (orchestrator needs it) ---'
timeout 3 bash -c 'echo > /dev/tcp/172.18.0.1/5432' 2>&1 && echo 'PG_PORT_172.18.0.1:5432 OPEN' || echo 'PG_PORT_172.18.0.1:5432 REFUSED (MUST FIX)'

echo; echo '########## DEBT-040 CODE PRESENT IN DEPLOYED GO-SERVER ##########'
echo '--- item A: drain-ready gate (EnableDrainReadyGate / ConfirmDrainReady) ---'
docker exec -i fonada-voice-go-server-1 sh -c 'grep -rl "EnableDrainReadyGate" /app 2>/dev/null | head -3' || echo "EnableDrainReadyGate grep failed"
echo '--- item B: drain-ready callback on Session (SetDrainReadyCallback) ---'
docker exec -i fonada-voice-go-server-1 sh -c 'grep -rl "SetDrainReadyCallback" /app 2>/dev/null | head -3' || echo "SetDrainReadyCallback grep failed"
echo '--- item C: defaultAudioBufferSize=64 (ingress enlarge) ---'
docker exec -i fonada-voice-go-server-1 sh -c 'grep -rl "defaultAudioBufferSize" /app 2>/dev/null | head -3' || echo "defaultAudioBufferSize grep failed"

echo; echo '########## DEBT-039 CODE PRESENT IN DEPLOYED BRAIN ##########'
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
# DEBT-039: preempt close replies spoken before end_call
import inspect
from app.engine import turn as turn_mod
from app.engine import safety as safety_mod
src_turn = inspect.getsource(turn_mod)
src_safety = inspect.getsource(safety_mod)
print("debt039_emit_preempt_close_helper:", "_emit_preempt_close" in src_turn)
print("debt039_resolve_turn_voice_helper:", "_resolve_turn_voice" in src_turn)
print("debt039_interpolate_close_reply:", "_interpolate_close_reply" in src_turn)
print("debt039_on_gated_reply_in_safety_early_exit:", "on_gated_reply" in inspect.getsource(turn_mod._run_safety_early_exit))
print("debt039_on_gated_reply_in_dnc_early_exit:", "on_gated_reply" in inspect.getsource(turn_mod._run_dnc_early_exit))
print("debt039_on_gated_reply_in_cw_early_exit:", "on_gated_reply" in inspect.getsource(turn_mod._run_call_window_early_exit))
print("debt039_on_gated_reply_in_flip_early_exit:", "on_gated_reply" in inspect.getsource(turn_mod._run_third_party_flip_early_exit))
# Profile fields
from app.engine.tenant_profile import get_tenant_profile
p = get_tenant_profile("paisalo")
print("debt039_profile_third_party_close:", bool(getattr(p, "third_party_close", "")))
print("debt039_profile_dnc_ack:", bool(getattr(p, "dnc_ack", "")))
print("debt039_profile_window_close:", bool(getattr(p, "window_close", "")))
print("debt039_profile_vulnerability_close:", bool(getattr(p, "vulnerability_close", "")))
print("debt039_third_party_close_has_customer_name:", "{customer_name}" in (getattr(p, "third_party_close", "") or ""))
print("debt039_third_party_close_no_ruppee:", "\u20b9" not in (getattr(p, "third_party_close", "") or ""))
print("debt039_third_party_close_no_kisht:", "\u0915\u093f\u0936\u094d\u0924" not in (getattr(p, "third_party_close", "") or ""))
PY

echo; echo '########## STACK HEALTH ##########'
systemctl is-active asterisk asterisk-connector ari-orchestrator nginx 2>&1 | paste -sd,
docker inspect fonada-voice-brain-1 --format 'brain={{.State.Health.Status}} image={{.Image}}'
docker inspect fonada-voice-go-server-1 --format 'go={{.State.Health.Status}} image={{.Image}}'
docker inspect fonada-voice-postgres-1 --format 'pg={{.State.Health.Status}}'

echo G3_FINAL_W1_DEPLOY_DONE
"""


def main() -> int:
    cfg: dict[str, str] = {}
    sec = ROOT / ".deploy_secret"
    for line in sec.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    script = (
        REMOTE.replace("__BRAIN_SHA__", BRAIN_SHA)
        .replace("__BRAIN_BRANCH__", BRAIN_BRANCH)
        .replace("__GO_SHA__", GO_SHA)
        .replace("__GO_BRANCH__", GO_BRANCH)
        .replace("__GO_IMG__", GO_IMG)
        .replace("\r\n", "\n")
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["SSH_HOST"], port=int(cfg["SSH_PORT"]),
               username=cfg["SSH_USER"], password=cfg["SSH_PASSWORD"],
               timeout=30, banner_timeout=30, auth_timeout=30,
               allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.file("/tmp/_g3_final_deploy.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_g3_final_deploy.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_g3_final_deploy.sh", timeout=1800)
    raw = o.read().decode(errors="replace") + e.read().decode(errors="replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_g3_final_deploy.txt"
    out.write_text(redacted, encoding="utf-8")
    sys.stdout.write(redacted)
    print(f"\nWrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
