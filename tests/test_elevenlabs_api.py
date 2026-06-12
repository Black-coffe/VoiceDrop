"""Tests for ElevenLabsClient.transcribe error classification (roadmap A1/A2).

Two things under test:
  * scribe_v2 is the model_id sent (A1 — scribe_v1 is removed 2026-07-09).
  * The retryable flag on TranscriptionError: a PERMANENT 4xx (e.g. 400
    audio_too_short / validation, 401/403 auth) -> retryable=False, so the
    pending loop dead-letters it instead of retrying for ever. A TRANSIENT
    status (500/502/503/504/429) -> retryable=True, so it keeps being retried.

We mock the underlying httpx client so no network is touched.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.elevenlabs_api import ElevenLabsClient, TranscriptionError


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _client_returning(response):
    """An ElevenLabsClient whose HTTP client returns `response` on post().
    max_retries=0 keeps the test fast (no backoff sleeps) — for a transient
    status this still raises with retryable=True at attempt 0."""
    client = ElevenLabsClient(api_key="test-key")
    client.max_retries = 0
    http = MagicMock()
    http.post.return_value = response
    client._client = http
    # _get_client returns the existing non-closed client; ensure is_closed False
    http.is_closed = False
    return client, http


class TranscribeModelTests(unittest.TestCase):
    def test_uses_scribe_v2_model_id(self):
        ok = FakeResponse(200, payload={"text": "привет", "language_code": "rus"})
        client, http = _client_returning(ok)
        result = client.transcribe(b"wav", language="ru")
        self.assertEqual(result, "привет")
        _, kwargs = http.post.call_args
        self.assertEqual(kwargs["data"]["model_id"], "scribe_v2")
        # We still suppress non-speech audio-event tags in v2.
        self.assertEqual(kwargs["data"]["tag_audio_events"], "false")


class TranscribeErrorClassificationTests(unittest.TestCase):
    def test_400_audio_too_short_is_not_retryable(self):
        resp = FakeResponse(
            400,
            payload={"detail": {"status": "audio_too_short",
                                "message": "audio too short"}},
        )
        client, _ = _client_returning(resp)
        with self.assertRaises(TranscriptionError) as ctx:
            client.transcribe(b"tiny")
        # Permanent -> pending loop will dead-letter, not retry.
        self.assertFalse(ctx.exception.retryable)
        self.assertFalse(ctx.exception.offline)

    def test_401_auth_is_not_retryable(self):
        resp = FakeResponse(401, payload={"detail": "invalid api key"})
        client, _ = _client_returning(resp)
        with self.assertRaises(TranscriptionError) as ctx:
            client.transcribe(b"wav")
        self.assertFalse(ctx.exception.retryable)

    def test_500_is_retryable(self):
        resp = FakeResponse(500, text="internal error")
        client, _ = _client_returning(resp)
        with patch("core.elevenlabs_api.time.sleep"):  # no real backoff wait
            with self.assertRaises(TranscriptionError) as ctx:
                client.transcribe(b"wav")
        # Transient server error -> keep retrying from the pending queue.
        self.assertTrue(ctx.exception.retryable)

    def test_429_rate_limited_is_retryable(self):
        resp = FakeResponse(429, text="slow down", headers={"Retry-After": "1"})
        client, _ = _client_returning(resp)
        with patch("core.elevenlabs_api.time.sleep"):
            with self.assertRaises(TranscriptionError) as ctx:
                client.transcribe(b"wav")
        self.assertTrue(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()
