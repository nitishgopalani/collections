#!/usr/bin/env python3
"""W2-4 brain turn_decision dump for session dfae962c."""
from __future__ import annotations
import pathlib, sys, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]
SID = "dfae962c1ed141eb801a097a8b6f1adf"

REMOTE = r"""#!/bin/bash
set -uo pipefail
echo '========== W2-4 brain turn_decision dump =========='
date
echo "session_id=__SID__"
echo '--- all turn_decision lines for this session ---'
docker logs fonada-voice-brain-1 --since 20m 2>&1 | grep 'turn_decision' | grep '__SID__'
echo '--- all turn_latency lines for this session ---'
docker logs fonada-voice-brain-1 --since 20m 2>&1 | grep 'turn_latency' | grep '__SID__'
echo '--- reply_empty / end_call events ---'
docker logs fonada-voice-brain-1 --since 20m 2>&1 | grep -iE 'reply_empty|end_call|disposition|escalat' | grep '__SID__' | tail -10
echo W24_BRAIN_TURNS_DONE
"""


def main() -> int:
    cfg: dict[str, str] = {}
    sec = ROOT / ".deploy_secret"
    for line in sec.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    script = REMOTE.replace("__SID__", SID).replace("\r\n", "\n")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["SSH_HOST"], port=int(cfg["SSH_PORT"]),
              username=cfg["SSH_USER"], password=cfg["SSH_PASSWORD"],
              timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.file("/tmp/_w24_turns.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_w24_turns.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_w24_turns.sh", timeout=90)
    out = o.read().decode("utf-8", "replace")
    sys.stdout.write(out)
    sys.stderr.write(e.read().decode("utf-8", "replace"))
    (ROOT / "scripts" / "_w24_turns_raw.txt").write_text(out, encoding="utf-8")
    rc = o.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
