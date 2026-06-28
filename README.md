# Collections Dialogue Engine

Text-in → text-out conversational engine for RBI-compliant outbound EMI collections.

See [docs/BUILD_SPEC.md](docs/BUILD_SPEC.md) for the full build specification.

## Quick start (Windows)

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env    # set creds or keep STUB_MODE=true
uvicorn app.main:app --reload
```

**Note:** If `python` opens a "Select an app" dialog, Windows is using a Store stub
(`C:\Windows\System32\python`). Use `py` or the venv path instead:
`.\.venv\Scripts\python.exe`.

## Quick start (Unix)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload
```

- `GET /healthz` — service and client connectivity
- `POST /turn` — process one dialogue turn (Sprint 0: static reply)

## Flow simulator (local, no telephony)

Replay scripted borrower lines through the same `handle_turn` pipeline as `POST /turn`
— no audio, Sarvam, or ElevenLabs. Use this to see which flow, `reply_id`, resolved
text, and gate decision fire at each step.

**Scripted run** (uses `llm_commands` in the JSON — no Vertex needed):

```powershell
.\.venv\Scripts\python.exe scripts\flow_sim.py --script tests\sim\happy_path_ptp.json
```

**Interactive REPL** (needs `--live-llm` for real command-gen, or turns fall through to clarify):

```powershell
.\.venv\Scripts\python.exe scripts\flow_sim.py --interactive `
  --call-id sim-1 --borrower-id B_VERIFY_OK --borrower-fixture B_VERIFY_OK `
  --call-date 2026-06-25 --live-llm
```

**Call-window override** — pin gate clock without changing `CALL_WINDOW_*` env:

```powershell
# Inside window (10:00 IST)
.\.venv\Scripts\python.exe scripts\flow_sim.py --script tests\sim\happy_path_ptp.json `
  --gate-now 2026-06-25T10:00:00+05:30

# Outside window (deliberate silent)
.\.venv\Scripts\python.exe scripts\flow_sim.py --script tests\sim\after_hours.json
```

Example scripts live under `tests/sim/` (`happy_path_ptp.json`, `identity_then_dispute.json`,
`after_hours.json`, `simple_ptp_test.json`). Each run prints an annotated transcript and flags turns with empty
text that lack a deliberate gate reason (the t4/t5 bug class).

**Simple PTP test flow** (TEST ONLY — name-only identity, not production-compliant):

```powershell
py -3 scripts\flow_sim.py --script tests\sim\simple_ptp_test.json
```

Use `tenant_id=test-simple-ptp` and `turn_meta.force_flow=simple_ptp_test` on live test calls.

```bash
pytest tests/unit/test_flow_sim.py
```

## Development

```bash
pytest
ruff check app tests
black --check app tests
mypy app
```
