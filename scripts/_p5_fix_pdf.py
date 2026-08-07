"""OCR-repair PaisaLo PDF joined extract → fixed JSON/text for YAML authoring."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOINED = ROOT / "scripts" / "_paisalo_pdf_joined.txt"

REPAIRS = [
    (r"कश्त", "किश्त"),
    (r"कस्त", "किश्त"),
    (r"दनों", "दिनों"),
    (r"दलाने", "दिलाने"),
    (r"\bलए\b", "लिए"),
    (r"राश(?!ि)", "राशि"),
    (r"इसलए", "इसलिए"),
    (r"\bसफ\b", "सिर्फ"),
    (r"\bकसी\b", "किसी"),
    (r"डीलरशप", "डीलरशिप"),
    (r"कारवाई", "कार्रवाई"),
    (r"कारवाही", "कार्रवाई"),
    (r"कायवाही", "कार्रवाई"),
    (r"डफॉल्ट", "डिफ़ॉल्ट"),
    (r"कीिजए", "कीजिए"),
    (r"िस्थत", "स्थिति"),
    (r"मुिश्कल", "मुश्किल"),
    (r"मनट", "मिनट"),
    (r"घोषत", "घोषित"),
    (r"सबल", "सिबिल"),
    (r"भवष्य", "भविष्य"),
    (r"मलने", "मिलने"),
    (r"आंशक", "आंशिक"),
    (r"वास्तवक", "वास्तविक"),
    (r"उचत", "उचित"),
    (r"संपक", "संपर्क"),
    (r"कमचारी", "कर्मचारी"),
    (r"कममचारी", "कर्मचारी"),
    (r"नगद", "नकद"),
    (r"पसनल", "पर्सनल"),
    (r"नज़दीकी", "नज़दीकी"),
    (r"नजदीकी", "नज़दीकी"),
    (r"पैसलो", "पैसालो"),
    (r"पैसा लो", "पैसालो"),
    (r"माफ़", "माफ़"),
    (r"लंबत", "लंबित"),
    (r"अभलेखों", "अभिलेखों"),
    (r"आधकारक", "आधिकारिक"),
    (r"नधारत", "निर्धारित"),
    (r"व्यिक्त", "व्यक्ति"),
    (r"बल्कुल", "बिल्कुल"),
    (r"बलकुल", "बिल्कुल"),
    (r"चंता", "चिंता"),
    (r"कठनाई", "कठिनाई"),
    (r"यद ", "यदि "),
    (r"लया", "लिया"),
    (r"दए", "दिए"),
    (r"चाहए", "चाहिए"),
    (r"वरुद्ध", "विरुद्ध"),
    (r"प्रभावत", "प्रभावित"),
    (r"फर ", "फिर "),
    (r"बढ़या", "बढ़िया"),
    (r"ख़ारज", "खारज"),
]


def fix(s: str) -> str:
    for a, b in REPAIRS:
        s = re.sub(a, b, s)
    repl = {
        "{{amount}}": "{repay_amount}",
        "{{customer_name}}": "{customer_name}",
        "{{days_past_due}}": "{days_past_due}",
        "{{loan_amount}}": "{loan_amount}",
        "{{disbursal_date}}": "{disbursal_date}",
        "{{last_date_paid}}": "{last_date_paid}",
        "{{branch}}": "{branch}",
    }
    for a, b in repl.items():
        s = s.replace(a, b)
    s = s.replace("QR code", "QR कोड").replace("QR Code", "QR कोड")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def main() -> None:
    text = JOINED.read_text(encoding="utf-8")
    pages: dict[int, list[str]] = {}
    cur: int | None = None
    for ln in text.splitlines():
        m = re.match(r"===== PAGE (\d+) =====", ln)
        if m:
            cur = int(m.group(1))
            pages[cur] = []
        elif cur is not None and ln.strip():
            pages[cur].append(fix(ln.strip()))

    out_json = ROOT / "scripts" / "_paisalo_pdf_fixed.json"
    out_json.write_text(json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf-8")
    chunks: list[str] = []
    for n in sorted(pages):
        chunks.append(f"===== PAGE {n} =====")
        chunks.extend(pages[n])
        chunks.append("")
    (ROOT / "scripts" / "_paisalo_pdf_fixed.txt").write_text(
        "\n".join(chunks), encoding="utf-8"
    )
    print(f"pages={len(pages)} lines={sum(len(v) for v in pages.values())}")
    print("wrote", out_json)


if __name__ == "__main__":
    main()
