"""Tests for RealtimeTranscriber (level B).

We don't open a real WebSocket here — the contract under test is:
  - END_OF_STREAM in the queue gets translated into a final ``commit: true``
    JSON frame on the wire.
  - audio chunks become base64-wrapped input_audio_chunk messages.
  - partial_transcript events go to the on_partial callback (and a raising
    callback doesn't break the session).
  - committed_transcript ends the session and returns its text.
  - fatal events / closed sockets raise RealtimeError.

We mock the websocket connection by patching ``websockets.connect`` to
return an async context manager that yields a fake socket. The fake socket
is a stateful object that records ``send()`` calls and yields canned
events on iteration.
"""
import asyncio
import base64
import json
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.elevenlabs_ws import END_OF_STREAM, RealtimeError, RealtimeTranscriber


class FakeWebSocket:
    """Minimal stand-in for the websockets ClientConnection used in tests."""

    def __init__(self, scripted_events):
        # Each event: a JSON-serialisable dict OR an instance of
        # ``websockets.exceptions.ConnectionClosed`` (will be raised on read).
        self._events = list(scripted_events)
        self.sent = []
        self._closed = False
        self._reader_done = asyncio.Event()

    async def send(self, payload):
        if self._closed:
            from websockets.exceptions import ConnectionClosedError
            raise ConnectionClosedError(rcvd=None, sent=None)
        self.sent.append(payload)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        while self._events:
            ev = self._events.pop(0)
            if isinstance(ev, BaseException):
                raise ev
            # Mimic a small delay so the sender has time to put chunks first.
            await asyncio.sleep(0)
            yield json.dumps(ev)
        # Iteration ends: the WS would normally stay open; we mark a flag.
        self._reader_done.set()


def fake_connect_factory(fake_ws):
    """Return something callable like ``websockets.connect(...)``."""
    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield fake_ws
    return _ctx


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class RealtimeTranscriberTests(unittest.TestCase):
    def setUp(self):
        self.rt = RealtimeTranscriber(api_key="test-key")

    def test_no_api_key_raises_non_retryable(self):
        rt = RealtimeTranscriber(api_key="")
        with self.assertRaises(RealtimeError) as ctx:
            asyncio.new_event_loop().run_until_complete(
                rt.transcribe_stream(asyncio.Queue(), sample_rate=16000)
            )
        self.assertFalse(ctx.exception.retryable)

    def test_small_chunks_coalesce_into_single_flush_then_commit(self):
        """Small inputs (well under the 100 ms / 3200-byte threshold at
        16 kHz PCM16) accumulate in the sender and flush together when
        END_OF_STREAM arrives. Two queued chunks → ONE audio flush + ONE
        commit = 2 sends total, NOT 3 like the unbatched implementation.
        """
        events = [
            {"message_type": "session_started", "session_id": "x"},
            {"message_type": "committed_transcript", "text": "Hello."},
        ]
        ws = FakeWebSocket(events)

        async def scenario():
            q = asyncio.Queue()
            await q.put(b"\x01\x02\x03\x04")
            await q.put(b"\x05\x06")
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                return await self.rt.transcribe_stream(q, sample_rate=16000)

        result = run(scenario())
        self.assertEqual(result, "Hello.")
        self.assertEqual(len(ws.sent), 2)
        m0 = json.loads(ws.sent[0])
        self.assertEqual(m0["message_type"], "input_audio_chunk")
        # Both queued chunks must be in the single flush — leaving any
        # behind would silently truncate audio on the wire.
        self.assertEqual(
            base64.b64decode(m0["audio_base_64"]),
            b"\x01\x02\x03\x04\x05\x06",
        )
        self.assertFalse(m0["commit"])
        m_last = json.loads(ws.sent[-1])
        self.assertTrue(m_last["commit"])
        self.assertEqual(m_last["audio_base_64"], "")

    def test_large_input_flushes_at_batch_threshold(self):
        """Once the buffer crosses ~100 ms of PCM16 audio, the sender
        flushes it without waiting for END_OF_STREAM — this is what keeps
        a long clip from backing up in the queue and getting tail-truncated
        by the server.
        """
        events = [
            {"message_type": "session_started"},
            {"message_type": "committed_transcript", "text": "Big."},
        ]
        ws = FakeWebSocket(events)
        # 100 ms at 16 kHz PCM16 = 3200 bytes. Send 4000 bytes in one go
        # so we cross the threshold immediately on the first chunk.
        big_chunk = b"\xab" * 4000

        async def scenario():
            q = asyncio.Queue()
            await q.put(big_chunk)
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                return await self.rt.transcribe_stream(q, sample_rate=16000)

        result = run(scenario())
        self.assertEqual(result, "Big.")
        # 1 mid-stream flush (threshold crossed) + 1 commit = 2 sends.
        # No "leftover" send after commit.
        self.assertEqual(len(ws.sent), 2)
        m0 = json.loads(ws.sent[0])
        self.assertEqual(base64.b64decode(m0["audio_base_64"]), big_chunk)
        self.assertFalse(m0["commit"])
        m_last = json.loads(ws.sent[-1])
        self.assertTrue(m_last["commit"])
        self.assertEqual(m_last["audio_base_64"], "")

    def test_partial_callback_receives_text_and_continues_on_exception(self):
        events = [
            {"message_type": "session_started", "session_id": "y"},
            {"message_type": "partial_transcript", "text": "Привет"},
            {"message_type": "partial_transcript", "text": "Привет мир"},
            {"message_type": "committed_transcript", "text": "Привет, мир!"},
        ]
        ws = FakeWebSocket(events)
        seen = []

        def on_partial(txt):
            seen.append(txt)
            if len(seen) == 1:
                raise RuntimeError("simulated overlay torn down")

        async def scenario():
            q = asyncio.Queue()
            await q.put(b"\x00" * 32)
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                return await self.rt.transcribe_stream(
                    q, sample_rate=16000, on_partial=on_partial
                )

        result = run(scenario())
        self.assertEqual(result, "Привет, мир!")
        self.assertEqual(seen, ["Привет", "Привет мир"])

    def test_fatal_event_raises_realtime_error(self):
        events = [
            {"message_type": "session_started"},
            {"message_type": "auth_error", "detail": "bad key"},
        ]
        ws = FakeWebSocket(events)

        async def scenario():
            q = asyncio.Queue()
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                await self.rt.transcribe_stream(q, sample_rate=16000)

        with self.assertRaises(RealtimeError) as ctx:
            run(scenario())
        self.assertFalse(ctx.exception.retryable)  # auth_error is permanent

    def test_quota_exceeded_is_retryable(self):
        events = [
            {"message_type": "session_started"},
            {"message_type": "quota_exceeded", "detail": "out of credits"},
        ]
        ws = FakeWebSocket(events)

        async def scenario():
            q = asyncio.Queue()
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                await self.rt.transcribe_stream(q, sample_rate=16000)

        with self.assertRaises(RealtimeError) as ctx:
            run(scenario())
        # Caller can queue for batch retry on quota errors — billing may
        # reset by the time the retry fires.
        self.assertTrue(ctx.exception.retryable)

    def test_closed_before_commit_raises_retryable(self):
        from websockets.exceptions import ConnectionClosedError
        events = [
            {"message_type": "session_started"},
            ConnectionClosedError(rcvd=None, sent=None),
        ]
        ws = FakeWebSocket(events)

        async def scenario():
            q = asyncio.Queue()
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                await self.rt.transcribe_stream(q, sample_rate=16000)

        with self.assertRaises(RealtimeError) as ctx:
            run(scenario())
        self.assertTrue(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()
