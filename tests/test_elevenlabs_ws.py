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

    def test_commit_throttled_with_committed_returns_text(self):
        """commit_throttled AFTER segments were committed (long clip whose VAD
        already finalized everything → 0.00s uncommitted) is NOT fatal: the
        transcript exists, so we return it as success instead of throwing it
        away and paying for a second batch transcription of the same audio.
        """
        events = [
            {"message_type": "session_started"},
            {"message_type": "committed_transcript",
             "text": "Длинная диктовка целиком."},
            {"message_type": "commit_throttled",
             "detail": "only 0.00s of uncommitted audio"},
        ]
        ws = FakeWebSocket(events)

        async def scenario():
            q = asyncio.Queue()
            await q.put(b"\x00" * 64)
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws._TAIL_IDLE_SEC", 0.05), \
                 patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                return await self.rt.transcribe_stream(q, sample_rate=16000)

        result = run(scenario())
        self.assertEqual(result, "Длинная диктовка целиком.")

    def test_commit_throttled_without_committed_raises_throttled_empty(self):
        """commit_throttled with NOTHING committed = a clip with no speech.
        Must raise RealtimeError(throttled_empty=True) so the caller discards a
        sub-0.3s clip instead of poison-queueing a guaranteed-to-fail WAV for
        batch retry (the audio_too_short pending loop, roadmap A2/A3).
        """
        events = [
            {"message_type": "session_started"},
            {"message_type": "commit_throttled",
             "detail": "only 0.00s of uncommitted audio"},
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
        self.assertTrue(ctx.exception.throttled_empty)
        # retryable=True but the caller short-circuits on throttled_empty for
        # tiny clips; for a non-tiny clip it would still fall back to batch.
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


class RealtimeQueryParamTests(unittest.TestCase):
    """B1 no_verbatim + B2 keyterms are encoded into the WS connection URL."""

    def setUp(self):
        self.rt = RealtimeTranscriber(api_key="test-key")

    def _capture_url(self, **stream_kwargs):
        events = [
            {"message_type": "session_started"},
            {"message_type": "committed_transcript", "text": "ok"},
        ]
        ws = FakeWebSocket(events)
        captured = {}

        def fake_connect(*a, **kw):
            captured["url"] = a[0] if a else kw.get("uri") or kw.get("url")
            return fake_connect_factory(ws)()

        async def scenario():
            q = asyncio.Queue()
            await q.put(b"\x00" * 32)
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws._TAIL_IDLE_SEC", 0.05), \
                 patch("core.elevenlabs_ws.websockets.connect", fake_connect):
                await self.rt.transcribe_stream(q, sample_rate=16000, **stream_kwargs)

        run(scenario())
        return captured["url"]

    def test_defaults_only_include_timestamps(self):
        # C1 turns include_timestamps on by default; nothing else is requested.
        url = self._capture_url()
        self.assertIn("include_timestamps=true", url)
        self.assertNotIn("language_code=", url)
        self.assertNotIn("no_verbatim=", url)
        self.assertNotIn("keyterms=", url)
        self.assertNotIn("commit_strategy=", url)

    def test_no_verbatim_in_query(self):
        url = self._capture_url(no_verbatim=True)
        self.assertIn("no_verbatim=true", url)

    def test_keyterms_repeated_params_and_clamped(self):
        url = self._capture_url(
            language="ru",
            keyterms=["VoiceDrop", "x" * 40],  # second term exceeds 20-char cap
        )
        self.assertIn("language_code=ru", url)
        self.assertEqual(url.count("keyterms="), 2)
        self.assertIn("keyterms=VoiceDrop", url)
        # The over-long term is truncated to 20 chars in the URL.
        self.assertIn("keyterms=" + "x" * 20 + "&", url + "&")

    def test_commit_strategy_manual_by_default(self):
        url = self._capture_url()
        self.assertNotIn("commit_strategy=", url)

    def test_commit_strategy_vad_with_threshold(self):  # B3
        url = self._capture_url(commit_strategy="vad",
                                vad_silence_threshold_secs=0.8)
        self.assertIn("commit_strategy=vad", url)
        self.assertIn("vad_silence_threshold_secs=0.8", url)


class _HangingWS:
    """Like FakeWebSocket but stays 'open' (awaits forever) after the scripted
    events, so the drain loop must end on its own logic (C1 coverage / idle)
    rather than the reader iterator simply running out."""

    def __init__(self, events):
        self._events = list(events)
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for ev in self._events:
            await asyncio.sleep(0)
            yield json.dumps(ev)
        await asyncio.sleep(3600)  # server hasn't closed; reader stays alive


class RealtimeC1CoverageTests(unittest.TestCase):
    """C1: finish the drain as soon as committed timestamps cover the audio."""

    def setUp(self):
        self.rt = RealtimeTranscriber(api_key="test-key")

    def _run(self, events, audio_bytes, idle_sec, overall_timeout):
        ws = _HangingWS(events)

        async def scenario():
            q = asyncio.Queue()
            await q.put(audio_bytes)
            await q.put(END_OF_STREAM)
            with patch("core.elevenlabs_ws._TAIL_IDLE_SEC", idle_sec), \
                 patch("core.elevenlabs_ws.websockets.connect",
                       lambda *a, **kw: fake_connect_factory(ws)()):
                # If C1 fails to break early, the hanging socket + large idle
                # would stall until overall_timeout → a clear test failure.
                return await asyncio.wait_for(
                    self.rt.transcribe_stream(q, sample_rate=16000),
                    timeout=overall_timeout,
                )

        return run(scenario())

    def test_finishes_early_when_timestamps_cover_audio(self):
        # 1 s of PCM16 @16k = 32000 bytes; committed words end at 1.0 s.
        audio = b"\x00" * 32000
        events = [
            {"message_type": "session_started"},
            {"message_type": "committed_transcript_with_timestamps",
             "words": [{"text": "привет", "start": 0.0, "end": 0.5},
                       {"text": "мир", "start": 0.5, "end": 1.0}]},
        ]
        # idle is huge: if coverage didn't break, wait_for would time out.
        result = self._run(events, audio, idle_sec=30.0, overall_timeout=5.0)
        self.assertEqual(result, "привет мир")

    def test_falls_back_to_idle_when_not_covered(self):
        # 2 s of audio but words only reach 0.4 s → no coverage; the idle-drain
        # fallback (small here) must still finish and return what we have.
        audio = b"\x00" * 64000  # 2 s
        events = [
            {"message_type": "session_started"},
            {"message_type": "committed_transcript_with_timestamps",
             "words": [{"text": "край", "start": 0.0, "end": 0.4}]},
        ]
        result = self._run(events, audio, idle_sec=0.1, overall_timeout=5.0)
        self.assertEqual(result, "край")


if __name__ == "__main__":
    unittest.main()
