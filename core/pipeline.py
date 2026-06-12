"""
Text pipeline — the PURE post-STT transform (roadmap E1).

This is the testable core extracted from main.py's god-object: given a raw
transcript and the resolved dictation mode, it runs the deterministic chain

    polish (LLM, text mode only) → dictionary replacements → voice commands
    → code-style

and returns the final text. It owns NO UI, I/O, threading or app state — the
side effects (overlay, clipboard/insert, history DB, usage metering, beeps,
pending fallback) stay in main, which remains composition + lifecycle.

Keeping this layer pure means the whole transform can be unit-tested with mock
processors (see tests/test_pipeline.py), and it's the seam a future end-to-end
test (F4) plugs into.
"""
import time
from typing import Callable, Optional, Tuple


class TextPipeline:
    def __init__(self, polisher, replacer, voice_commands, profiles):
        self.polisher = polisher
        self.replacer = replacer
        self.voice_commands = voice_commands
        self.profiles = profiles

    def process(
        self,
        text: str,
        *,
        mode: str,
        language: Optional[str] = None,
        polish: bool = False,
        polish_min_words: int = 0,
        on_polish_partial: Optional[Callable[[str], None]] = None,
    ) -> Tuple[str, float]:
        """Run the transform chain and return (final_text, polish_seconds).

        Order matters and mirrors the original main._finish_text:
          1. LLM polish — ONLY in text mode when ``polish`` is on (code mode is
             verbatim). Best-effort inside TextPolisher; short clips skipped via
             ``polish_min_words`` (C2).
          2. Dictionary replacements (tech terms / names STT mangles).
          3. Voice formatting commands ("новая строка", "код блок", …).
          4. Code-style (verbatim cleanup) — ONLY in code mode.

        ``polish_seconds`` is returned so the caller can keep the single
        per-clip latency line (C5) without re-timing.
        """
        polish_sec = 0.0
        if mode == "text" and polish:
            t0 = time.monotonic()
            text = self.polisher.polish(
                text, language=language, on_partial=on_polish_partial,
                min_words=polish_min_words,
            )
            polish_sec = time.monotonic() - t0

        text = self.replacer.apply(text)
        text = self.voice_commands.apply(text)

        if mode == "code":
            text = self.profiles.apply_code_style(text)

        return text, polish_sec
