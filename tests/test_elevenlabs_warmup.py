"""Tests for ElevenLabsClient.warm_up() — added with the level-A optimization
(connection pre-warming so the first transcribe() of the session skips the
TLS handshake). The contract is best-effort: warm_up must never raise.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make repo root importable when run via `python -m unittest discover tests`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from core.elevenlabs_api import ElevenLabsClient


class WarmUpTests(unittest.TestCase):
    def test_skips_when_no_api_key(self):
        """No key → no network call. Otherwise we'd hit the API with a bogus
        header and waste rate-limit budget on a guaranteed 401."""
        client = ElevenLabsClient(api_key=None)
        # Force-set in case env had a key
        client.api_key = None
        with patch.object(client, "_get_client") as get_client_mock:
            client.warm_up()
            get_client_mock.assert_not_called()

    def test_sends_head_with_api_key(self):
        """With a key, warm-up issues HEAD to the STT URL carrying xi-api-key.
        HEAD opens TCP+TLS without uploading audio or being billed for an STT
        request."""
        client = ElevenLabsClient(api_key="test-key-123")
        http_mock = MagicMock()
        with patch.object(client, "_get_client", return_value=http_mock):
            client.warm_up()
        http_mock.head.assert_called_once()
        args, kwargs = http_mock.head.call_args
        # First positional is the URL
        self.assertIn("elevenlabs.io", args[0])
        self.assertEqual(kwargs["headers"]["xi-api-key"], "test-key-123")
        # Custom short timeout so warm-up never piles up behind a real call
        self.assertIsInstance(kwargs["timeout"], httpx.Timeout)

    def test_silent_on_network_failure(self):
        """A failing warm-up must not raise — first transcribe just pays the
        handshake. If this test goes red, callers will see startup exceptions."""
        client = ElevenLabsClient(api_key="test-key")
        http_mock = MagicMock()
        http_mock.head.side_effect = httpx.ConnectError("offline")
        with patch.object(client, "_get_client", return_value=http_mock):
            # Must not raise.
            client.warm_up()

    def test_silent_on_unexpected_exception(self):
        """Even non-httpx exceptions are swallowed — defence in depth."""
        client = ElevenLabsClient(api_key="test-key")
        http_mock = MagicMock()
        http_mock.head.side_effect = RuntimeError("boom")
        with patch.object(client, "_get_client", return_value=http_mock):
            client.warm_up()


if __name__ == "__main__":
    unittest.main()
