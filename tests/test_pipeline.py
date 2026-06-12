"""Tests for TextPipeline — the pure post-STT transform extracted from main (E1).

Verifies the chain order and the mode gating without any UI/IO: polish runs only
in text mode when enabled (and is skipped via min_words), dictionary + voice
commands always run, code-style runs only in code mode, and polish seconds are
returned for the C5 latency line.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pipeline import TextPipeline


def _pipeline():
    polisher = MagicMock()
    polisher.polish.side_effect = lambda t, **kw: t + "|P"
    replacer = MagicMock()
    replacer.apply.side_effect = lambda t: t + "|R"
    commands = MagicMock()
    commands.apply.side_effect = lambda t: t + "|C"
    profiles = MagicMock()
    profiles.apply_code_style.side_effect = lambda t: t + "|CODE"
    return TextPipeline(polisher, replacer, commands, profiles), polisher, profiles


class PipelineTests(unittest.TestCase):
    def test_text_mode_polishes_then_dict_then_commands(self):
        p, polisher, profiles = _pipeline()
        out, polish_sec = p.process("raw", mode="text", polish=True)
        self.assertEqual(out, "raw|P|R|C")          # order preserved
        polisher.polish.assert_called_once()
        profiles.apply_code_style.assert_not_called()  # not code mode
        self.assertGreaterEqual(polish_sec, 0.0)

    def test_text_mode_polish_disabled_skips_llm(self):
        p, polisher, _ = _pipeline()
        out, polish_sec = p.process("raw", mode="text", polish=False)
        self.assertEqual(out, "raw|R|C")            # no |P
        polisher.polish.assert_not_called()
        self.assertEqual(polish_sec, 0.0)

    def test_code_mode_never_polishes_and_applies_code_style(self):
        p, polisher, profiles = _pipeline()
        out, _ = p.process("raw", mode="code", polish=True)
        self.assertEqual(out, "raw|R|C|CODE")       # dict+commands then code-style
        polisher.polish.assert_not_called()
        profiles.apply_code_style.assert_called_once()

    def test_min_words_passed_to_polisher(self):
        p, polisher, _ = _pipeline()
        p.process("raw", mode="text", polish=True, polish_min_words=8)
        _, kwargs = polisher.polish.call_args
        self.assertEqual(kwargs["min_words"], 8)

    def test_on_partial_and_language_forwarded(self):
        p, polisher, _ = _pipeline()
        cb = object()
        p.process("raw", mode="text", polish=True, language="ru", on_polish_partial=cb)
        _, kwargs = polisher.polish.call_args
        self.assertEqual(kwargs["language"], "ru")
        self.assertIs(kwargs["on_partial"], cb)


if __name__ == "__main__":
    unittest.main()
