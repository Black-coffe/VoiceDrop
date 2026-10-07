"""Tests for AudioRecorder stream lifecycle — no orphaned streams (roadmap D8).

A live PortAudio stream that loses its last reference is a time bomb:
sounddevice has no __del__, so GC eventually frees the cffi callback while
PortAudio keeps calling it → 0xc0000005, no traceback. Until then the orphan
also feeds _frames, so every clip is captured twice. Seen in production twice
(2026-10-05, 2026-10-07) after a hotkey chord bounce opened two streams.

We patch core.audio_recorder.sd so nothing touches real audio hardware.
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio_recorder import AudioRecorder


class StreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.rec = AudioRecorder()
        for target, kwargs in (
            ("_query_native_samplerate", {"return_value": 16000}),
            ("_resolve_device_for_capture", {"return_value": 3}),
        ):
            p = patch.object(self.rec, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        sleep_patch = patch("core.audio_recorder.time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_second_start_closes_previous_stream(self):
        first, second = MagicMock(), MagicMock()
        with patch("core.audio_recorder.sd.InputStream", side_effect=[first, second]):
            self.rec.start_recording()
            with self.assertLogs(level="WARNING") as logs:
                self.rec.start_recording()
        first.close.assert_called_once()
        second.close.assert_not_called()
        self.assertIs(self.rec._stream, second)
        self.assertTrue(any("orphan guard" in m for m in logs.output))

    def test_stop_during_inflight_open_closes_that_stream(self):
        """Old bug: stop saw _stream=None while the open was still in flight,
        returned, and the stream opened a moment later kept running forever."""
        stream = MagicMock()
        opening = threading.Event()
        release_open = threading.Event()

        def slow_open(**_kwargs):
            opening.set()
            release_open.wait(2.0)
            return stream

        with patch("core.audio_recorder.sd.InputStream", side_effect=slow_open):
            starter = threading.Thread(target=self.rec.start_recording)
            starter.start()
            self.assertTrue(opening.wait(2.0))
            stopper_result = []
            stopper = threading.Thread(
                target=lambda: stopper_result.append(self.rec.stop_recording())
            )
            stopper.start()
            stopper.join(0.2)
            self.assertTrue(stopper.is_alive(), "stop must wait for the open")
            release_open.set()
            starter.join(2.0)
            stopper.join(2.0)

        stream.stop.assert_called_once()
        stream.close.assert_called_once()
        self.assertIsNone(self.rec._stream)
        self.assertFalse(self.rec.is_recording)
        self.assertEqual(stopper_result, [(b"", 0)])

    def test_failed_start_closes_the_opened_stream(self):
        bad, good = MagicMock(), MagicMock()
        bad.start.side_effect = Exception("boom -9999 out of range")
        with patch("core.audio_recorder.sd.InputStream", side_effect=[bad, good]):
            self.rec.start_recording()
        bad.close.assert_called_once()
        self.assertIs(self.rec._stream, good)

    def test_stop_closes_stream_even_if_stop_raises(self):
        stream = MagicMock()
        stream.stop.side_effect = Exception("MME stop failed")
        with patch("core.audio_recorder.sd.InputStream", return_value=stream):
            self.rec.start_recording()
        with self.assertRaises(Exception):
            self.rec.stop_recording()
        stream.close.assert_called_once()
        self.assertIsNone(self.rec._stream)


if __name__ == "__main__":
    unittest.main()
