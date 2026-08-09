"""G3 DEBT-039 preempt sim only (no dial): verify the preempt close is spoken
on the deployed brain via a simulated DNC cue + recording on_gated_reply.
"""
from __future__ import annotations
import pathlib, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r'''#!/bin/bash
set -uo pipefail
echo '=== G3 DEBT-039 PREEMPT SIM (DNC cue -> spoken close) on deployed brain ==='
date '+%Y-%m-%d %H:%M:%S %Z'
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
import asyncio
from app.engine.turn import handle_turn
from app.engine.tracker import new_conversation_state
from app.clients.tools_sim import FakeToolClient
from app.memory.store import InMemoryMemoryStore

class _EmptyKB:
    is_stub = False
    async def ping(self): return True
    async def retrieve(self, text, tenant_id, k=6): return []

class _NoLLM:
    is_stub = False
    call_count = 0
    async def ping(self): return True
    async def complete(self, system, user, *, json_only=True, **kw):
        _NoLLM.call_count += 1
        return "[]"

class _Recorder:
    def __init__(self): self.calls = []
    async def __call__(self, reply_text, *, voice_id=None, tts_model=None, tts_pace=None):
        self.calls.append({"text": reply_text, "voice_id": voice_id})

async def run():
    memory = InMemoryMemoryStore()
    state = new_conversation_state("sim-debt039-dnc", "paisalo", "sim_borrower")
    state.slots["customer_name"] = "Ramesh"
    state.slots["identity_ok"] = True
    memory._states["sim-debt039-dnc"] = state.model_copy(deep=True)
    rec = _Recorder()
    from app.schemas.api import TurnRequest
    req = TurnRequest(call_id="sim-debt039-dnc", borrower_id="sim_borrower",
                      tenant_id="paisalo", channel="voice", locale="hi-IN",
                      transcript="dobara call mat karna",
                      turn_meta={"force_flow": "plo_opener", "call_date": "2026-08-09"})
    resp = await handle_turn(req, memory=memory, llm=_NoLLM(),
                             tools=FakeToolClient(), kb=_EmptyKB(),
                             on_gated_reply=rec)
    print("debt039_sim_disposition:", resp.disposition)
    print("debt039_sim_end_call:", resp.end_call)
    print("debt039_sim_reply_text_len:", len(resp.reply_text or ""))
    print("debt039_sim_on_gated_reply_calls:", len(rec.calls))
    if rec.calls:
        print("debt039_sim_close_voice_id:", rec.calls[0]["voice_id"])
        print("debt039_sim_close_has_darj:", "darj" in resp.reply_text.lower() or "\u0926\u0930\u094d\u091c" in resp.reply_text)
        print("debt039_sim_close_no_ruppee:", "\u20b9" not in resp.reply_text)
        print("debt039_sim_close_no_kisht:", "\u0915\u093f\u0936\u094d\u0924" not in resp.reply_text)
        print("debt039_sim_close_text:", resp.reply_text[:120])

asyncio.run(run())
PY
echo G3_PREEMPT_SIM_DONE
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
    with sftp.file("/tmp/_g3_preempt_sim.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_g3_preempt_sim.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_g3_preempt_sim.sh", timeout=60)
    raw = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    c.close()
    redacted = raw.replace(cfg["SSH_PASSWORD"], "[REDACTED]")
    out = pathlib.Path(__file__).resolve().parent / "_g3_preempt_sim.txt"
    out.write_text(redacted + (("\n[stderr]\n" + err) if err.strip() else ""), encoding="utf-8")
    print(redacted)
    if err.strip():
        print("STDERR", err[:1500])
    print(f"\nWrote {out}")


main()
