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
# server a reasonable window before giving up. PoC showed ~300-400 ms.
_COMMIT_GRACE_SEC = 8.0
# Connection establishment timeout. Cold TLS to api.elevenlabs.io is well
# under 1s in healthy paths.
_CONNECT_TIMEOUT_SEC = 6.0
# Sentinel that the caller drops into the chunk_queue to mean "end of audio,
# send commit and finalize". Public so callers can import it.
END_OF_STREAM = object()

# Inbound event types that are fatal — abandon WS, raise RealtimeError so the
# caller can fall back to batch + pending queue.
_FATAL_EVENTS = frozenset({
    "error",
    "auth_error",
    "quota_exceeded",
    "rate_limited",
    "commit_throttled",
    "queue_overflow",
    "resource_exhausted",
    "session_time_limit_exceeded",
    "input_error",
    "chunk_size_exceeded",
    "insufficient_audio_activity",
    "transcriber_error",
})


class RealtimeError(Exception):
    """Realtime transcription failed. ``offline`` mirrors the same flag in
    TranscriptionError so the caller can route fallback the same way.

    ``retryable`` means the failure isn't a permanent input/auth error and
    the caller MAY queue the accumulated WAV for batch retry.
    """

    def __init__(self, message: str, offline: bool = False, retryable: bool = True):
        super().__init__(message)
        self.offline = offline
        self.retryable = retryable


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
        # Auto-detect when no language pinned (parity with batch path).
        url = WS_URL
        if language:
            url = f"{WS_URL}?language_code={language}"

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
        fatal_err: Optional[RealtimeError] = None

        async def reader():
            nonlocal committed_text, fatal_err
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
                        committed_done.set()
                        return  # one committed event closes the session
                    elif et == "session_started":
                        # Useful for debugging, but not actionable.
                        logging.debug(
                            "Realtime session_started: %s",
                            evt.get("session_id", "?"),
                        )
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

        async def sender():
            """Drain the chunk_queue, send each as input_audio_chunk."""
            try:
                while True:
                    item = await chunk_queue.get()
                    if item is END_OF_STREAM:
                        # Send an empty commit message to finalize. Server
                        # accepts either commit=true with audio_base_64 or
                        # an audio-less commit; we use the latter so an
                        # already-drained queue doesn't need a final chunk.
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
                    b64 = base64.b64encode(item).decode("ascii")
                    await ws.send(json.dumps({
                        "message_type": "input_audio_chunk",
                        "audio_base_64": b64,
                        "sample_rate": sample_rate,
                        "commit": False,
                    }))
            except ConnectionClosed:
                # Reader will notice and either pick up an already-arrived
                # committed_transcript or surface fatal_err.
                return

        reader_task = asyncio.create_task(reader(), name="rt-reader")
        sender_task = asyncio.create_task(sender(), name="rt-sender")

        try:
            await sender_task  # blocks until END_OF_STREAM or socket closed
            # Now wait (with a grace window) for the server's commit reply.
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

        if fatal_err is not None:
            raise fatal_err
        return committed_text.strip()
