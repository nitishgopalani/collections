import os

# Force stub mode for all tests (no external creds required in CI).
os.environ.setdefault("STUB_MODE", "true")
os.environ.setdefault("KB_STUB", "true")
os.environ.setdefault("TOOLS_STUB", "true")
os.environ.setdefault("LLM_STUB", "true")
# Never make a real human-transfer network call from tests, even if the local
# .env has TRANSFER_MODE=live (env vars take precedence over the .env file).
os.environ.setdefault("TRANSFER_MODE", "stub")
os.environ.setdefault("TRANSFER_ENDPOINT_URL", "")
# Same for the live WhatsApp sender.
os.environ.setdefault("WHATSAPP_MODE", "stub")
os.environ.setdefault("WHATSAPP_ENDPOINT_URL", "")
# CALM digression defaults OFF in tests (the local .env enables it for live calls).
# Env vars take precedence over the .env file, so this pins deterministic behaviour;
# individual tests opt in with monkeypatch.setenv("SOT_DIGRESSION", "true").
os.environ.setdefault("SOT_DIGRESSION", "false")
# The local .env is provisioned for the live test server (TEST_MODE on, 24/7 call
# window). Pin the framework defaults here so config-sensitive tests are deterministic;
# tests that need the live behaviour opt in with monkeypatch.setenv(...).
os.environ.setdefault("TEST_MODE", "false")
os.environ.setdefault("CALL_WINDOW_START", "08:00")
os.environ.setdefault("CALL_WINDOW_END", "19:00")
os.environ.setdefault("CALL_WINDOW_TIMEZONE", "Asia/Kolkata")
