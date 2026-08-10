#!/usr/bin/env python3
"""Verify confirm_plo_payment_intent fragment loads + renders in deployed brain."""
from __future__ import annotations
import pathlib, sys, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r"""#!/bin/bash
set -uo pipefail
echo '========== W2-4 confirm fragment render check =========='
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
from app.engine.compose_renderer import render_compose
for fid in ["confirm_plo_payment_intent", "confirm_pay_date", "confirm_pay_today"]:
    try:
        text = render_compose("paisalo", [fid], {"committed_date":"15 \xe0\xa4\x85\xe0\xa4\x97\xe0\xa4\xb8\xe0\xa5\x8d\xe0\xa4\xa4","repay_amount":"5000"}, persona_voice="simran")
        print(f"{fid}: len={len(text)} text={text!r}")
    except Exception as e:
        print(f"{fid}: ERROR {e}")
PY
echo W24_CONFIRM_CHECK_DONE
"""


def main() -> int:
    cfg: dict[str, str] = {}
    sec = ROOT / ".deploy_secret"
    for line in sec.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["SSH_HOST"], port=int(cfg["SSH_PORT"]),
              username=cfg["SSH_USER"], password=cfg["SSH_PASSWORD"],
              timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.file("/tmp/_w24_confirm.sh", "w") as f:
        f.write(REMOTE.replace("\r\n", "\n"))
    sftp.chmod("/tmp/_w24_confirm.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_w24_confirm.sh", timeout=60)
    out = o.read().decode("utf-8", "replace")
    sys.stdout.write(out)
    sys.stderr.write(e.read().decode("utf-8", "replace"))
    c.close()
    return o.channel.recv_exit_status()


if __name__ == "__main__":
    raise SystemExit(main())
