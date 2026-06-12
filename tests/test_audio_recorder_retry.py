"""Tests for AudioRecorder.start_recording smart retry (roadmap A4).

After a USB selective-suspend wake the FOX mic can fail to open in two ways,
both of which must be retried (not fail the press): the device is missing from
sd.query_devices() for a moment (resolve raises RuntimeError), or sd.InputStream
raises PaErrorCode -9999. We retry up to 5 times with backoff, re-resolving BY
NAME each attempt, and only raise once the budget is exhausted.

We patch the recorder's resolve/samplerate helpers and core.audio_recorder.sd so
nothing touches real audio hardware.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.audio_recorder as ar_mod
from core.audio_recorder import AudioRecorder, _OPEN_MAX_ATTEMPTS


class StartRecordingRetryTests(unittest.TestCase):
    def setUp(self):
        self.rec = AudioRecorder()
        # Always pretend the native rate query succeeds.
        self._sr_patch = patch.object(
            self.rec, "_query_native_samplerate", return_value=16000
        )
        self._sr_patch.start()
        # Never actually sleep through the backoff in tests.
        self._sleep_patch = patch("core.audio_recorder.time.sleep")
        self._sleep_patch.start()

    def tearDown(self):
        self._sr_patch.stop()
        self._sleep_patch.stop()

    def test_success_first_attempt_no_retry(self):
        on_retry = MagicMock()
        with patch.object(self.rec, "_resolve_device_for_capture", return_value=3) as resolve, \
             patch("core.audio_recorder.sd.InputStream") as InputStream:
            InputStream.return_value = MagicMock()
            self.rec.start_recording(on_retry=on_retry)
        self.assertTrue(self.rec.is_recording)
        resolve.assert_called_once()
        on_retry.assert_not_called()

    def test_device_not_found_then_recovers(self):
        """First attempt: mic not in the list yet (RuntimeError). Second:
        it reappears and opens. Press succeeds; re-resolve happened twice."""
        on_retry = MagicMock()
        with patch.object(
            self.rec, "_resolve_device_for_capture",
            side_effect=[RuntimeError("микрофон не найден"), 3],
        ) as resolve, \
             patch("core.audio_recorder.sd.InputStream") as InputStream:
            InputStream.return_value = MagicMock()
            self.rec.start_recording(on_retry=on_retry)
        self.assertTrue(self.rec.is_recording)
        self.assertEqual(resolve.call_count, 2)
        # on_retry fires before attempt 2 with (attempt_number, total).
        on_retry.assert_called_once_with(2, _OPEN_MAX_ATTEMPTS)

    def test_minus_9999_open_error_then_recovers(self):
        ok_stream = MagicMock()
        with patch.object(self.rec, "_resolve_device_for_capture", return_value=3), \
             patch("core.audio_recorder.sd.InputStream") as InputStream:
            InputStream.side_effect = [
                Exception("Error opening InputStream: Invalid device [PaErrorCode -9999]"),
                ok_stream,
            ]
            self.rec.start_recording()
        self.assertTrue(self.rec.is_recording)
        self.assertEqual(InputStream.call_count, 2)
        ok_stream.start.assert_called_once()

    def test_all_attempts_fail_raises_after_budget(self):
        on_retry = MagicMock()
        with patch.object(
            self.rec, "_resolve_device_for_capture",
            side_effect=RuntimeError("микрофон не найден"),
        ) as resolve:
            with self.assertRaises(RuntimeError):
                self.rec.start_recording(on_retry=on_retry)
        # Re-resolved on every attempt; on_retry fired before attempts 2..5.
        self.assertEqual(resolve.call_count, _OPEN_MAX_ATTEMPTS)
        self.assertEqual(on_retry.call_count, _OPEN_MAX_ATTEMPTS - 1)
        self.assertFalse(self.rec.is_recording)

    def test_open_error_rolls_back_recording_state_on_final_failure(self):
        with patch.object(self.rec, "_resolve_device_for_capture", return_value=3), \
             patch("core.audio_recorder.sd.InputStream") as InputStream:
            InputStream.side_effect = Exception("boom -9999 out of range")
            with self.assertRaises(Exception):
                self.rec.start_recording()
        self.assertFalse(self.rec.is_recording)
        self.assertEqual(InputStream.call_count, _OPEN_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
