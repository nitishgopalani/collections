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

## Development

```bash
pytest
ruff check app tests
black --check app tests
mypy app
```
