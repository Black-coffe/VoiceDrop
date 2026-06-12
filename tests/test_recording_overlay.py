"""Tests for RecordingOverlay pure helpers (roadmap G1/G2).

The Tk widget itself needs a display, so we only unit-test the pure static
helpers: the live-subtitle tail formatting (G1) and the peak-hold decay (G2).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.recording_overlay import RecordingOverlay as RO


class SubtitleDisplayTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(RO._subtitle_display("  привет   мир \n\t"), "привет мир")

    def test_short_text_unchanged(self):
        self.assertEqual(RO._subtitle_display("hello world", 90), "hello world")

    def test_long_text_keeps_tail_with_ellipsis(self):
        out = RO._subtitle_display("x" * 200, 20)
        self.assertEqual(len(out), 20)
        self.assertTrue(out.startswith("…"))
        self.assertTrue(out.endswith("x"))

    def test_empty_and_none_safe(self):
        self.assertEqual(RO._subtitle_display(""), "")
        self.assertEqual(RO._subtitle_display(None), "")

    def test_tail_is_most_recent_text(self):
        # The user cares about the latest words — the END must be preserved.
        out = RO._subtitle_display("начало " + "слово " * 50 + "КОНЕЦ", 15)
        self.assertIn("КОНЕЦ", out)


class PeakDecayTests(unittest.TestCase):
    def test_instant_rise_to_louder_level(self):
        self.assertEqual(RO._decay_peak(0.3, 0.9), 0.9)

    def test_decays_when_quieter(self):
        # Held peak decays toward the current (quieter) level, not instantly.
        p = RO._decay_peak(0.8, 0.1, decay=0.9)
        self.assertAlmostEqual(p, 0.72, places=6)
        self.assertGreater(p, 0.1)  # still above the current level (held)

    def test_decay_never_below_current_level(self):
        p = RO._decay_peak(0.05, 0.2)  # current louder than decayed peak
        self.assertEqual(p, 0.2)

    def test_repeated_decay_converges_down(self):
        p = 1.0
        for _ in range(50):
            p = RO._decay_peak(p, 0.0, decay=0.9)
        self.assertLess(p, 0.01)


if __name__ == "__main__":
    unittest.main()
