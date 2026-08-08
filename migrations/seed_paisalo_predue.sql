-- Seed PaisaLo PREDUE test borrower for ANI 9810587857 (paisalo tenant).
-- Idempotent upsert — safe to re-run. dpd=-5 => 5 days before due => select_plo_scenario -> predue.
-- Run AFTER 002_paisalo_loan_fields.sql (columns must exist).

INSERT INTO borrowers (
    id, name, phone, amount_due, account_ref, language, tenant_id,
    repay_amount, loan_amount, due_date, disbursal_date,
    days_past_due, dpd, branch, branch_address, last_date_paid, product, npa_flag
) VALUES (
    'PLO_RAMESH_PREDUE', 'Ramesh', '+919810587857', 4500, 'PLO-ABF-RM-001', 'hi-IN', 'paisalo',
    4500, 50000, DATE '2026-08-13', DATE '2026-02-09',
    -5, -5, 'Kanpur City', '12 MG Road, Kanpur', DATE '2026-07-13', 'ABF', false
)
ON CONFLICT (id) DO UPDATE SET
    name = EXCLUDED.name,
    phone = EXCLUDED.phone,
    amount_due = EXCLUDED.amount_due,
    account_ref = EXCLUDED.account_ref,
    language = EXCLUDED.language,
    tenant_id = EXCLUDED.tenant_id,
    repay_amount = EXCLUDED.repay_amount,
    loan_amount = EXCLUDED.loan_amount,
    due_date = EXCLUDED.due_date,
    disbursal_date = EXCLUDED.disbursal_date,
    days_past_due = EXCLUDED.days_past_due,
    dpd = EXCLUDED.dpd,
    branch = EXCLUDED.branch,
    branch_address = EXCLUDED.branch_address,
    last_date_paid = EXCLUDED.last_date_paid,
    product = EXCLUDED.product,
    npa_flag = EXCLUDED.npa_flag;
