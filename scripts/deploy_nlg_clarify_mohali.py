#!/usr/bin/env python3
"""Deploy NLG clarify fix to Mohali via SFTP + brain rebuild (no git fetch)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    (ROOT / "app/engine/nlg.py", "/opt/fonada/Collection/app/engine/nlg.py"),
    (ROOT / "app/engine/turn.py", "/opt/fonada/Collection/app/engine/turn.py"),
    (
        ROOT / "tests/golden/test_slot_validation.py",
        "/opt/fonada/Collection/tests/golden/test_slot_validation.py",
    ),
]

REMOTE = r"""#!/bin/bash
set -eu
echo '========== DEPLOY SOT BRAIN FIXES (SFTP) =========='
date

ENV=/opt/fonada/Websocket/deploy/.env
if grep -q '^TRANSFER_AGENT_NUMBER=' "$ENV" 2>/dev/null; then
  sed -i 's/^TRANSFER_AGENT_NUMBER=.*/TRANSFER_AGENT_NUMBER=9910779326/' "$ENV"
else
  echo 'TRANSFER_AGENT_NUMBER=9910779326' >> "$ENV"
fi
grep '^TRANSFER_AGENT_NUMBER=' "$ENV"

cd /opt/fonada/Collection
grep -n 'CLARIFY_REASK_REPLY_IDS\|_coerce_sot_payment_refusal' app/engine/nlg.py app/engine/turn.py | head -5
echo "local_sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

cd /opt/fonada/Websocket/deploy
echo '--- build brain ---'
docker compose -f docker-compose.build.yml build brain 2>&1 | tail -5
echo '--- restart brain ---'
docker compose up -d brain 2>&1 | tail -3

for i in $(seq 1 30); do
  ST=$(docker inspect fonada-voice-brain-1 --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)
  [ "$ST" = healthy ] && break
  sleep 2
done
echo "brain_health=$ST after ${i}x2s"

docker exec fonada-voice-brain-1 curl -sf http://127.0.0.1:8000/healthz && echo healthz_ok
docker exec fonada-voice-brain-1 python -c "
from app.engine import nlg, turn
assert 'sot_payment_intent' in nlg.CLARIFY_REASK_REPLY_IDS
cmds, fired = turn._coerce_sot_payment_refusal([], 'sot_payment_intent', 'payment nahi ho payegi')
assert fired and cmds[0].value == 'refused'
print('sot_fixes=OK transfer_agent from env')
"
echo DEPLOY_NLG_CLARIFY_DONE
"""


def main() -> int:
    pw = os.environ.get("MOH_PW", "")
    if not pw:
        print("Set MOH_PW env var", file=sys.stderr)
        return 1

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        "103.132.145.55",
        9156,
        "root",
        pw,
        timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        sftp = client.open_sftp()
        for local, remote in FILES:
            print(f"upload {local.name} -> {remote}")
            sftp.put(str(local), remote)
        sftp.close()

        _, stdout, stderr = client.exec_command(
            f"bash -s <<'EOS'\n{REMOTE}\nEOS", timeout=600
        )
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        print(out)
        if err.strip():
            print(err, file=sys.stderr)
        return 0 if "DEPLOY_NLG_CLARIFY_DONE" in out else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
