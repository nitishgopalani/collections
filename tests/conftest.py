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
