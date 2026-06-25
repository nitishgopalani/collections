import os

# Force stub mode for all tests (no external creds required in CI).
os.environ.setdefault("STUB_MODE", "true")
os.environ.setdefault("KB_STUB", "true")
os.environ.setdefault("TOOLS_STUB", "true")
os.environ.setdefault("LLM_STUB", "true")
