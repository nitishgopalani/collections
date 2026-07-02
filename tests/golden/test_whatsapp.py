"""Live WhatsApp sender (campaign creator) — payload contract + stub/live modes."""

from types import SimpleNamespace

import pytest

from app.clients.whatsapp import WhatsappResult, _build_payload, send_whatsapp


def test_build_payload_matches_campaign_contract():
    settings = SimpleNamespace(
        whatsapp_campaign_name="emi_campaign",
        whatsapp_template_name="mcp_test_85115",
    )
    payload = _build_payload(phone="+919810587857", name="Rishabh", settings=settings)
    assert payload["campaign_name"] == "emi_campaign"
    assert payload["template_name"] == "mcp_test_85115"
    # Phone is digits-with-country-code (the + is stripped); BODY_1 is the name.
    assert payload["audience_rows"] == [{"Phone": "919810587857", "BODY_1": "Rishabh"}]


@pytest.mark.asyncio
async def test_stub_returns_pending():
    # conftest forces WHATSAPP_MODE=stub / empty endpoint.
    result = await send_whatsapp(phone="+919810587857", name="Rishabh")
    assert isinstance(result, WhatsappResult)
    assert result.status == "pending"
    assert result.ok is True


def test_result_ok_semantics():
    assert WhatsappResult("pending").ok is True
    assert WhatsappResult("sent").ok is True
    assert WhatsappResult("failed").ok is False
