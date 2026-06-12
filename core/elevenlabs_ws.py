"""ElevenLabs Scribe v2 Realtime — WebSocket client.

Async client. The caller pushes PCM16 16-kHz mono chunks (raw bytes) into
an asyncio.Queue, and we stream them to the realtime STT endpoint. On
release of the hotkey, the caller puts a sentinel (None) into the queue;
we send the last chunk with `commit: true` and wait for the final
`committed_transcript` event before returning.

Failure model: ANY failure (auth, connect, network drop, server-side
error, timeout, bad audio) raises RealtimeError. The caller is expected
to handle the fallback (save accumulated WAV to pending/, batch retry).
We never silently fall back inside the WS path — the caller is the only
place that knows what the fallback wants.

Partial transcripts are pushed via on_partial callback. Callback
exceptions are swallowed so a destroyed Tk overlay never breaks the
transcription path.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Callable, Optional
from urllib.parse import urlencode

# Realtime keyterm limits (ElevenLabs): max 50 terms, 20 chars each (vs batch
# 1000×50). We clamp defensively so a too-large list can't be rejected.
_RT_KEYTERMS_MAX = 50
_RT_KEYTERM_MAXLEN = 20

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatus,
    WebSocketException,
)

WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"

# After commit we still want a final committed_transcript event — give the
# server a reasonable window before giving up. PoC showed ~300-400 ms; long
# clips with batched send can take longer to fully drain.
_COMMIT_GRACE_SEC = 12.0
# Connection establishment timeout. Cold TLS to api.elevenlabs.io is well
# under 1s in healthy paths.
_CONNECT_TIMEOUT_SEC = 6.0
# Coalesce small PCM blocks from the PortAudio callback (one per ~23 ms
# at 44.1 kHz / 1024 blocksize) into ~100 ms WS messages. Without this,
# on a 54-second clip the sender was emitting ~43 msg/sec which exceeded
# the server's effective ingest rate — the queue backed up by ~10 s and
# the trailing audio got truncated by server-side finalization. PoC used
# 120 ms chunks and was clean; we land in the same range here.
_SEND_BATCH_MS = 100
# After each committed_transcript, wait at MOST this long for ANOTHER one
# before declaring the session done. The timer resets on every committed
# event, so a server that emits N segments spread out over the session
# (Scribe v2 VAD-segments long audio) is fully drained regardless of N.
# Observed gap between segments on a 96 s clip = sub-second; 2.5 s buys
# generous headroom.
_TAIL_IDLE_SEC = 2.5
# Hard cap so a server that NEVER stops emitting committeds (would be a
# bug, but defence in depth) can't keep us in the drain loop forever.
_TAIL_MAX_TOTAL_SEC = 30.0
# C1: once committed word timestamps cover the sent audio to within this slack,
# finish immediately instead of waiting out _TAIL_IDLE_SEC — saves ~1.5-2 s per
# clip. Kept SMALL so we never finish before a real trailing word commits
# (segments commit atomically with all their words, so the only risk is a tiny
# slack window; 300 ms covers push-to-talk release reaction without truncating).
_TAIL_COVERAGE_SLACK_MS = 300
# Sentinel that the caller drops into the chunk_queue to mean "end of audio,
# send commit and finalize". Public so callers can import it.
END_OF_STREAM = object()

# Inbound event types that are fatal — abandon WS, raise RealtimeError so the
# caller can fall back to batch + pending queue.
# NOTE: commit_throttled is deliberately NOT here — it is handled specially
# (see reader()). The server emits it when we send commit:true but there is too
# little *uncommitted* audio to commit. On a long clip the VAD has usually
# already committed everything (uncommitted ~0.00s) so the transcript EXISTS and
# we must return it as success instead of throwing it away and paying for a
# second batch run. On a sub-0.3s clip nothing was committed and there is simply
# no speech — that must be discarded, not poison-queued for batch retry.
_FATAL_EVENTS = frozenset({
    "error",
    "auth_error",
    "quota_exceeded",
    "rate_limited",
    "queue_overflow",
    "resource_exhausted",
    "session_time_limit_exceeded",
    "input_error",
    "chunk_size_exceeded",
    "insufficient_audio_activity",
    "transcriber_error",
})


def _max_word_end_ms(evt: dict) -> int:
    """Largest word END timestamp (ms) in a committed event, or 0 if none.

    ElevenLabs word timestamps are floats in SECONDS. Tolerant of a few key
    spellings; ignores anything non-numeric so a schema tweak can't crash us."""
    best = 0
    for w in (evt.get("words") or []):
        if not isinstance(w, dict):
            continue
        for k in ("end", "end_time", "end_sec", "endTime", "end_s"):
            v = w.get(k)
            if isinstance(v, (int, float)):
                best = max(best, int(float(v) * 1000))
                break
    return best


