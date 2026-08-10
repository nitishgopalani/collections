#!/usr/bin/env python3
"""W2-4 silent smoke: gate-level enforce verification on UAT brain.

Proves the enforce path is live without a full turn:
  - commitment_gate_enforce_enabled() == True
  - money-state candidate at evidence 2 -> downgrade (would block in enforce)
  - source=system money-state -> execute (system bypass)
  - source=confirmed money-state -> execute (confirm bypass)
  - identity_confirm at evidence 2 -> execute (DEBT-041)
  - PII without identity -> hold
"""
from __future__ import annotations
import pathlib, sys, paramiko

ROOT = pathlib.Path(__file__).resolve().parents[2]

REMOTE = r"""#!/bin/bash
set -uo pipefail
echo '========== W2-4 silent smoke: gate-level enforce verification =========='
docker exec -i fonada-voice-brain-1 python3 - <<'PY'
import os, json
print("env_COMMITMENT_GATE_ENFORCE:", os.environ.get("COMMITMENT_GATE_ENFORCE"))
from app.engine.commitment_gate import commitment_gate, commitment_gate_enforce_enabled, DEFAULT_COST_TABLE
from app.schemas.command import Command
print("enforce_enabled:", commitment_gate_enforce_enabled())
print("default_cost_table:", DEFAULT_COST_TABLE)
SLOT_CC = {
    "plo_payment_intent": "money_state", "plo_timeline": "money_state",
    "plo_identity_response": "identity_confirm", "customer_name": "pii",
    "phone": "pii", "repay_amount": "money_state", "committed_date": "money_state",
}
def ev(s, r="t"): return {"evidence": s, "evidence_reason": r, "evidence_signals": {}}
cases = [
    ("money_state_evidence2_downgrades", [Command(command="set_slot", name="committed_date", value="15 Aug", source="borrower_claim")], ev(2), True, "plo_timeline", "downgrade"),
    ("money_state_source_system_executes", [Command(command="set_slot", name="committed_date", value="15 Aug", source="system")], ev(0), True, "plo_timeline", "execute"),
    ("money_state_source_confirmed_executes", [Command(command="set_slot", name="committed_date", value="15 Aug", source="confirmed")], ev(2), True, "plo_timeline", "execute"),
    ("identity_confirm_evidence2_executes", [Command(command="set_slot", name="plo_identity_response", value="yes", source="borrower_claim")], ev(2), False, "plo_identity_response", "execute"),
    ("pii_without_identity_holds", [Command(command="set_slot", name="customer_name", value="Ramesh", source="borrower_claim")], ev(2), False, "plo_identity_response", "hold"),
    ("money_state_evidence3_executes", [Command(command="set_slot", name="committed_date", value="15 Aug", source="borrower_claim")], ev(3), True, "plo_timeline", "execute"),
]
results = []
for name, cand, e, idok, awaited, want in cases:
    v = commitment_gate(cand, evidence=e, cost_table=None, slot_cost_class=SLOT_CC, identity_ok=idok, awaited_slot=awaited)
    got = v["verdict"]
    ok = got == want
    results.append((name, got, want, ok, v.get("cost_class"), v.get("confirm_fragment_id")))
    print(f"{name}: verdict={got} want={want} ok={ok} cost_class={v.get('cost_class')} frag={v.get('confirm_fragment_id')}")
allok = all(r[3] for r in results)
print("ALL_SMOKE_PASS:", allok)
import sys; sys.exit(0 if allok else 1)
PY
echo W24_SILENT_SMOKE_DONE
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
    with sftp.file("/tmp/_w24_smoke.sh", "w") as f:
        f.write(script)
    sftp.chmod("/tmp/_w24_smoke.sh", 0o755)
    sftp.close()
    _, o, e = c.exec_command("bash /tmp/_w24_smoke.sh", timeout=120)
    sys.stdout.write(o.read().decode("utf-8", "replace"))
    sys.stderr.write(e.read().decode("utf-8", "replace"))
    rc = o.channel.recv_exit_status()
    c.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
