"""Phase B: brain-side orchestrator client + conferencing/transfer actions.

The client (``app.clients.orchestrator``) is exercised by mocking its HTTP
transport (``_post``); the new actions are exercised by mocking the client
functions and asserting each action calls the right endpoint with the right
payload.
"""

import pytest

import app.clients.orchestrator as orch
from app.clients.tools_sim import FakeToolClient
from app.engine.actions import make_async_action_runner
from app.engine.tracker import new_conversation_state


# ---------------------------------------------------------------------------
# Client payload shapes (transport mocked)
# ---------------------------------------------------------------------------


def _capture_post(monkeypatch):
    captured: dict = {}

    def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"bridge_id": "br-1", "channel_ids": ["c1", "c2"], "status": "ok"}

    monkeypatch.setattr(orch, "_post", fake_post)
    return captured


def test_originate_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.originate(to="919910779326", caller_id="1800", context="from-telco")
    assert captured["path"] == "/v1/originate"
    assert captured["payload"] == {
        "to": "919910779326",
        "caller_id": "1800",
        "context": "from-telco",
    }


def test_post_sends_bearer_when_service_key_set(monkeypatch):
    captured: dict = {}

    def fake_post(path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"status": "ok"}

    headers_captured: dict = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            headers_captured["headers"] = headers or {}
            fake_post(url.split("8095")[-1], json or {})
            resp = type("R", (), {})()
            resp.content = b'{"status":"ok"}'
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"status": "ok"}
            return resp

    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8095")
    monkeypatch.setenv("ORCHESTRATOR_SERVICE_KEY", "sk_test_brain_service_key")
    monkeypatch.setattr(orch.httpx, "Client", FakeClient)
    orch.originate(to="919910779326", caller_id="1800")
    assert headers_captured["headers"]["Authorization"] == "Bearer sk_test_brain_service_key"


def test_transfer_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.transfer(existing_channel_id="c1", to="999", caller_id="1", ring_budget_s=25.0)
    assert captured["path"] == "/v1/transfer"
    assert captured["payload"] == {
        "existing_channel_id": "c1",
        "to": "999",
        "caller_id": "1",
        "ring_budget_s": 25.0,
    }


def test_warm_transfer_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.warm_transfer(session_uuid="uuid-1", to="999", caller_id="1", ring_budget_s=30.0)
    assert captured["path"] == "/v1/transfer"
    assert captured["payload"] == {
        "session_uuid": "uuid-1",
        "to": "999",
        "caller_id": "1",
        "ring_budget_s": 30.0,
    }


def test_transfer_complete_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.transfer_complete(transfer_id="transfer-9")
    assert captured["path"] == "/v1/transfer/complete"
    assert captured["payload"] == {"transfer_id": "transfer-9", "id": "transfer-9"}


def test_transfer_cancel_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.transfer_cancel(transfer_id="transfer-9")
    assert captured["path"] == "/v1/transfer/cancel"
    assert captured["payload"] == {"transfer_id": "transfer-9", "id": "transfer-9"}


def test_conference_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.conference(channel_ids=["a", "b", "c"])
    assert captured["path"] == "/v1/conference"
    assert captured["payload"] == {"channel_ids": ["a", "b", "c"]}


def test_participant_payload(monkeypatch):
    captured = _capture_post(monkeypatch)
    orch.participant(bridge_id="br", channel_id="c9", action="remove")
    assert captured["path"] == "/v1/participant"
    assert captured["payload"] == {
        "bridge_id": "br",
        "channel_id": "c9",
        "action": "remove",
    }


