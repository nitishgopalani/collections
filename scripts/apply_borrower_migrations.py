#!/usr/bin/env python3
"""Apply borrower migrations to local Postgres (docker compose postgres service)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings

MIGRATIONS_DIR = ROOT / "migrations"


def apply_sql(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    conn.execute(sql)
    conn.commit()
    print(f"applied {path.name}")


def describe_borrowers(conn: psycopg.Connection) -> None:
    cur = conn.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'borrowers'
        ORDER BY ordinal_position
        """
    )
    rows = cur.fetchall()
    print("\nborrowers schema:")
    for row in rows:
        print(
            f"  {row['column_name']:16} {row['data_type']:24} "
            f"nullable={row['is_nullable']} default={row['column_default']}"
        )


def print_borrowers(conn: psycopg.Connection) -> None:
    cur = conn.execute(
        """
        SELECT id, name, phone, amount_due, account_ref, language, tenant_id, created_at
        FROM borrowers
        ORDER BY created_at
        """
    )
    rows = cur.fetchall()
    print(f"\nborrowers rows ({len(rows)}):")
    for row in rows:
        print(
            f"  id={row['id']} name={row['name']} phone={row['phone']} "
            f"amount_due={row['amount_due']} tenant_id={row['tenant_id']}"
        )


def main() -> int:
    settings = get_settings()
    url = settings.effective_borrower_database_url
    if not url:
        print("Set BORROWER_DATABASE_URL or DATABASE_URL to local postgres (see .env.example).")
        return 1

    migrations = sorted(MIGRATIONS_DIR.glob("0*.sql"))
    seed = MIGRATIONS_DIR / "seed_local_borrowers.sql"
    if not migrations:
        print(f"Missing migrations in {MIGRATIONS_DIR}")
        return 1

    with psycopg.connect(url, row_factory=dict_row) as conn:
        for migration in migrations:
            apply_sql(conn, migration)
        describe_borrowers(conn)
        if seed.is_file():
            apply_sql(conn, seed)
            print_borrowers(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
