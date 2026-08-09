"""Pull CALL 2 session logs (latest session after 23:41 IST)."""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''
echo '=== Latest brain session_start (after CALL 1) ==='
docker logs fonada-voice-brain-1 2>&1 | grep -i 'brain ws session_start received' | tail -3

echo; echo '=== Latest connector audiosocket starts ==='
journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | grep -i 'audiosocket call started' | tail -3

echo; echo '=== Latest go-server session_ready ==='
docker logs fonada-voice-go-server-1 2>&1 | grep -i 'brain session_ready' | tail -3

echo; echo '=== Last 40 brain turn_decision/dnc/disposition lines ==='
docker logs fonada-voice-brain-1 2>&1 | tail -n 200 | grep -iE 'turn_decision|dnc|disposition|policy_stop|end_call|sot_call_closed|outcome|reply_empty|session_end|third_party' | tail -40

echo; echo '=== Last 20 go-server turn-timing/disposition lines ==='
docker logs fonada-voice-go-server-1 2>&1 | tail -n 200 | grep -iE 'turn timing complete|disposition|end_call|dnc|policy_stop|end_of_call|session closed|asr ws close|sarvam read ended' | tail -20

echo; echo '=== Last 15 connector end_of_call lines ==='
journalctl -u asterisk-connector --no-pager -o cat 2>/dev/null | tail -n 100 | grep -iE 'end_of_call|audiosocket call ended|audiosocket call started' | tail -15

echo PREDUE012_CALL2_CHECK_DONE
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
    with sftp.file("/tmp/_predue012_call2_check.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_predue012_call2_check.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_predue012_call2_check.sh", timeout=60)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_predue012_call2_check.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    print(f"\nWrote {out}")


main()
