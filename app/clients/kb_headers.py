"""Shared Fonada KB HTTP headers."""

from app.config import Settings


def kb_client_headers(settings: Settings) -> dict[str, str]:
    # SECURITY: runtime KB calls must use a client/agent API key, never the admin key.
    return {
        "X-API-Key": settings.kb_api_key,
        "Content-Type": "application/json",
        "User-Agent": settings.kb_user_agent,
    }


def kb_health_headers(settings: Settings) -> dict[str, str]:
    return {
        "X-API-Key": settings.kb_health_api_key,
        "Content-Type": "application/json",
        "User-Agent": settings.kb_user_agent,
    }
