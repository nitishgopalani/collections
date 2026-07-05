"""Unit tests for VertexLLMClient.stream() (SDK iterator faked, no network)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.clients.llm_vertex import VertexLLMClient


class _Piece:
    def __init__(self, text: str) -> None:
        self.text = text


class _NoTextPiece:
    """Streamed responses without text parts raise on .text (SDK behaviour)."""

    @property
    def text(self) -> str:
        raise ValueError("no text part")


class FakeSDKStream:
    """Stand-in for the blocking vertexai streaming iterator."""

    def __init__(self, pieces: list[Any], block_after: int | None = None) -> None:
        self._pieces = list(pieces)
        self._idx = 0
        self.block_after = block_after
        self.release = threading.Event()
        self.closed = False

    def __iter__(self) -> FakeSDKStream:
        return self

    def __next__(self) -> Any:
        if self.block_after is not None and self._idx == self.block_after:
            # Simulate the SDK blocking on the network for the next piece.
            self.release.wait(timeout=5.0)
        if self._idx >= len(self._pieces):
            raise StopIteration
        piece = self._pieces[self._idx]
        self._idx += 1
        return piece

    def close(self) -> None:
        self.closed = True
        self.release.set()


def _live_client(monkeypatch, fake: FakeSDKStream) -> VertexLLMClient:
    client = VertexLLMClient(timeout=5.0)
    monkeypatch.setattr(client._settings, "llm_stub", False)
    monkeypatch.setattr(client, "_start_stream_sync", lambda system, user: fake)
    return client


async def test_stream_yields_token_deltas_in_order(monkeypatch):
    fake = FakeSDKStream([_Piece("Namaste"), _NoTextPiece(), _Piece(" ji."), _Piece("")])
    client = _live_client(monkeypatch, fake)
    out = [tok async for tok in client.stream("sys", "user")]
    # Empty/no-text pieces are skipped; order preserved.
    assert out == ["Namaste", " ji."]
    for _ in range(100):
        if fake.closed:
            break
        await asyncio.sleep(0.02)
    assert fake.closed, "SDK stream must be closed after normal exhaustion"


async def test_stream_stub_mode_yields_nothing():
    client = VertexLLMClient(timeout=1.0)
    if not client.is_stub:  # pragma: no cover - CI always runs stubbed
        return
    assert [tok async for tok in client.stream("sys", "user")] == []


async def test_aborting_stream_closes_sdk_stream(monkeypatch):
    """Barge-in path: consumer aborts mid-stream -> SDK stream is closed, the
    worker thread exits, nothing leaks."""
    fake = FakeSDKStream([_Piece("Pehla vakya. "), _Piece("kabhi nahi aayega")], block_after=1)
    client = _live_client(monkeypatch, fake)

    agen = client.stream("sys", "user")
    first = await agen.__anext__()
    assert first == "Pehla vakya. "
    await agen.aclose()  # what CancelledError propagation does in the agent
    fake.release.set()  # let the fake's blocking read return

    for _ in range(100):
        if fake.closed:
            break
        await asyncio.sleep(0.02)
    assert fake.closed, "aborted stream must close the underlying SDK stream"


async def test_stream_surfaces_sdk_errors(monkeypatch):
    class BoomStream:
        def __iter__(self):
            return self

        def __next__(self):
            raise RuntimeError("vertex exploded")

        def close(self):
            pass

    client = VertexLLMClient(timeout=5.0)
    monkeypatch.setattr(client._settings, "llm_stub", False)
    monkeypatch.setattr(client, "_start_stream_sync", lambda s, u: BoomStream())
    try:
        async for _ in client.stream("sys", "user"):
            raise AssertionError("no tokens expected")
    except RuntimeError as exc:
        assert "vertex exploded" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
