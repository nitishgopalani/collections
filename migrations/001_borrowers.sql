-- Local test-stack borrower table (brain phone lookup). NOT for Supabase/managed DB.
-- Apply via: python scripts/apply_borrower_migrations.py

CREATE TABLE IF NOT EXISTS borrowers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    amount_due NUMERIC(12, 2) NOT NULL DEFAULT 0,
    account_ref TEXT,
    language TEXT NOT NULL DEFAULT 'hi-IN',
    tenant_id TEXT NOT NULL DEFAULT 'default',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (phone, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_borrowers_phone_tenant ON borrowers (phone, tenant_id);
CREATE INDEX IF NOT EXISTS idx_borrowers_tenant ON borrowers (tenant_id);
