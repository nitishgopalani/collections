"""Pull full brain + go-server logs for session 9aaf5dd2 (CALL 1-redux)."""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''
SID=9aaf5dd264d245848c9828d5dfba090f
echo "=== BRAIN full logs for session $SID ==="
docker logs fonada-voice-brain-1 2>&1 | grep -iE "$SID|third_party|disclosure|disposition|end_call|sot_call_closed|outcome|policy_preempt|preempt" | grep -i "$SID\|third_party\|disclosure\|disposition\|end_call\|sot_call_closed\|outcome\|preempt" | tail -80

echo; echo "=== GO-SERVER full logs for session $SID ==="
docker logs fonada-voice-go-server-1 2>&1 | grep -i "$SID" | grep -ivE "dropping oldest audio frame" | tail -60

echo; echo "=== GO-SERVER asr close/reconnect for session $SID ==="
docker logs fonada-voice-go-server-1 2>&1 | grep -i "$SID" | grep -iE "asr ws close|asr reconnect|asr ws dial|asr drained|setup_buffer|brain ws read ended|session ended|end of call|hangup" | tail -20

echo; echo "=== CONNECTOR logs for session $SID ==="
journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | grep -i "$SID" | tail -20

echo PREDUE012_CALL1_FULL_LOGS_DONE
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
    with sftp.file("/tmp/_predue012_call1_fulllogs.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_predue012_call1_fulllogs.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_predue012_call1_fulllogs.sh", timeout=60)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_predue012_call1_fulllogs.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    print(f"\nWrote {out}")


main()
