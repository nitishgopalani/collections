"""Phone normalization tests."""

import pytest

from app.util.phone import canonical_phone, phone_lookup_variants, phone_match_suffix


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+919810587857", "+919810587857"),
        ("919810587857", "+919810587857"),
        ("9810587857", "+919810587857"),
        ("+91 98105 87857", "+919810587857"),
    ],
)
def test_canonical_phone_indian_variants(raw: str, expected: str) -> None:
    assert canonical_phone(raw) == expected


def test_phone_match_suffix_same_for_all_variants() -> None:
    assert phone_match_suffix("+919810587857") == "9810587857"
    assert phone_match_suffix("919810587857") == "9810587857"
    assert phone_match_suffix("9810587857") == "9810587857"


def test_phone_lookup_variants_cover_carrier_formats() -> None:
    variants = phone_lookup_variants("9810587857")
    assert "+919810587857" in variants
    assert "919810587857" in variants
    assert "9810587857" in variants
