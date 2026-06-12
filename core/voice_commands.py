"""
Voice Commands - turn spoken formatting commands into real characters.

Scribe (v2) already adds punctuation, so the defaults focus on STRUCTURE that STT
cannot produce: new line / paragraph / code block / list item / tab. Phrases are
matched as whole words (RU/UK, case-insensitive); after substitution the spacing
around inserted newlines is tidied. Editable via commands.json next to the exe
(hot-reloaded by content compare).
"""
import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from config import BASE_DIR

# Spoken phrase -> inserted characters. Say commands in base form.
DEFAULT_COMMANDS = [
    {"say": "новый абзац", "insert": "\n\n"},
    {"say": "новый параграф", "insert": "\n\n"},
    {"say": "новий абзац", "insert": "\n\n"},
    {"say": "новая строка", "insert": "\n"},
    {"say": "новая строчка", "insert": "\n"},
    {"say": "нова строка", "insert": "\n"},
    {"say": "перенос строки", "insert": "\n"},
    {"say": "блок кода", "insert": "\n```\n"},
    {"say": "код блок", "insert": "\n```\n"},
    {"say": "новый пункт", "insert": "\n- "},
    {"say": "пункт списка", "insert": "\n- "},
    {"say": "табуляция", "insert": "\t"},
]


class VoiceCommands:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (BASE_DIR / "commands.json")
        self._raw: Optional[str] = None
        self._compiled: List[Tuple[re.Pattern, str]] = []
        if not self.path.exists():
            self._seed_defaults()
        self._load()

    def _seed_defaults(self):
        payload = {
            "_comment": ("Голосовые команды форматирования: say -> insert "
                         "(\\n = новая строка, \\t = табуляция). Произносите команды "
                         "в базовой форме. Пунктуация (точка/запятая) НЕ включена: "
                         "распознавание Scribe расставляет её само. Добавляйте свои."),
            "commands": DEFAULT_COMMANDS,
        }
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logging.info(f"Seeded default voice commands: {self.path}")
        except Exception as e:
            logging.error(f"Could not seed commands file: {e}")

    def _load(self):
        """(Re)load commands if the file content changed (content compare, not mtime)."""
        raw: Optional[str] = None
        if self.path.exists():
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception as e:
                logging.error(f"Failed to read commands.json: {e}")
                raw = None
        if raw == self._raw:
            return
        self._raw = raw

        commands = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    commands = data.get("commands", [])
            except Exception as e:
                logging.error(f"Failed to parse commands.json: {e}")

        compiled: List[Tuple[re.Pattern, str]] = []
        for cmd in commands:
            try:
                say = (cmd.get("say") or "").strip()
                insert = cmd.get("insert", "")
                if not say:
                    continue
                pattern = re.compile(r"\b" + re.escape(say) + r"\b", re.IGNORECASE)
                compiled.append((pattern, insert))
            except re.error as e:
                logging.error(f"Bad voice command {cmd}: {e}")
        self._compiled = compiled
        logging.info(f"Loaded {len(compiled)} voice command(s)")

    def apply(self, text: str) -> str:
        """Replace spoken commands with characters; tidy spacing only if any fired."""
        if not text:
            return text
        self._load()
        total = 0
        for pattern, insert in self._compiled:
            text, n = pattern.subn(lambda m, s=insert: s, text)
            total += n
        if total:
            text = self._tidy(text)
        return text

    @staticmethod
    def _tidy(text: str) -> str:
        # Collapse runs of spaces/tabs, then trim spaces/tabs around newlines.
        text = re.sub(r'[^\S\n]{2,}', ' ', text)
        text = re.sub(r'[^\S\n]*\n[^\S\n]*', '\n', text)
        return text.strip()
