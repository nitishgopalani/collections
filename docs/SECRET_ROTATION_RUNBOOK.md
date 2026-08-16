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

**On-box result (16 Aug 2026):** route **B1**. Hints now differ: SOT `Z51k` · PaisaLo `i8vY`. Shared `ef01` hole is closed.

Admin PUT hardcodes `AllowWS=false` unless this box sets a UAT-only flag. There is no `ENV=uat` media gate.

```bash
# this box only — never production
# /etc/ari-orchestrator/ari-orchestrator.env
ORCH_ALLOW_INSECURE_MEDIA_WS=true
# alias also accepted: ALLOW_INSECURE_MEDIA_WS=true
```

What the flag does:

- Admin `PUT /admin/v1/media-stream` may keep an existing `ws://` + private host (PaisaLo is `ws://172.18.0.1:8080/stream`).
- Persists `allow_private_urls=true` so the **connector** dial-time SSRF also allows `ws://`. Without that row, orch PUT succeeds and the connector still refuses BYO (`ssrf: media_ws_url must use wss://`).
- `ari.secret` must be `root:asterisk` 0640. The unit is `User=asterisk`; `root:root` 0640 → empty ARI password → `websocket: bad handshake` → Stasis app not registered.

```bash
sudo ORCH_ADMIN_API_KEY=… MEDIA_TENANTS=paisalo \
  HTTP_LISTEN_ADDR=172.18.0.1:8095 \
  bash /opt/fonada/ari-orchestrator/scripts/rotate_media_secrets.sh
```

Authorization header is `Admin <key>`, not `Bearer`. Script writes `/etc/fonada/media_secrets/{tenant}.secret` 0640 and fails if hints are still shared. Does not write raw secrets into compose `.env`. Go-server has no per-tenant `FONADA_MEDIA_SECRET` on this box (live `/stream` does not HMAC-verify; orch mints the dial token).

Verify:

```bash
# hints must differ (Authorization: Admin …)
curl -sS -H "Authorization: Admin $ORCH_ADMIN_API_KEY" \
  'http://172.18.0.1:8095/admin/v1/media-stream?tenant=paisalo'
curl -sS -H "Authorization: Admin $ORCH_ADMIN_API_KEY" \
  'http://172.18.0.1:8095/admin/v1/media-stream?tenant=salary-on-time'
```

Synthetic smoke (Local/5000, no human dial): both tenants BYO-authenticate, 8 kHz slin, PaisaLo voice amit (NPA stub), SOT voice amit. If a tenant 401s, bounce `asterisk-connector` and re-smoke.

**wss:// + cert for the media endpoint** remains a W-post-pilot item. On-box connector cannot hairpin `wss://voice-api.fonada.ai:18444` (`103.132.145.55:18444` i/o timeout). UAT SOT URL was pointed at the same local `ws://172.18.0.1:8080/stream` as PaisaLo so the live path works.

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
