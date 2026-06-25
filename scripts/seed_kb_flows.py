#!/usr/bin/env python3
"""Seed local flow descriptions into the Fonada FAISS knowledge base."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.clients.kb import ADD_TEXT_PATH, STATS_PATH
from app.clients.kb_headers import kb_client_headers
from app.config import get_settings
from app.engine.retrieval import FLOW_DOC_MAP_PATH, tagged_flow_text

HEALTH_API_PATH = "/api/health"


def _load_flow_descriptions(flows_dir: Path) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for path in sorted(flows_dir.glob("*.yml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        flows = raw.get("flows", {})
        if not isinstance(flows, dict):
            continue
        for name, definition in flows.items():
            if isinstance(definition, dict) and definition.get("description"):
                descriptions[str(name)] = str(definition["description"]).strip()
    return descriptions


def _extract_doc_id(payload: dict[str, Any]) -> str | None:
    for key in ("doc_id", "id", "document_id"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _print_stats(client: httpx.Client, base: str, headers: dict[str, str]) -> None:
    try:
        response = client.get(f"{base}{STATS_PATH}", headers=headers)
        response.raise_for_status()
        stats = response.json()
        print(f"stats {response.status_code}: {json.dumps(stats, ensure_ascii=False)}")
    except httpx.HTTPError as exc:
        print(f"stats check failed: {exc}")


def seed_flows(force: bool = False, clear: bool = False) -> int:
    settings = get_settings()
    if not settings.kb_api_key:
        print("KB_API_KEY is not set — use a client/agent key (never the admin key).")
        return 1

    flows_dir = Path(__file__).resolve().parents[1] / "app" / "flows"
    descriptions = _load_flow_descriptions(flows_dir)
    if not descriptions:
        print("No flow descriptions found to seed.")
        return 1

    map_path = FLOW_DOC_MAP_PATH
    existing: dict[str, str] = {}
    if map_path.is_file() and not clear:
        existing = json.loads(map_path.read_text(encoding="utf-8"))

    if clear:
        existing = {}

    base = settings.kb_base_url.rstrip("/")
    headers = kb_client_headers(settings)
    verify = settings.kb_verify_ssl

    # SECURITY: seeding uses the client key; admin key is not used by the engine runtime.
    added = 0
    skipped = 0
    failed = 0
    updated_map: dict[str, str] = dict(existing)
    seeded_flows = {flow_name for flow_name in updated_map.values()}

    with httpx.Client(timeout=30.0, verify=verify) as client:
        health = client.get(
            f"{base}{HEALTH_API_PATH}",
            headers={
                "X-API-Key": settings.kb_health_api_key,
                "Content-Type": "application/json",
                "User-Agent": settings.kb_user_agent,
            },
        )
        print(f"health {health.status_code}: {health.text[:120]}")

        for flow_name, description in descriptions.items():
            if flow_name in seeded_flows and not force:
                print(f"skip  {flow_name} (already mapped)")
                skipped += 1
                continue

            text = tagged_flow_text(flow_name, description)
            try:
                response = client.post(
                    f"{base}{ADD_TEXT_PATH}",
                    headers=headers,
                    json={"text": text},
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    print(f"fail  {flow_name}: unexpected response type")
                    failed += 1
                    continue
                doc_id = _extract_doc_id(body)
                if doc_id:
                    updated_map[doc_id] = flow_name
                    print(f"add   {flow_name} -> {doc_id}")
                elif body.get("status") == "success" or body.get("chunks_added"):
                    print(f"add   {flow_name} (tag-only; no doc_id in response)")
                else:
                    print(f"fail  {flow_name}: unexpected response: {body}")
                    failed += 1
                    continue
                seeded_flows.add(flow_name)
                added += 1
            except httpx.HTTPError as exc:
                print(f"fail  {flow_name}: {exc}")
                failed += 1

        _print_stats(client, base, headers)

    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text(json.dumps(updated_map, indent=2), encoding="utf-8")
    print(
        f"\nSummary: added={added} skipped={skipped} failed={failed} "
        f"map_entries={len(updated_map)} -> {map_path}"
    )
    return 0 if failed == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed flow descriptions into Fonada KB")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-add flows even if already present in flow_doc_map.json",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear local flow_doc_map.json before seeding",
    )
    args = parser.parse_args()
    sys.exit(seed_flows(force=args.force, clear=args.clear))


if __name__ == "__main__":
    main()
