#!/usr/bin/env python3
"""W2-4 live-retest silent smoke (bd98fb0 already deployed — no rebuild).

Three criteria from the retest brief:
  1. COMMITMENT_GATE_ENFORCE=true + enforce_enabled()
  2. pending_confirm persists across turns (set on T, readable on T+1)
  3. "haan pakka" fixture at plo_payment_intent + pending_confirm → evidence 3
"""
from __future__ import annotations
import pathlib, sys, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r"""#!/bin/bash
set -uo pipefail
echo '========== W2-4 retest silent smoke (no deploy) =========='
date
echo '--- brain health + image ---'
docker inspect fonada-voice-brain-1 --format 'health={{.State.Health.Status}} image={{.Image}}' 2>/dev/null
echo '--- stack ---'
systemctl is-active asterisk asterisk-connector ari-orchestrator 2>/dev/null || true
docker inspect fonada-voice-brain-1 --format '{{.State.Health.Status}}' 2>/dev/null
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
import os, inspect
print("env_COMMITMENT_GATE_ENFORCE:", os.environ.get("COMMITMENT_GATE_ENFORCE"))
from app.engine.commitment_gate import commitment_gate, commitment_gate_enforce_enabled
from app.engine.evidence_scorer import score_evidence
from app.engine.robustness import (
    PENDING_CONFIRM_KEY, set_pending_confirm, track_slot_reask_gated,
)
from app.engine.tracker import new_conversation_state
from app.engine.tenant_profile import get_tenant_profile
from app.engine import turn as turn_mod
from app.schemas.command import Command

ok = True

# 1. enforce flag
enf = commitment_gate_enforce_enabled()
print("enforce_enabled:", enf)
if not enf or os.environ.get("COMMITMENT_GATE_ENFORCE") != "true":
    print("FAIL enforce flag")
    ok = False

# 2. pending_confirm persists across turns
#    set_pending_confirm writes _pending_confirm; track_slot_reask_gated
#    must NOT pop it (prior_pending_confirm is captured by the caller).
state = new_conversation_state("retest", "paisalo", "b")
state = set_pending_confirm(state, slot="plo_payment_intent", fragment_id="confirm_plo_payment_intent")
pending = state.slots.get(PENDING_CONFIRM_KEY)
print("pending_after_set:", pending)
if not (isinstance(pending, dict) and pending.get("slot") == "plo_payment_intent"):
    print("FAIL set_pending_confirm did not persist")
    ok = False

# Simulate T+1: caller captures prior, repair counter uses the capture,
# state still holds the key (gate owns lifecycle).
prior = dict(pending) if isinstance(pending, dict) else None
state2, escalate, reason = track_slot_reask_gated(
    state, question_slot="plo_payment_intent", had_inbound=True,
    max_retries=3, evidence_score=3, prior_pending_confirm=prior,
)
still = PENDING_CONFIRM_KEY in state2.slots
print("pending_survives_repair_counter:", still, "reason=", reason, "escalate=", escalate)
if not still:
    print("FAIL track_slot_reask_gated popped _pending_confirm")
    ok = False
if reason is not None or escalate:
    print("FAIL successful confirm (ev=3) should not increment/escalate")
    ok = False

# turn.py must capture prior BEFORE the gate (source sentinel)
tsrc = inspect.getsource(turn_mod)
has_prior = "_prior_pending_confirm" in tsrc and "pending_confirm=bool(_prior_pending_confirm)" in tsrc
print("turn_captures_prior_pending:", has_prior)
if not has_prior:
    print("FAIL turn.py missing prior_pending_confirm capture")
    ok = False

# 3. haan pakka fixture
profile = get_tenant_profile("paisalo")
st = new_conversation_state("retest2", "paisalo", "b")
s_no = score_evidence(
    transcript="haan pakka", state=st, profile=profile,
    llm_calls=1, commands=[], last_spoken_reply="",
    echo=False, awaited_slot="plo_payment_intent", pending_confirm=False,
)
s_yes = score_evidence(
    transcript="haan pakka", state=st, profile=profile,
    llm_calls=1, commands=[], last_spoken_reply="",
    echo=False, awaited_slot="plo_payment_intent", pending_confirm=True,
)
s_haan = score_evidence(
    transcript="haan", state=st, profile=profile,
    llm_calls=1, commands=[], last_spoken_reply="",
    echo=False, awaited_slot="plo_payment_intent", pending_confirm=True,
)
print(f"haan_pakka_no_pending: evidence={s_no['evidence']} reason={s_no['evidence_reason']} (want 2)")
print(f"haan_pakka_pending: evidence={s_yes['evidence']} reason={s_yes['evidence_reason']} (want 3)")
print(f"bare_haan_pending: evidence={s_haan['evidence']} reason={s_haan['evidence_reason']} (want 3)")
if s_no["evidence"] != 2 or s_yes["evidence"] != 3 or s_haan["evidence"] != 3:
    print("FAIL haan pakka fixture")
    ok = False

# 4. gate still downgrades money-state at ev 2 and executes at ev 3
SLOT_CC = {
    "plo_payment_intent": "money_state",
    "plo_identity_response": "identity_confirm",
}
def ev(s):
    return {"evidence": s, "evidence_reason": "t", "evidence_signals": {}}
cand = [Command(command="set_slot", name="plo_payment_intent", value="willing", source="borrower_claim")]
v2 = commitment_gate(cand, evidence=ev(2), cost_table=None, slot_cost_class=SLOT_CC, identity_ok=True, awaited_slot="plo_payment_intent")
v3 = commitment_gate(cand, evidence=ev(3), cost_table=None, slot_cost_class=SLOT_CC, identity_ok=True, awaited_slot="plo_payment_intent")
print(f"gate_ev2: verdict={v2['verdict']} frag={v2.get('confirm_fragment_id')} (want downgrade)")
print(f"gate_ev3: verdict={v3['verdict']} (want execute)")
if v2["verdict"] != "downgrade" or v3["verdict"] != "execute":
    print("FAIL gate verdicts")
    ok = False

print("ALL_RETEST_SMOKE_PASS:", ok)
raise SystemExit(0 if ok else 1)
PY
echo W24_RETEST_SMOKE_DONE
"""


def main() -> int:
    cfg: dict[str, str] = {}
    sec = ROOT / ".deploy_secret"
    for line in sec.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    script = REMOTE.replace("\r\n", "\n")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["SSH_HOST"], port=int(cfg["SSH_PORT"]),
              username=cfg["SSH_USER"], password=cfg["SSH_PASSWORD"],
              timeout=30, allow_agent=False, look_for_keys=False)
    sftp = c.open_sftp()
    with sftp.file("/tmp/_w24_retest_smoke.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_w24_retest_smoke.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_w24_retest_smoke.sh", timeout=90)
    sys.stdout.write(o.read().decode("utf-8", "replace"))
    sys.stderr.write(e.read().decode("utf-8", "replace"))
    rc = o.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
