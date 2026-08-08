-- PaisaLo loan-detail fields on the borrowers table (nullable; only paisalo rows populate them).
-- Apply via: python scripts/apply_borrower_migrations.py
--
-- These columns back select_plo_scenario (dpd/npa_flag/product) and the Tier-3
-- respond/grounding path (branch/branch_address) for the PaisaLo tenant. Existing
-- rows/tenants are unaffected (all new columns nullable with sensible defaults).

ALTER TABLE borrowers
    ADD COLUMN IF NOT EXISTS repay_amount NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS loan_amount NUMERIC(12, 2),
    ADD COLUMN IF NOT EXISTS due_date DATE,
    ADD COLUMN IF NOT EXISTS disbursal_date DATE,
    ADD COLUMN IF NOT EXISTS days_past_due INTEGER,
    ADD COLUMN IF NOT EXISTS dpd INTEGER,
    ADD COLUMN IF NOT EXISTS branch TEXT,
    ADD COLUMN IF NOT EXISTS branch_address TEXT,
    ADD COLUMN IF NOT EXISTS last_date_paid DATE,
    ADD COLUMN IF NOT EXISTS product TEXT,
    ADD COLUMN IF NOT EXISTS npa_flag BOOLEAN DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_borrowers_tenant_dpd ON borrowers (tenant_id, dpd);
