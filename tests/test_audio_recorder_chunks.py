"""Tests for AudioRecorder.set_chunk_callback / _to_rt_pcm16 (level B).

The realtime path lives or dies by whether the audio callback delivers
well-shaped PCM16 16-kHz mono bytes to the chunk hook. Direct unit tests
on the resample + format helper let us verify that without spinning up
a real PortAudio stream.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from core.audio_recorder import AudioRecorder, _RT_TARGET_SR


class RealtimeChunkHookTests(unittest.TestCase):
    def setUp(self):
        self.rec = AudioRecorder()

    def test_to_rt_pcm16_passthrough_at_target_rate(self):
        """When the device already runs at 16 kHz, no resample work."""
        self.rec.sample_rate = _RT_TARGET_SR
        block = np.full((1024, 1), 0.5, dtype=np.float32)
        out = self.rec._to_rt_pcm16(block)
        pcm = np.frombuffer(out, dtype="<i2")
        self.assertEqual(len(pcm), 1024)  # no length change
        # 0.5 * 32767 = 16383
        self.assertTrue(np.all(np.abs(pcm - 16383) <= 1))

    def test_to_rt_pcm16_resamples_from_44100_to_16000(self):
        """1024 frames at 44.1 kHz → ~372 frames at 16 kHz, within ±1."""
        self.rec.sample_rate = 44100
        block = np.full((1024, 1), 0.1, dtype=np.float32)
        out = self.rec._to_rt_pcm16(block)
        pcm = np.frombuffer(out, dtype="<i2")
        expected = int(round(1024 * _RT_TARGET_SR / 44100))
        self.assertTrue(abs(len(pcm) - expected) <= 1)
        # Constant-input clipped to int16: 0.1*32767 = 3276
        self.assertTrue(np.all(np.abs(pcm - 3276) <= 2))

    def test_to_rt_pcm16_clips_overload(self):
        """Out-of-range float input must be clipped, not wrap around."""
        self.rec.sample_rate = _RT_TARGET_SR
        block = np.array([[2.0], [-2.0], [0.0]], dtype=np.float32)
        pcm = np.frombuffer(self.rec._to_rt_pcm16(block), dtype="<i2")
        self.assertEqual(pcm[0], 32767)
        self.assertEqual(pcm[1], -32767)
        self.assertEqual(pcm[2], 0)

    def test_set_chunk_callback_install_and_remove(self):
        called = []
        self.rec.set_chunk_callback(lambda b: called.append(b))
        self.assertIsNotNone(self.rec._chunk_callback)
        self.rec.set_chunk_callback(None)
        self.assertIsNone(self.rec._chunk_callback)

    def test_audio_callback_invokes_chunk_hook_only_while_recording(self):
        """No chunks must leak when is_recording=False (between presses)."""
        called = []
        self.rec.set_chunk_callback(lambda b: called.append(b))
        self.rec.sample_rate = _RT_TARGET_SR
        block = np.zeros((1024, 1), dtype=np.float32)
        # Not recording — callback must NOT fire.
        self.rec._audio_callback(block, 1024, None, None)
        self.assertEqual(called, [])
        # Now recording — must fire once.
        self.rec.is_recording = True
        self.rec._audio_callback(block, 1024, None, None)
        self.assertEqual(len(called), 1)
        # Chunk shape is bytes, length = 1024 frames * 2 bytes.
        self.assertIsInstance(called[0], (bytes, bytearray))
        self.assertEqual(len(called[0]), 1024 * 2)

    def test_audio_callback_swallows_chunk_hook_exception(self):
        """A failing chunk hook must NOT corrupt the recording (raw _frames
        still accumulates so the batch fallback has the audio)."""
        def boom(_):
            raise RuntimeError("downstream queue is dead")
        self.rec.set_chunk_callback(boom)
        self.rec.is_recording = True
        self.rec.sample_rate = _RT_TARGET_SR
        block = np.full((128, 1), 0.2, dtype=np.float32)
        # Must not propagate.
        self.rec._audio_callback(block, 128, None, None)
        # Raw frame still captured.
        self.assertEqual(len(self.rec._frames), 1)


if __name__ == "__main__":
    unittest.main()
