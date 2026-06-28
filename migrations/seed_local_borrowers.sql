-- Seed local test borrowers (run after 001_borrowers.sql). Replace phone with your E.164 test number.
-- Idempotent upserts — safe to re-run.

INSERT INTO borrowers (id, name, phone, amount_due, account_ref, language, tenant_id)
VALUES
    ('B_RAJESH', 'Rajesh', '+919810587857', 350, 'LN-RAJ-001', 'hi-IN', 'default'),
    ('B_PRIYA', 'Priya', '+919876543211', 1200, 'LN-PRI-002', 'hi-IN', 'default'),
    ('B_AMIT', 'Amit', '+919876543212', 750, 'LN-AMI-003', 'en-IN', 'default')
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    phone = EXCLUDED.phone,
    amount_due = EXCLUDED.amount_due,
    account_ref = EXCLUDED.account_ref,
    language = EXCLUDED.language,
    tenant_id = EXCLUDED.tenant_id;
