"""Hardcoded test borrower for TEST_MODE."""

from datetime import date, timedelta

from app.memory.test_borrower import apply_test_borrower_slots, hardcoded_test_borrower
from app.engine.tracker import new_conversation_state


def test_hardcoded_test_borrower_pre_closure_fields():
    borrower = hardcoded_test_borrower()
    today = date.today()
    assert borrower.identity["name"] == "Rishabh"
    assert borrower.loan["customer_name"] == "Rishabh"
    assert borrower.loan["repay_amount"] == 2300
    assert borrower.loan["offer_amount"] == 2000
    assert borrower.loan["discount_amount"] == 300
    assert borrower.loan["loan_amount"] == 2020
    assert borrower.loan["loan_tenure"] == "4 months"
    assert borrower.loan["due_date"] == (today + timedelta(days=5)).isoformat()
    assert borrower.loan["disbursal_date"] == (today - timedelta(days=25)).isoformat()


def test_apply_test_borrower_slots_hydrates_nlg_fields():
    borrower = hardcoded_test_borrower()
    state = new_conversation_state("call-1", "salary_on_time", borrower.borrower_id)
    state = apply_test_borrower_slots(state, borrower)
    assert state.slots["customer_name"] == "Rishabh"
    assert state.slots["repay_amount"] == 2300
    assert state.slots["offer_amount"] == 2000
    assert state.slots["discount_amount"] == 300
    assert state.slots["due_date"] == borrower.loan["due_date"]
