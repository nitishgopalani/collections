#!/bin/bash
# W4-4 — flip UAT brain to TOOLS_MODE=stub and smoke get_borrower_state.
# Run on the box (same sitting as secret rotation).
set -euo pipefail

ENV_FILE="${BRAIN_ENV:-/opt/fonada/Websocket/deploy/.env}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/fonada/Websocket/deploy}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 1
fi

if grep -q '^TOOLS_MODE=' "$ENV_FILE"; then
  sed -i 's/^TOOLS_MODE=.*/TOOLS_MODE=stub/' "$ENV_FILE"
else
  echo 'TOOLS_MODE=stub' >> "$ENV_FILE"
fi

echo "TOOLS_MODE now: $(grep '^TOOLS_MODE=' "$ENV_FILE")"

cd "$COMPOSE_DIR"
docker compose up -d --no-deps --force-recreate brain
sleep 8
curl -sf http://127.0.0.1:8000/healthz | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('tools_mode')=='stub', d; print('healthz tools_mode=stub')"
curl -sf http://127.0.0.1:8000/version | python3 -c "import json,sys; d=json.load(sys.stdin); print('brain /version', d)"

docker exec fonada-voice-brain-1 python - <<'PY'
import asyncio
from app.clients.tools import create_tool_client
from app.memory.store import create_memory_store

async def main():
    mem = create_memory_store()
    tools = create_tool_client()
    bind = getattr(tools, "bind_source", None)
    if callable(bind):
        bind(mem)
    assert getattr(tools, "mode", "") == "stub", getattr(tools, "mode", None)
    row = await tools.get_borrower_state(phone="9810587857", tenant_id="paisalo")
    print("stub borrower-state", {k: row.get("result", {}).get(k) for k in ("found", "outstanding", "loan_ref")})
    assert row.get("ok") is True

asyncio.run(main())
PY

echo "UAT TOOLS_MODE=stub smoke PASS"
