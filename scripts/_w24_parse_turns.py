#!/usr/bin/env python3
"""Parse turn_decision lines from a captured raw log file into a clean table."""
from __future__ import annotations
import json, pathlib, re, sys

RAW = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "scripts/_w24_turns_cf7d_raw.txt")
text = RAW.read_text(encoding="utf-8", errors="replace")

# Each turn_decision line: INFO app.engine.turn_decision_log turn_decision {json...}
pat = re.compile(r'turn_decision (\{.*\})\s*$')
seen_sids = set()
rows = []
for line in text.splitlines():
    m = pat.search(line)
    if not m:
        continue
    try:
        d = json.loads(m.group(1))
    except Exception:
        continue
    sid = d.get("session_id", "")
    if sid not in seen_sids:
        seen_sids.add(sid)
        print(f"### session={sid}")
    g = d.get("guards", {})
    rows.append({
        "t": len(rows) + 1,
        "transcript": d.get("transcript", ""),
        "evidence": g.get("evidence"),
        "ev_reason": g.get("evidence_reason"),
        "ev_signals": g.get("evidence_signals"),
        "verdict": g.get("gate_verdict"),
        "cost_class": g.get("gate_cost_class"),
        "blocked": g.get("gate_blocked_writes"),
        "repair_reason": g.get("repair_reason"),
        "repair_escalate": g.get("repair_escalate"),
        "compose_fired": g.get("compose_fired"),
        "compose_ids": g.get("compose_fragment_ids"),
        "reply_id": d.get("reply_id"),
        "commands": d.get("commands"),
        "slots_set": d.get("slots_set"),
        "final_text_len": g.get("final_text_len"),
        "outcome": g.get("outcome"),
    })

for r in rows:
    print(f"\n--- t{r['t']} ---")
    print(f"  transcript     : {r['transcript']!r}")
    print(f"  evidence       : {r['evidence']} ({r['ev_reason']}) signals={r['ev_signals']}")
    print(f"  gate_verdict   : {r['verdict']} | cost={r['cost_class']} | blocked={r['blocked']} | repair={r['repair_reason']} esc={r['repair_escalate']}")
    print(f"  compose        : fired={r['compose_fired']} ids={r['compose_ids']}")
    print(f"  reply_id       : {r['reply_id']} (len={r['final_text_len']})")
    print(f"  commands       : {r['commands']}")
    print(f"  slots_set      : {r['slots_set']}")
    print(f"  outcome        : {r['outcome']}")
