"""Legit manual originate — the only supported host-side dial path after W4-1 B1.

Calls brain POST /dialer/v0/originate (DNC / cadence / active-call gate).
Does NOT curl Asterisk ARI. Raw ARI creds are container-only after rotation.

  python scripts/dialer_originate.py --borrower-id plo_test_borrower --phone 9810587857
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Gated campaign originate via /dialer/v0")
    parser.add_argument("--borrower-id", required=True)
    parser.add_argument("--phone", required=True)
    parser.add_argument("--tenant-id", default="paisalo")
    parser.add_argument("--brain", default=os.environ.get("BRAIN_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--place-call", action="store_true", help="Ask brain to hit orchestrator after the gate")
    args = parser.parse_args()

    url = args.brain.rstrip("/") + "/dialer/v0/originate"
    body = json.dumps(
        {
            "borrower_id": args.borrower_id,
            "phone": args.phone,
            "tenant_id": args.tenant_id,
            "dry_run": args.dry_run,
            "place_call": args.place_call,
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(resp.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8"), file=sys.stderr)
        print("originate refused (gate). Do not fall back to ARI curl.", file=sys.stderr)
        return exc.code or 1


if __name__ == "__main__":
    raise SystemExit(main())
