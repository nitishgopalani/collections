import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
WL = ROOT / "docs" / "WORKLOG.md"
parts = [ROOT / "docs" / "_worklog_011_entry.md", ROOT / "docs" / "_worklog_011_entry_b.md"]
with WL.open("a", encoding="utf-8") as f:
    for p in parts:
        f.write(p.read_text(encoding="utf-8"))
print("appended", sum(p.read_text(encoding="utf-8").count(chr(10)) for p in parts), "lines")
