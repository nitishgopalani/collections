import pytest

from app.engine.conditions import evaluate_condition


def test_equals_boolean_true():
    assert evaluate_condition("ptp_allowed == true", {"ptp_allowed": True})


def test_not_equals():
    assert evaluate_condition("ptp_allowed != true", {"ptp_allowed": False})


def test_numeric_compare():
    assert evaluate_condition("dpd >= 30", {"dpd": 45})


def test_in_operator_list():
    assert evaluate_condition('bucket in ["B1", "B2"]', {"bucket": "B1"})


def test_unsupported_expression_raises():
    with pytest.raises(ValueError):
        evaluate_condition("import os", {})
