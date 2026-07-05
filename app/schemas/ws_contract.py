"""EB-6 WebSocket contract — text-only bridge between Go telephony and the brain."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class SessionStartMessage(BaseModel):
    type: Literal["session_start"] = "session_start"
    session_id: str = Field(min_length=1)
    borrower_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    pack_id: str = ""
    locale: str = "hi-IN"
    tenant_id: str | None = None
    # Phase C: the connector (asterisk-connector) already sends "client_id" on the
    # wire; it identifies the owning tenant. Optional + default so callers that
    # omit it still validate (backward compatible).
    client_id: str = ""
    borrower_context: dict[str, Any] = Field(default_factory=dict)


class TurnMessage(BaseModel):
    type: Literal["turn"] = "turn"
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    transcript: str = ""
    flow_class: str = "Default"


class CancelMessage(BaseModel):
    type: Literal["cancel"] = "cancel"
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)


class SessionEndMessage(BaseModel):
    type: Literal["session_end"] = "session_end"
    session_id: str = Field(min_length=1)


class PlaybackDoneMessage(BaseModel):
    """Go → brain: a reply turn's audio finished playing to the caller.

    Sent when the go-server egresses the turn's last paced frame (AudioSocket)
    or receives the carrier mark echo. The brain uses it to sequence actions
    that must wait until the caller has HEARD a line (consult start after the
    hold announcement) and to arm the no-input reprompt timer.
    """

    type: Literal["playback_done"] = "playback_done"
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)


class SessionReadyMessage(BaseModel):
    """Ack after session_start — resolved borrower + ASR locale for go-server."""

    type: Literal["session_ready"] = "session_ready"
    session_id: str = Field(min_length=1)
    borrower_id: str = ""
    borrower_name: str = ""
    asr_language: str = "hi-IN"


GoInboundMessage = Annotated[
    SessionStartMessage | TurnMessage | CancelMessage | SessionEndMessage | PlaybackDoneMessage,
    Field(discriminator="type"),
]


class ChunkMessage(BaseModel):
    type: Literal["chunk"] = "chunk"
    turn_id: str
    seq: int = Field(ge=0)
    text: str


class FlowClassMessage(BaseModel):
    type: Literal["flow_class"] = "flow_class"
    turn_id: str
    next: Literal["YesNo", "Default", "SpelledInput"]


class DoneMessage(BaseModel):
    type: Literal["done"] = "done"
    turn_id: str
    disposition: str | None = None
    end_call: bool = False
    # Hangup grace: the go-server waits this long AFTER the turn's playback
    # completes before ending the call (e.g. 3s after the no-input
    # "disconnecting this call" line). 0 = hang up right after playback.
    end_call_delay_ms: int = 0
    audit_id: str | None = None


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    turn_id: str
    fallback_text: str


BrainOutboundMessage = Annotated[
    SessionReadyMessage | ChunkMessage | FlowClassMessage | DoneMessage | ErrorMessage,
    Field(discriminator="type"),
]


def parse_go_inbound(payload: dict[str, Any]) -> GoInboundMessage:
    """Validate an inbound Go → brain message."""
    msg_type = payload.get("type")
    if msg_type == "session_start":
        return SessionStartMessage.model_validate(payload)
    if msg_type == "turn":
        return TurnMessage.model_validate(payload)
    if msg_type == "cancel":
        return CancelMessage.model_validate(payload)
    if msg_type == "session_end":
        return SessionEndMessage.model_validate(payload)
    if msg_type == "playback_done":
        return PlaybackDoneMessage.model_validate(payload)
    raise ValueError(f"unknown inbound ws message type: {msg_type!r}")
