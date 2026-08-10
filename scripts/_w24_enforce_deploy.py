#!/usr/bin/env python3
"""W2-4 enforce-deploy: brain 938f5a7 + COMMITMENT_GATE_ENFORCE=true + verify.

Brain-only (no go-server / connector changes this phase). Flips the gate
from SHADOW to ENFORCE by setting COMMITMENT_GATE_ENFORCE=true in the UAT
.env (env_file'd by the brain service). Verifies:
  - brain SHA == 938f5a7
  - brain container healthy + image-match
  - COMMITMENT_GATE_ENFORCE=true visible inside the container
  - commitment_gate_enforce_enabled() returns True
  - gate verdict fields + gate_blocked_writes + repair_reason in guards source
  - source tagging: Command.source field + _slot_sources in tracker
  - silent smoke: synthetic turn logs gate_enforce=true + gate_verdict
"""
from __future__ import annotations
import pathlib, sys, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]
BRAIN_SHA = "938f5a7"
BRAIN_BRANCH = "feature/tier23-engine-upgrade"
BRAIN_IMG = "fonada/voice-brain:latest"

REMOTE = r"""#!/bin/bash
set -uo pipefail
BRAIN_SHA='__BRAIN_SHA__'
BRAIN_BRANCH='__BRAIN_BRANCH__'
BRAIN_IMG='__BRAIN_IMG__'

echo '========== W2-4 enforce-deploy: brain (clean build) + COMMITMENT_GATE_ENFORCE=true =========='
date

echo '========== BRAIN deploy =========='
cd /opt/fonada/Collection
git fetch origin "$BRAIN_BRANCH" 2>&1 | tail -2
git reset --hard HEAD 2>&1 | tail -1
git clean -fd 2>&1 | tail -2
git checkout -B "$BRAIN_BRANCH" -f "origin/$BRAIN_BRANCH" 2>&1 | tail -1
CUR=$(git rev-parse HEAD)
echo "brain_checked_out=$CUR"
case "$CUR" in "$BRAIN_SHA"*) echo BRAIN_SHA_OK ;; *) echo "BRAIN_SHA_MISMATCH want=$BRAIN_SHA got=$CUR"; exit 1 ;; esac

echo '--- flip COMMITMENT_GATE_ENFORCE=true in .env (idempotent) ---'
ENV_FILE=/opt/fonada/Websocket/deploy/.env
grep -q '^COMMITMENT_GATE_ENFORCE=' "$ENV_FILE" 2>/dev/null && sed -i 's/^COMMITMENT_GATE_ENFORCE=.*/COMMITMENT_GATE_ENFORCE=true/' "$ENV_FILE" || echo 'COMMITMENT_GATE_ENFORCE=true' >> "$ENV_FILE"
grep '^COMMITMENT_GATE_ENFORCE=' "$ENV_FILE"

echo '--- rebuild brain IMAGE ---'
docker build -t "$BRAIN_IMG" -f /opt/fonada/Websocket/deploy/docker/Dockerfile.brain /opt/fonada/Collection 2>&1 | tail -12
BRAIN_IMG_ID=$(docker inspect "$BRAIN_IMG" --format '{{.Id}}' 2>/dev/null)
echo "brain_image_id=$BRAIN_IMG_ID"

echo '--- recreate brain (no-deps) ---'
cd /opt/fonada/Websocket/deploy
docker compose up -d --force-recreate --no-deps brain 2>&1 | tail -5
ST=missing
for i in $(seq 1 50); do
  ST=$(docker inspect fonada-voice-brain-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)
  [ "$ST" = healthy ] && break
  sleep 3
done
echo "brain_health=$ST"
test "$ST" = healthy
RUN_BRAIN_IMG=$(docker inspect fonada-voice-brain-1 --format '{{.Image}}' 2>/dev/null)
echo "brain_container_image=$RUN_BRAIN_IMG"
[ "$RUN_BRAIN_IMG" = "$BRAIN_IMG_ID" ] && echo BRAIN_IMAGE_MATCH_OK || echo BRAIN_IMAGE_MATCH_FAIL

echo '--- verify W2-4 sentinels in deployed brain ---'
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
import inspect, os
from app.engine import commitment_gate, turn, robustness, tracker
from app.schemas import command as cmd_mod
gsrc = inspect.getsource(commitment_gate)
tsrc = inspect.getsource(turn)
rsrc = inspect.getsource(robustness)
trksrc = inspect.getsource(tracker)
cmdsrc = inspect.getsource(cmd_mod)
print("gate_module_importable: True")
print("gate_has_source_aware_cost_class:", "cmd.source in" in gsrc and '"system"' in gsrc and '"confirmed"' in gsrc)
print("gate_call_site_in_turn:", "commitment_gate(" in tsrc and "_gate_verdict" in tsrc)
print("turn_has_enforce_flip:", "_gate_enforce" in tsrc and "downgrade" in tsrc and "set_pending_confirm" in tsrc)
print("turn_has_gate_blocked_writes_guard:", "gate_blocked_writes" in tsrc and "repair_reason" in tsrc)
print("robustness_has_track_slot_reask_gated:", "def track_slot_reask_gated" in rsrc)
print("robustness_has_set_pending_confirm:", "def set_pending_confirm" in rsrc)
print("robustness_has_pending_confirm_key:", "PENDING_CONFIRM_KEY" in rsrc)
print("tracker_has_slot_sources:", "_slot_sources" in trksrc)
print("tracker_hydrate_tags_source_system:", 'sources.setdefault(key, "system")' in trksrc)
print("command_has_source_field:", "source:" in cmdsrc and "system" in cmdsrc)
env_enforce = os.environ.get("COMMITMENT_GATE_ENFORCE", "")
print("env_COMMITMENT_GATE_ENFORCE:", env_enforce)
from app.engine.commitment_gate import commitment_gate_enforce_enabled
print("enforce_enabled_returns:", commitment_gate_enforce_enabled())
PY

echo '--- silent smoke: synthetic turn -> grep gate_enforce + gate_verdict ---'
docker exec -i fonada-voice-brain-1 python3 - <<'PY' 2>&1 | tail -25
import os, json, logging, asyncio
os.environ.setdefault("STUB_MODE", "true")
os.environ.setdefault("LLM_STUB", "true")
os.environ.setdefault("KB_STUB", "true")
os.environ.setdefault("COMMITMENT_GATE_ENFORCE", "true")
logging.basicConfig(level=logging.INFO, format="%(message)s")
from app.engine.turn import run_turn
from app.engine.tracker import new_conversation_state
from app.schemas.turn import TurnRequest

async def main():
    state = new_conversation_state("call-w24-smoke", "paisalo", "borrower-w24-smoke")
    req = TurnRequest(call_id="call-w24-smoke", session_id="sess-w24-smoke",
                      transcript="theek hai kar dunga", tenant_id="paisalo",
                      borrower_id="borrower-w24-smoke")
    resp = await run_turn(req, state)
    print("smoke_reply_text:", (resp.reply_text or "")[:80])
    print("smoke_end_call:", bool(resp.end_call))

asyncio.run(main())
PY

echo '--- post: go-server /version (unchanged) ---'
curl -sS -m 5 http://127.0.0.1:8080/version 2>&1; echo
echo '--- container health ---'
docker inspect fonada-voice-brain-1 --format 'brain image={{.Image}} status={{.State.Health.Status}}'
echo '--- stack health ---'
systemctl is-active asterisk asterisk-connector ari-orchestrator nginx | paste -sd,

echo W24_ENFORCE_DEPLOY_DONE
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
        .replace("__BRAIN_IMG__", BRAIN_IMG)
        .replace("\r\n", "\n")
    )
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["SSH_HOST"], port=int(cfg["SSH_PORT"]),
              username=cfg["SSH_USER"], password=cfg["SSH_PASSWORD"],
              timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.file("/tmp/_w24_enforce_deploy.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_w24_enforce_deploy.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_w24_enforce_deploy.sh", timeout=900)
    sys.stdout.write(o.read().decode("utf-8", "replace"))
    sys.stderr.write(e.read().decode("utf-8", "replace"))
    rc = o.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
