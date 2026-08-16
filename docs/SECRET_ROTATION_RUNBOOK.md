# Secret rotation runbook (UAT)

_W4-4 · G-A4-03 + W4-2 B1. Execute on the box with Nitish. Scripts never print secrets._

Two rotations in one sitting, then the TOOLS_MODE flip.

| # | What | Script | Closes |
|---|---|---|---|
| 1 | Asterisk ARI password → file only | `ari-orchestrator/scripts/rotate_ari_password.sh` | W4-2 B1 |
| 2 | Per-tenant `media_streams.secret_hash` | `ari-orchestrator/scripts/rotate_media_secrets.sh` | G-A4-03 |
| 3 | Brain `TOOLS_MODE=stub` | `Collection/scripts/uat_tools_mode_stub.sh` | W4-3 / W4-4 |

## 0. Preconditions

- Root on UAT.
- `ORCH_ADMIN_API_KEY` in `/etc/ari-orchestrator/ari-orchestrator.env` (or export it).
- Orchestrator admin API on `127.0.0.1:8095`.
- Compose stack under `/opt/fonada/Websocket/deploy`.
- No live call in flight (or drain first: `deploy/drain_restart.sh`).

## 1. ARI password (W4-2 leftover)

```bash
sudo bash /opt/fonada/ari-orchestrator/scripts/rotate_ari_password.sh
# Expect: Host greps of the world-readable env for ARI_PASSWORD now fail.
curl -sS -u orchestrator:wrong http://127.0.0.1:8088/ari/asterisk/info | head -c 80; echo
```

Legit manual originate after this: `Collection/scripts/dialer_originate.py` → `/dialer/v0/originate`. Raw ARI curls from the box fail loudly.

## 2. Media secrets (G-A4-03)

PaisaLo and Salary-On-Time currently share `secret_hash` / hint `ef01`. A client authed for one tenant can open the other's media WS.

```bash
sudo ORCH_ADMIN_API_KEY=… bash /opt/fonada/ari-orchestrator/scripts/rotate_media_secrets.sh
```

What it does:

- GET each tenant's `media_ws_url` via `/admin/v1/media-stream`.
- Writes a **new unique** secret to `/etc/fonada/media_secrets/{tenant}.secret` (0640).
- PUT the same URL + new secret (SKU `plo` → `g722`).
- Fails if `secret_hint` is still shared.
- Does **not** write raw secrets into the compose `.env`.

Verify:

```bash
# hints must differ
curl -sS -H "Authorization: Bearer $ORCH_ADMIN_API_KEY" \
  'http://127.0.0.1:8095/admin/v1/media-stream?tenant=paisalo'
curl -sS -H "Authorization: Bearer $ORCH_ADMIN_API_KEY" \
  'http://127.0.0.1:8095/admin/v1/media-stream?tenant=salary-on-time'
sudo systemctl restart ari-orchestrator   # if in-process cache
```

Then one short PaisaLo dial + one SOT dial. Media WS must still connect. If a tenant 401s, the connector is still holding the old shared secret — bounce connector after orch.

## 3. TOOLS_MODE=stub

`CARRIER=asterisk` + `TOOLS_MODE=simulate` now fails brain startup (W4-3).

```bash
sudo bash /opt/fonada/Collection/scripts/uat_tools_mode_stub.sh
# healthz tools_mode=stub
# stub borrower-state against the local Postgres seed
```

## 4. After

- `GET /version` on brain (`:8000`) and orch (`:8095`) and go-server (`:8080`) — sha matches the images just loaded.
- Grep `call_summary` on the next live call (one JSON line at session end).
- Weekly: `python scripts/mining_weekly.py` → `docs/mining/YYYY-WW.md`.
