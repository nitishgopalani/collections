"""Borrower DB URL built safely from POSTGRES_* env components."""

from app.config import Settings, get_settings


def test_effective_borrower_database_url_encodes_special_password(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "collections")
    monkeypatch.setenv("POSTGRES_PASSWORD", "123!q!@secret")
    monkeypatch.setenv("POSTGRES_DB", "collections")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.delenv("BORROWER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        url = get_settings().effective_borrower_database_url
        assert url.startswith("postgresql://collections:")
        assert "@postgres:5432/collections" in url
        assert "123!q!@secret" not in url
        assert "123%21q%21%40secret" in url
    finally:
        # Drop the POSTGRES_*-poisoned Settings from the cache; otherwise every
        # later test sees borrower_db=True pointing at an unreachable host and
        # the WS-contract tests hang on the DB connect.
        get_settings.cache_clear()
