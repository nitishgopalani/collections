"""Weekly mining report from turn_decision logs + obligation exports.

Usage:
  python scripts/mining_weekly.py
  python scripts/mining_weekly.py --logs scripts --exports exports --week 2026-33
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_TURN_RE = re.compile(r"turn_decision\s+(\{.*\})\s*$")
_LADDER_SIDS = {
    "1debe02d": "ondue",
    "db767332": "postdue1",
    "a8642ebb": "postdue3",
    "5c6c7663": "npa",
    "d66ce098": "predue",
    "950e271c": "predue",
    "e1d5d837": "ondue",
    "dc4c5808": "predue",
    "dfae962c": "predue",
}


def iso_week(day: date | None = None) -> str:
    d = day or datetime.now(_IST).date()
    iso = d.isocalendar()
    return f"{iso.year}-{iso.week:02d}"


def parse_turn_line(line: str) -> dict[str, Any] | None:
    m = _TURN_RE.search(line.rstrip())
    if not m:
        idx = line.find("turn_decision {")
        if idx < 0:
            return None
        raw = line[idx + len("turn_decision ") :].strip()
    else:
        raw = m.group(1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def iter_log_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    files: list[Path] = []
    for pat in ("*.txt", "*.log", "*.jsonl"):
        files.extend(root.rglob(pat))
    return [p for p in files if p.is_file()]


def load_turns(log_roots: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in log_roots:
        if not root.exists():
            continue
        for path in iter_log_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if "turn_decision" not in line:
                    continue
                payload = parse_turn_line(line)
                if not payload:
                    continue
                key = (
                    str(payload.get("session_id") or ""),
                    str(payload.get("transcript") or "")[:80],
                    str(payload.get("reply_id") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(payload)
    return rows


def load_exports(export_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not export_dir.is_dir():
        return rows
    for path in sorted(export_dir.glob("dispositions_*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _guards(row: dict[str, Any]) -> dict[str, Any]:
    g = row.get("guards")
    return g if isinstance(g, dict) else {}


def render_report(turns: list[dict[str, Any]], exports: list[dict[str, Any]], week: str) -> str:
    sessions = defaultdict(list)
    for row in turns:
        sid = str(row.get("session_id") or "")
        if sid:
            sessions[sid].append(row)

    related_miss: Counter[str] = Counter()
    ack_dropped = 0
    unknown_info = 0
    scope_miss = 0
    hatch = 0
    l1_topics: Counter[str] = Counter()
    confirm_ok = 0
    confirm_total = 0
    oof_llm = 0

    for row in turns:
        g = _guards(row)
        spoken = str(row.get("reply_id") or "")
        if g.get("related_miss"):
            related_miss[str(row.get("transcript") or "")[:60] or spoken] += 1
        if g.get("ack_dropped"):
            ack_dropped += 1
        if "unknown_info" in spoken or g.get("unknown_info"):
            unknown_info += 1
        if g.get("scope_miss"):
            scope_miss += 1
        if g.get("escape_hatch_used"):
            hatch += 1
        if g.get("oof_layer") == "llm" or (
            g.get("oof_class") and g.get("related") is False
        ):
            oof_llm += 1
            topic = str(g.get("oof_subclass") or g.get("oof_class") or "unclassified")
            l1_topics[topic] += 1
        verdict = g.get("gate_verdict")
        if verdict in {"execute", "downgrade"} and g.get("confirm_fragment_id"):
            confirm_total += 1
            if verdict == "execute" and not g.get("would_downgrade"):
                confirm_ok += 1
        if g.get("gate_verdict") == "execute" and g.get("evidence", 0) >= 3:
            if str(g.get("gate_cost_class") or "") in {"money_state", "identity_confirm"}:
                confirm_total += 1
                confirm_ok += 1

    n_turns = len(turns)
    n_sess = len(sessions)
    hatch_rate = (100.0 * hatch / n_turns) if n_turns else 0.0
    unknown_rate = (100.0 * unknown_info / n_turns) if n_turns else 0.0
    confirm_pct = (100.0 * confirm_ok / confirm_total) if confirm_total else 0.0

    ladder_hits = [
        f"`{sid[:8]}` {label}"
        for sid, rows in sessions.items()
        for prefix, label in _LADDER_SIDS.items()
        if sid.startswith(prefix)
    ]

    lines = [
        f"# Mining report {week}",
        "",
        f"_Generated {datetime.now(_IST).isoformat(timespec='seconds')} IST. "
        f"Source: turn_decision logs + dispositions jsonl._",
        "",
        "## Coverage",
        "",
        f"- Sessions: **{n_sess}**",
        f"- Turns: **{n_turns}**",
        f"- Export rows: **{len(exports)}**",
        f"- Ladder/pilot sessions seen: {', '.join(ladder_hits) or '(none in this pull)'}",
        "",
        "## Counters",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| related_miss turns | {sum(related_miss.values())} |",
        f"| ack_dropped | {ack_dropped} |",
        f"| unknown_info rate | {unknown_rate:.1f}% ({unknown_info}/{n_turns or 1}) |",
        f"| scope_miss | {scope_miss} |",
        f"| hatch rate | {hatch_rate:.1f}% ({hatch}/{n_turns or 1}) |",
        f"| confirm-success | {confirm_ok}/{confirm_total or 0} ({confirm_pct:.0f}%) |",
        f"| L1/llm OOF turns | {oof_llm} |",
        "",
        "## related_miss clusters",
        "",
    ]
    if related_miss:
        for text, n in related_miss.most_common(12):
            lines.append(f"- `{n}×` {text}")
    else:
        lines.append("_None in this window._")
    lines += ["", "## L1 topics (L0 promotion candidates)", ""]
    if l1_topics:
        for topic, n in l1_topics.most_common(12):
            flag = " — **promote if ≥5 sessions / 7d**" if n >= 5 else ""
            lines.append(f"- `{topic}` ×{n}{flag}")
    else:
        lines.append("_No llm-layer OOF clusters._")
    lines += [
        "",
        "## Notes",
        "",
        "- Diversion (`redirect_count`) and repair counters stay independent.",
        "- Do not auto-author fragment text. L0 promotion still needs client ack.",
        "- Ops grep: `call_summary` (one JSON line per session).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly turn_decision mining report")
    parser.add_argument("--logs", action="append", default=[], help="file or dir of turn_decision lines")
    parser.add_argument("--exports", default="exports", help="obligation export dir")
    parser.add_argument("--week", default="", help="YYYY-WW (default: current IST week)")
    parser.add_argument("--out", default="", help="output markdown path")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    log_roots = [Path(p) for p in args.logs] if args.logs else [root / "scripts", root / "exports"]
    week = args.week or iso_week()
    turns = load_turns(log_roots)
    exports = load_exports(Path(args.exports) if Path(args.exports).is_absolute() else root / args.exports)
    report = render_report(turns, exports, week)
    out = Path(args.out) if args.out else root / "docs" / "mining" / f"{week}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out} sessions={len({t.get('session_id') for t in turns})} turns={len(turns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