class RealtimeError(Exception):
    """Realtime transcription failed. ``offline`` mirrors the same flag in
    TranscriptionError so the caller can route fallback the same way.

    ``retryable`` means the failure isn't a permanent input/auth error and
    the caller MAY queue the accumulated WAV for batch retry.
    """

    def __init__(self, message: str, offline: bool = False, retryable: bool = True,
                 throttled_empty: bool = False):
        super().__init__(message)
        self.offline = offline
        self.retryable = retryable
        # True for commit_throttled where the server had nothing committed and
        # nothing left to commit — i.e. the clip carried no transcribable speech.
        # The caller discards a sub-0.3s clip on this flag instead of queueing a
        # guaranteed-to-fail WAV for batch retry (the pending poison-loop source).
        self.throttled_empty = throttled_empty


class RealtimeTranscriber:
    """Stateless factory — one ``transcribe_stream`` per recording.

    The session is fully torn down at the end of ``transcribe_stream``; we
    do NOT keep a warm WS between presses (USB selective-suspend wakeup,
    re-auth windows, server-side per-connection limits all argue against
    a persistent connection on a push-to-talk app).
    """

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def transcribe_stream(
        self,
        chunk_queue: "asyncio.Queue[bytes | object]",
        sample_rate: int,
        on_partial: Optional[Callable[[str], None]] = None,
        language: Optional[str] = None,
        no_verbatim: bool = False,
        keyterms: Optional[list] = None,
        commit_strategy: str = "manual",
        vad_silence_threshold_secs: Optional[float] = None,
        include_timestamps: bool = True,
    ) -> str:
        """Open WS, drain ``chunk_queue``, return the final committed text.

        ``chunk_queue`` yields PCM16 little-endian mono ``bytes`` chunks.
        ``END_OF_STREAM`` (the module sentinel) signals the recorder has
        released and we should send `commit: true` on the next message
        (or by itself if no audio is pending).

        Raises ``RealtimeError`` on any failure — the caller falls back.
        """
        if not self.api_key:
            raise RealtimeError(
                "ElevenLabs API key is not configured", retryable=False
            )

        headers = {"xi-api-key": self.api_key}
        # Build the connection query string. language_code auto-detects when
        # absent (parity with batch). no_verbatim (B1) strips fillers server-side;
        # keyterms (B2) bias recognition — passed as repeated query params, each
        # clamped to the realtime limits so an over-long list can't be rejected.
        params: list = []
        if language:
            params.append(("language_code", language))
        if no_verbatim:
            params.append(("no_verbatim", "true"))
        for kt in (keyterms or [])[:_RT_KEYTERMS_MAX]:
            term = str(kt).strip()[:_RT_KEYTERM_MAXLEN].strip()
            if term:
                params.append(("keyterms", term))
        # C1: ask for word timestamps so the drain loop can finish as soon as
        # the committed transcript covers the audio (instead of waiting 2.5 s).
        if include_timestamps:
            params.append(("include_timestamps", "true"))
        # B3: opt-in server-side VAD commit (recommended for mic input — the
        # server segments on silence, removing the commit_throttled class). Off
        # by default; the manual final commit:true still flushes the tail.
        if str(commit_strategy).lower() == "vad":
            params.append(("commit_strategy", "vad"))
            if vad_silence_threshold_secs is not None:
                params.append(
                    ("vad_silence_threshold_secs", str(vad_silence_threshold_secs))
                )
        url = f"{WS_URL}?{urlencode(params)}" if params else WS_URL

        try:
            ws_ctx = websockets.connect(
                url,
                additional_headers=headers,
                max_size=8 * 1024 * 1024,
                open_timeout=_CONNECT_TIMEOUT_SEC,
                # Disable per-message compression — small PCM chunks don't
                # compress meaningfully and the negotiation adds latency.
                compression=None,
            )
        except Exception as e:  # pragma: no cover — defensive
            raise RealtimeError(f"WS connect failed: {e}", retryable=True) from e

        connect_t0 = time.time()
        try:
            async with ws_ctx as ws:
                logging.info(
                    "Realtime WS connected in %.0f ms",
                    (time.time() - connect_t0) * 1000,
                )
                return await self._drive_session(
                    ws=ws,
                    chunk_queue=chunk_queue,
                    sample_rate=sample_rate,
                    on_partial=on_partial,
                )
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise RealtimeError(f"WS timeout: {e}", retryable=True) from e
        except InvalidStatus as e:
            # 401/403 = our key has no realtime access. NOT retryable —
            # batch retry from pending/ would have the same auth result.
            status = getattr(e.response, "status_code", 0)
            non_retry = status in (401, 403)
            raise RealtimeError(
                f"WS handshake rejected ({status})", retryable=not non_retry
            ) from e
        except (ConnectionClosedError, ConnectionClosedOK, ConnectionClosed) as e:
            raise RealtimeError(f"WS connection closed: {e}", retryable=True) from e
        except WebSocketException as e:
            raise RealtimeError(f"WS protocol error: {e}", retryable=True) from e
        except OSError as e:
            # DNS / no route — same shape as TranscriptionError(offline=True).
            raise RealtimeError(
                f"Network error contacting ElevenLabs WS: {e}",
                offline=True, retryable=True,
            ) from e
        except RealtimeError:
            raise  # let our own already-typed errors out
        except Exception as e:  # pragma: no cover — defensive
            raise RealtimeError(
                f"Unexpected realtime failure: {type(e).__name__}: {e}",
                retryable=True,
            ) from e

    async def _drive_session(
        self,
        ws,
        chunk_queue: "asyncio.Queue[bytes | object]",
        sample_rate: int,
        on_partial: Optional[Callable[[str], None]],
    ) -> str:
        """Run the sender + reader concurrently inside an open WS."""
        committed_text: str = ""
        committed_done = asyncio.Event()
        # Set on EVERY committed_transcript (not just the first) so the
        # drain loop can adaptively wait through any number of segments.
        new_committed = asyncio.Event()
        fatal_err: Optional[RealtimeError] = None
        # Diagnostics so we can see in voicedrop.log whether truncated tail
        # was the server segmenting (multiple committed_transcript events)
        # or our sender lagging behind (low send rate, queue backed up).
        committed_count = 0
        partial_count = 0
        # C1: max END timestamp (ms) across all committed words so far. The
        # drain loop finishes early once this covers the sent audio.
        committed_end_ms = 0

        async def reader():
            nonlocal committed_text, fatal_err, committed_count, partial_count
            nonlocal committed_end_ms
            try:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        # Realtime endpoint is JSON-text — binary frames are
                        # unexpected, log & skip.
                        logging.debug("Realtime WS unexpected binary frame ignored")
                        continue
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        logging.debug("Realtime WS non-JSON frame ignored")
                        continue
                    et = evt.get("type") or evt.get("message_type") or ""
                    if et == "partial_transcript":
                        partial_count += 1
                        if on_partial is not None:
                            partial = evt.get("text") or ""
                            try:
                                on_partial(partial)
                            except Exception as cb_err:
                                logging.debug(
                                    "Realtime on_partial callback error: %s", cb_err
                                )
                    elif et in (
                        "committed_transcript",
                        "committed_transcript_with_timestamps",
                    ):
                        committed_count += 1
                        txt = evt.get("text") or ""
                        if not txt and "words" in evt:
                            txt = " ".join(
                                w.get("text", "") for w in evt.get("words") or []
                            )
                        if txt:
                            committed_text = (
                                txt if not committed_text
                                else f"{committed_text} {txt}"
                            )
                        # C1: track how far the committed transcript reaches.
                        committed_end_ms = max(committed_end_ms, _max_word_end_ms(evt))
                        # Per-segment INFO log so we can SEE in voicedrop.log
                        # how the server segmented this clip — and notice
                        # immediately if the count drifts vs the final text
                        # length on a "looks-truncated" complaint.
                        logging.info(
                            "Realtime committed segment #%d (len=%d): %s",
                            committed_count, len(txt), (txt[:80] + "…") if len(txt) > 80 else txt,
                        )
                        # Signal both: "first committed arrived" (lets the
                        # caller release its initial wait) and "we just got
                        # a fresh committed" (resets the idle-drain timer).
                        committed_done.set()
                        new_committed.set()
                    elif et == "session_started":
                        # Useful for debugging, but not actionable.
                        logging.debug(
                            "Realtime session_started: %s",
                            evt.get("session_id", "?"),
                        )
                    elif et == "commit_throttled":
                        # Server rejected our commit:true because too little
                        # *uncommitted* audio remained. NOT fatal:
                        #  - if segments were already committed (VAD finalized
                        #    the whole clip), the transcript exists — finalize
                        #    and return it as success (don't pay for batch again);
                        #  - if nothing was committed, the clip had no speech —
                        #    flag throttled_empty so the caller discards a tiny
                        #    clip instead of poison-queueing it for batch.
                        logging.info("Realtime commit_throttled: %s", evt)
                        if committed_count == 0:
                            fatal_err = RealtimeError(
                                f"commit_throttled with no committed audio: {evt}",
                                retryable=True, throttled_empty=True,
                            )
                        committed_done.set()
                        return
                    elif et in _FATAL_EVENTS:
                        non_retry = et in ("auth_error", "input_error")
                        fatal_err = RealtimeError(
                            f"Realtime fatal event: {et}: {evt}",
                            retryable=not non_retry,
                        )
                        committed_done.set()
                        return
                    else:
                        logging.debug(
                            "Realtime WS event ignored: type=%s keys=%s",
                            et, list(evt.keys()),
                        )
            except ConnectionClosed:
                # If we already got a committed_transcript, that's fine.
                if not committed_done.is_set():
                    fatal_err = RealtimeError(
                        "WS closed before committed_transcript",
                        retryable=True,
                    )
                    committed_done.set()

        # Pre-compute the size threshold for batched sends. PCM16 = 2 bytes
        # per sample; sample_rate samples per second.
        batch_threshold_bytes = int(sample_rate * 2 * _SEND_BATCH_MS / 1000)
        # Diagnostics: how much audio went out vs how many WS messages.
        # Logged once at session end so we can correlate truncation with
        # sender rate after the fact.
        sent_bytes = 0
        sent_messages = 0
        send_start_t = time.monotonic()

        async def _flush(buffer: bytearray):
            nonlocal sent_bytes, sent_messages
            if not buffer:
                return
            n = len(buffer)
            b64 = base64.b64encode(bytes(buffer)).decode("ascii")
            await ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": b64,
                "sample_rate": sample_rate,
                "commit": False,
            }))
            sent_bytes += n
            sent_messages += 1
            buffer.clear()

        async def sender():
            """Drain ``chunk_queue``, coalescing into ~100 ms WS messages.

            Sending one frame per PortAudio callback (~43/s) overruns the
            realtime endpoint's ingest pipeline on long clips and the tail
            audio gets dropped server-side. Buffering to ~100 ms holds the
            send rate at ~10 msg/s, in line with the published streaming
            cookbook cadence.
            """
            buffer = bytearray()
            try:
                while True:
                    item = await chunk_queue.get()
                    if item is END_OF_STREAM:
                        # Drain whatever's still in the buffer first so no
                        # audio is left behind by the coalescer.
                        await _flush(buffer)
                        # Empty commit frame finalizes the session.
                        await ws.send(json.dumps({
                            "message_type": "input_audio_chunk",
                            "audio_base_64": "",
                            "sample_rate": sample_rate,
                            "commit": True,
                        }))
                        return
                    if not isinstance(item, (bytes, bytearray)):
                        logging.warning(
                            "Realtime sender: unexpected queue item %s, skipping",
                            type(item).__name__,
                        )
                        continue
                    buffer.extend(item)
                    if len(buffer) >= batch_threshold_bytes:
                        await _flush(buffer)
            except ConnectionClosed:
                # Reader will notice and either pick up an already-arrived
                # committed_transcript or surface fatal_err.
                return

        reader_task = asyncio.create_task(reader(), name="rt-reader")
        sender_task = asyncio.create_task(sender(), name="rt-sender")

        try:
            await sender_task  # blocks until END_OF_STREAM or socket closed
            # Wait for the FIRST committed_transcript (with grace window).
            try:
                await asyncio.wait_for(
                    committed_done.wait(), timeout=_COMMIT_GRACE_SEC
                )
            except asyncio.TimeoutError:
                if not committed_done.is_set():
                    raise RealtimeError(
                        "Timed out waiting for committed_transcript",
                        retryable=True,
                    )

            # Adaptive drain: Scribe v2 segments long audio by internal VAD
            # into N committed_transcript events. We wait _TAIL_IDLE_SEC
            # after the LAST committed; the timer resets every time a new
            # one arrives. So a 4-minute clip emitting 8 segments is just
            # as well drained as a 30-second clip emitting 1. The reader
            # also exits naturally on server-side close, ending the loop.
            # C1: total audio we actually sent (PCM16 = 2 bytes/sample). Final
            # once the sender has drained, which it has (awaited above).
            sent_audio_ms = (
                int(sent_bytes / (sample_rate * 2) * 1000) if sample_rate else 0
            )
            drain_start = time.monotonic()
            while True:
                if reader_task.done():
                    break
                # C1: committed timestamps cover (almost) all sent audio →
                # the transcript is complete, finish now instead of idling.
                # The `<= 2×` guard rejects a seconds/ms misparse so we can't
                # finish early on a bogusly-large timestamp.
                if (sent_audio_ms > 0 and committed_end_ms > 0
                        and committed_end_ms <= sent_audio_ms * 2
                        and committed_end_ms >= sent_audio_ms - _TAIL_COVERAGE_SLACK_MS):
                    logging.info(
                        "Realtime drain: timestamps cover audio "
                        "(%d/%d ms) — finishing early (C1)",
                        committed_end_ms, sent_audio_ms,
                    )
                    break
                if time.monotonic() - drain_start > _TAIL_MAX_TOTAL_SEC:
                    logging.warning(
                        "Realtime drain hit hard cap (%.0fs) — bailing "
                        "(committed=%d so far)",
                        _TAIL_MAX_TOTAL_SEC, committed_count,
                    )
                    break
                new_committed.clear()
                try:
                    await asyncio.wait_for(
                        new_committed.wait(), timeout=_TAIL_IDLE_SEC
                    )
                    # Got another committed — loop and wait for the next.
                    continue
                except asyncio.TimeoutError:
                    # Idle window elapsed without new committed → done.
                    break
        finally:
            # Both tasks must end before the `async with ws` block exits to
            # avoid "Task was destroyed but it is pending" warnings.
            for t in (reader_task, sender_task):
                if not t.done():
                    t.cancel()
            for t in (reader_task, sender_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        # Diagnostics: helps tell server-segmented truncation from
        # sender-lag truncation when the user reports a clipped tail.
        send_dur = time.monotonic() - send_start_t
        rate = (sent_bytes / send_dur) if send_dur > 0 else 0.0
        logging.info(
            "Realtime session: %d msgs / %d bytes sent in %.1f s "
            "(%.0f B/s), %d partial, %d committed event(s)",
            sent_messages, sent_bytes, send_dur, rate,
            partial_count, committed_count,
        )

        if fatal_err is not None:
            raise fatal_err
        return committed_text.strip()
