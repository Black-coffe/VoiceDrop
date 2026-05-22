"""
Text Replacer - user-defined post-STT replacements (custom dictionary).

Fixes tech terms / names that speech-to-text mangles when dictated in RU/UK,
e.g. spoken "элевен лабс" -> "ElevenLabs". Rules live in replacements.json next
to the exe so they can be edited without rebuilding; the file is hot-reloaded
when it changes (mtime check), so edits apply to the next dictation.
"""
import json
import logging
import re
from pathlib import Path
from typing import List, Optional, Tuple

from config import BASE_DIR

# Spoken (usually Cyrillic phonetic) variant -> canonical spelling.
# Order matters: longer / multi-word entries first so they win before shorter ones
# (e.g. "клод код" -> "Claude Code" before "клод" -> "Claude").
DEFAULT_RULES = [
    {"from": "войс дроп", "to": "VoiceDrop"},
    {"from": "войсдроп", "to": "VoiceDrop"},
    {"from": "voice drop", "to": "VoiceDrop"},
    {"from": "элевен лабс", "to": "ElevenLabs"},
    {"from": "элевенлабс", "to": "ElevenLabs"},
    {"from": "eleven labs", "to": "ElevenLabs"},
    # Inflection-aware (regex): \w* swallows RU/UK case endings (кодом, питоне, …).
    # "Claude Code" must come before standalone "Claude".
    {"from": r"\b(клод|клауд|клоуд)\s+код\w*", "to": "Claude Code", "regex": True},
    {"from": r"\b(клод|клауд|клоуд)\b", "to": "Claude", "regex": True},
    {"from": r"\b(пай\s?чарм)\w*", "to": "PyCharm", "regex": True},
    {"from": r"\b(гит\s?хаб)\w*", "to": "GitHub", "regex": True},
    {"from": r"\bдокер\w*", "to": "Docker", "regex": True},
    {"from": r"\b(пайтон|питон|патон)\w*", "to": "Python", "regex": True},
    {"from": r"\bвиндо(вс|ус)\w*", "to": "Windows", "regex": True},
    {"from": "эй пи ай", "to": "API"},
]


class TextReplacer:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (BASE_DIR / "replacements.json")
        self._raw: Optional[str] = None  # last-seen file content (for change detection)
        self._compiled: List[Tuple[re.Pattern, str]] = []
        if not self.path.exists():
            self._seed_defaults()
        self._load()

    def _seed_defaults(self):
        """Create replacements.json with sensible defaults on first run."""
        payload = {
            "_comment": ("Правила замены текста после распознавания (from -> to). "
                         "Регистр игнорируется, совпадение по целым словам. "
                         "Добавляйте свои термины/имена и варианты их произношения. "
                         "Поля рядом с правилом (необязательные): "
                         "\"whole_word\": false — подстрока; "
                         "\"case_sensitive\": true; \"regex\": true."),
            "rules": DEFAULT_RULES,
        }
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logging.info(f"Seeded default replacements: {self.path}")
        except Exception as e:
            logging.error(f"Could not seed replacements file: {e}")

    def _load(self):
        """(Re)load rules if the file content changed since last load.

        Compares raw content rather than mtime — Windows mtime resolution is
        coarse (~15 ms) and would miss quick edits.
        """
        raw: Optional[str] = None
        if self.path.exists():
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception as e:
                logging.error(f"Failed to read replacements.json: {e}")
                raw = None

        if raw == self._raw:
            return  # unchanged -> keep compiled rules (cheap no-op per dictation)
        self._raw = raw

        rules = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    rules = data.get("rules", [])
            except Exception as e:
                logging.error(f"Failed to parse replacements.json: {e}")

        compiled: List[Tuple[re.Pattern, str]] = []
        for rule in rules:
            try:
                src = (rule.get("from") or "").strip()
                dst = rule.get("to", "")
                if not src:
                    continue
                flags = 0 if rule.get("case_sensitive") else re.IGNORECASE
                if rule.get("regex"):
                    pattern = re.compile(src, flags)
                else:
                    body = re.escape(src)
                    if rule.get("whole_word", True):
                        body = r"\b" + body + r"\b"
                    pattern = re.compile(body, flags)
                compiled.append((pattern, dst))
            except re.error as e:
                logging.error(f"Bad replacement rule {rule}: {e}")
        self._compiled = compiled
        logging.info(f"Loaded {len(compiled)} text-replacement rule(s)")

    def apply(self, text: str) -> str:
        """Apply all replacement rules to text (in order)."""
        if not text:
            return text
        self._load()  # hot-reload on edit
        for pattern, dst in self._compiled:
            text = pattern.sub(lambda m, d=dst: d, text)
        return text
