"""Tests for TextPolisher streaming mode — added with the level-A optimization
that streams partial polished text into the recording overlay.

Invariants we MUST not break:
  - The blocking path (no on_partial callback) keeps working as before.
  - When streaming, on_partial sees the ACCUMULATED text on each delta.
  - If on_partial raises, streaming continues and the final paste is intact.
  - Any API/network/parse failure returns the ORIGINAL text (best-effort).
  - The >1.5×+30 expansion safety net still kicks in for streaming too.
"""
import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.text_polisher import TextPolisher


def _sse_response(events, status_code=200):
    """Build a context-manager-shaped mock for httpx Client.stream() that yields
    the given event objects as JSON-encoded `data:` lines."""
    resp = MagicMock()
    resp.status_code = status_code
    lines = []
    for e in events:
        lines.append(f"data: {json.dumps(e)}")
        lines.append("")  # blank separator like real SSE
    resp.iter_lines.return_value = iter(lines)
    resp.read.return_value = b"error body"

    @contextmanager
    def streamer(method, url, **kwargs):
        yield resp

    return streamer


def _text_delta(text):
    return {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


class PolisherBlockingPathTests(unittest.TestCase):
    """Make sure the non-streaming path still works (regression guard)."""

    def setUp(self):
        self.polisher = TextPolisher(api_key="test-key")

    def test_blocking_returns_cleaned_text(self):
        http_mock = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "content": [{"type": "text", "text": "Привет, мир."}]
        }
        http_mock.post.return_value = resp
        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            out = self.polisher.polish("привет мир и ну вот")
        self.assertEqual(out, "Привет, мир.")
        http_mock.stream.assert_not_called()  # blocking path didn't stream

    def test_blocking_returns_original_on_api_error(self):
        http_mock = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "boom"
        http_mock.post.return_value = resp
        original = "привет мир и ну вот"
        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            out = self.polisher.polish(original)
        self.assertEqual(out, original)

    def test_returns_original_when_below_min_chars(self):
        """Don't burn an API call on a 3-letter dictation."""
        out = self.polisher.polish("ок")
        self.assertEqual(out, "ок")


class PolisherStreamingPathTests(unittest.TestCase):
    def setUp(self):
        self.polisher = TextPolisher(api_key="test-key")

    def test_callback_sees_accumulated_text(self):
        """Each delta should hand on_partial the cumulative string, not just
        the latest chunk — overlay needs the running result."""
        events = [
            _text_delta("Привет"),
            _text_delta(", "),
            _text_delta("мир."),
        ]
        http_mock = MagicMock()
        http_mock.stream = _sse_response(events)

        seen = []
        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish(
                "привет мир и ну вот", on_partial=seen.append
            )

        self.assertEqual(final, "Привет, мир.")
        self.assertEqual(seen, ["Привет", "Привет, ", "Привет, мир."])

    def test_callback_exception_does_not_break_pipeline(self):
        """If overlay update raises (e.g. Tk torn down), streaming continues
        and the FINAL paste is still correct."""
        events = [_text_delta("Hello"), _text_delta(", world!")]
        http_mock = MagicMock()
        http_mock.stream = _sse_response(events)

        def boom(_text):
            raise RuntimeError("overlay gone")

        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish("hello world ну вот", on_partial=boom)

        self.assertEqual(final, "Hello, world!")

    def test_non_200_streaming_returns_original(self):
        events = []
        http_mock = MagicMock()
        http_mock.stream = _sse_response(events, status_code=429)

        original = "this is a test of the polish path"
        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish(original, on_partial=lambda _t: None)
        self.assertEqual(final, original)

    def test_streaming_safety_net_expansion(self):
        """If the model 'answers' instead of polishing (output >1.5×+30 of
        input), we still return the ORIGINAL — same guard as the blocking path."""
        bloat = "X" * 500  # way more than input
        events = [_text_delta(bloat)]
        http_mock = MagicMock()
        http_mock.stream = _sse_response(events)

        original = "сделай разметку"  # short, will trip 1.5×+30
        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish(original, on_partial=lambda _t: None)
        self.assertEqual(final, original)

    def test_streaming_strips_wrapper_tags_if_echoed(self):
        events = [_text_delta("<recognized_speech>\nПривет.\n</recognized_speech>")]
        http_mock = MagicMock()
        http_mock.stream = _sse_response(events)

        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish(
                "привет ну вот тест", on_partial=lambda _t: None
            )
        self.assertEqual(final, "Привет.")

    def test_unknown_event_types_ignored(self):
        """Real Anthropic stream sends ping/message_start/etc — must not crash."""
        events = [
            {"type": "message_start"},
            {"type": "ping"},
            _text_delta("Hi."),
            {"type": "message_stop"},
        ]
        http_mock = MagicMock()
        http_mock.stream = _sse_response(events)

        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish(
                "hi there ну вот тест", on_partial=lambda _t: None
            )
        self.assertEqual(final, "Hi.")

    def test_streaming_falls_back_on_exception(self):
        """A mid-stream httpx error returns the original text, like blocking."""
        http_mock = MagicMock()

        @contextmanager
        def boom_stream(*a, **kw):
            raise RuntimeError("connection reset")
            yield  # unreachable

        http_mock.stream = boom_stream
        original = "this is a sentence that should survive"
        with patch.object(self.polisher, "_get_client", return_value=http_mock):
            final = self.polisher.polish(original, on_partial=lambda _t: None)
        self.assertEqual(final, original)


if __name__ == "__main__":
    unittest.main()