def test_base_url_required(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_BASE_URL", raising=False)
    with pytest.raises(orch.OrchestratorError):
        orch.originate(to="1")


# ---------------------------------------------------------------------------
# Actions (client mocked)
# ---------------------------------------------------------------------------


class RecordingOrchestrator:
    """Records calls and returns canned responses in place of the HTTP client."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def warm_transfer(self, **kwargs):
        self.calls.append(("warm_transfer", kwargs))
        return {
            "transfer_id": "transfer-1",
            "bridge_id": "br-1",
            "channel_ids": ["call-1", "transfer-1-leg"],
            "agent_channel_id": "transfer-1-leg",
            "status": "originating",
        }

    def conference(self, **kwargs):
        self.calls.append(("conference", kwargs))
        return {"bridge_id": "br-9", "channel_ids": kwargs.get("channel_ids"), "status": "conferenced"}

    def participant(self, **kwargs):
        self.calls.append(("participant", kwargs))
        return {"status": "ok"}


def _patch_orchestrator(monkeypatch) -> RecordingOrchestrator:
    rec = RecordingOrchestrator()
    monkeypatch.setattr(orch, "warm_transfer", rec.warm_transfer)
    monkeypatch.setattr(orch, "conference", rec.conference)
    monkeypatch.setattr(orch, "participant", rec.participant)
    return rec


def _state(**slots):
    state = new_conversation_state("call-1", "salary_on_time", "b-1")
    state.slots.update(slots)
    return state


@pytest.mark.asyncio
async def test_warm_transfer_calls_transfer_endpoint(monkeypatch):
    rec = _patch_orchestrator(monkeypatch)
    runner = make_async_action_runner(FakeToolClient())
    result = await runner("warm_transfer", _state(transfer_to="9910779326"))

    # The session id (call-1) is sent as session_uuid: the orchestrator's
    # inbound registry resolves it, exactly like consult_start.
    assert rec.calls == [
        (
            "warm_transfer",
            {
                "session_uuid": "call-1",
                "to": "9910779326",
                "caller_id": "",
                "ring_budget_s": pytest.approx(30.0),
            },
        )
    ]
    slots = result.slots
    assert slots["warm_transfer_requested"] is True
    assert slots["transfer_id"] == "transfer-1"
    assert slots["conference_bridge_id"] == "br-1"
    assert slots["transfer_channel_ids"] == ["call-1", "transfer-1-leg"]


@pytest.mark.asyncio
async def test_warm_transfer_without_target_records_error(monkeypatch):
    rec = _patch_orchestrator(monkeypatch)
    runner = make_async_action_runner(FakeToolClient())
    result = await runner("warm_transfer", _state())

    assert rec.calls == []  # no network call attempted
    assert "orchestrator_error" in result.slots


@pytest.mark.asyncio
async def test_start_conference_includes_self_leg(monkeypatch):
    rec = _patch_orchestrator(monkeypatch)
    runner = make_async_action_runner(FakeToolClient())
    result = await runner(
        "start_conference", _state(conference_channel_ids=["manufacturer-1"])
    )

    assert rec.calls == [
        ("conference", {"channel_ids": ["call-1", "manufacturer-1"]})
    ]
    assert result.slots["conference_started"] is True
    assert result.slots["conference_bridge_id"] == "br-9"


@pytest.mark.asyncio
async def test_add_participant(monkeypatch):
    rec = _patch_orchestrator(monkeypatch)
    runner = make_async_action_runner(FakeToolClient())
    result = await runner(
        "add_participant",
        _state(conference_bridge_id="br-9", participant_channel_id="p-1"),
    )
    assert rec.calls == [
        ("participant", {"bridge_id": "br-9", "channel_id": "p-1", "action": "add"})
    ]
    assert result.slots["last_participant_added"] == "p-1"


@pytest.mark.asyncio
async def test_drop_participant(monkeypatch):
    rec = _patch_orchestrator(monkeypatch)
    runner = make_async_action_runner(FakeToolClient())
    result = await runner(
        "drop_participant",
        _state(conference_bridge_id="br-9", participant_channel_id="p-2"),
    )
    assert rec.calls == [
        ("participant", {"bridge_id": "br-9", "channel_id": "p-2", "action": "remove"})
    ]
    assert result.slots["last_participant_dropped"] == "p-2"


@pytest.mark.asyncio
async def test_drop_self_removes_bot_leg(monkeypatch):
    rec = _patch_orchestrator(monkeypatch)
    runner = make_async_action_runner(FakeToolClient())
    result = await runner("drop_self", _state(conference_bridge_id="br-9"))

    assert rec.calls == [
        ("participant", {"bridge_id": "br-9", "channel_id": "call-1", "action": "remove"})
    ]
    assert result.slots["self_dropped"] is True

