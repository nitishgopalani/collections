from app.memory.pii import mask_pii, mask_pii_in_value


def test_mask_phone_in_logs():
    assert "[PHONE]" in mask_pii("contact 9876543210 for help")


def test_mask_pii_in_nested_structure():
    masked = mask_pii_in_value(
        {
            "phone": "9876543210",
            "nested": {"note": "no sensitive digits here"},
        }
    )
    assert masked["phone"] == "[PHONE]"
    assert masked["nested"]["note"] == "no sensitive digits here"
