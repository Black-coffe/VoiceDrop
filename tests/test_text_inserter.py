"""Tests for TextInserter paste-target verification (roadmap D1).

The key contract: when there is no focused field to paste into, insert_text must
NOT send Ctrl+V (which would be silently lost) — it leaves the text in the
clipboard and returns False so the caller can tell the user. When a target
exists it pastes and returns True. Text is always copied to the clipboard first,
so it is never lost either way.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.text_inserter import TextInserter


class InsertTargetTests(unittest.TestCase):
    def setUp(self):
        self.ti = TextInserter()

    def test_empty_text_returns_false(self):
        self.assertFalse(self.ti.insert_text(""))

    def test_no_target_skips_paste_but_copies(self):
        with patch.object(self.ti, "_has_paste_target", return_value=False), \
             patch.object(self.ti, "_paste") as paste, \
             patch("core.text_inserter.pyperclip.copy") as copy, \
             patch("core.text_inserter.time.sleep"):
            result = self.ti.insert_text("привет")
        self.assertFalse(result)            # signals "paste failed" to caller
        paste.assert_not_called()           # no Ctrl+V into the void
        copy.assert_called_once_with("привет")  # text safe in clipboard

    def test_target_present_pastes_and_returns_true(self):
        with patch.object(self.ti, "_has_paste_target", return_value=True), \
             patch.object(self.ti, "_paste") as paste, \
             patch("core.text_inserter.pyperclip.copy"), \
             patch("core.text_inserter.time.sleep"):
            result = self.ti.insert_text("привет")
        self.assertTrue(result)
        paste.assert_called_once()

    def test_press_enter_only_when_target(self):
        # Enter must not fire when there's no target (else a stray newline).
        with patch.object(self.ti, "_has_paste_target", return_value=False), \
             patch.object(self.ti, "_paste"), \
             patch.object(self.ti, "_send_key") as send_key, \
             patch("core.text_inserter.pyperclip.copy"), \
             patch("core.text_inserter.time.sleep"):
            self.ti.insert_text("привет", press_enter=True)
        send_key.assert_not_called()        # no Enter sent on no-target

    def test_no_foreground_window_means_no_target(self):
        with patch("core.text_inserter.user32.GetForegroundWindow", return_value=0):
            self.assertFalse(self.ti._has_paste_target())

    def test_target_check_never_raises(self):
        with patch("core.text_inserter.user32.GetForegroundWindow",
                   side_effect=OSError("boom")):
            # On error we assume a target exists (never block a working paste).
            self.assertTrue(self.ti._has_paste_target())


if __name__ == "__main__":
    unittest.main()
