"""
Key Terms - vocabulary biasing list for ElevenLabs Scribe v2 (roadmap B2).

A hot-reloaded ``keyterms.json`` next to the exe (same content-compare pattern as
replacements.json / commands.json) holding words/phrases the STT should be biased
toward — names, brands, technical terms it otherwise mangles (e.g. "VoiceDrop",
"PyInstaller", Ukrainian surnames).

Sent to ElevenLabs ONLY when ``settings.keyterms_enabled`` is true, because the
keyterms feature adds a +20% surcharge on the transcription cost. Batch allows up
to 1000 terms × 50 chars; realtime up to 50 × 20 — the caller passes the right
limits to :meth:`get`. Terms are sanitized: ElevenLabs rejects the characters
``< > { } [ ] \\`` and allows at most 5 words per term.
"""
import json
import logging
import re
from pathlib import Path
from typing import List, Optional

from config import BASE_DIR

# Characters ElevenLabs does not accept inside a keyterm.
_UNSUPPORTED = re.compile(r"[<>{}\[\]\\]")
_MAX_WORDS = 5

# Seed examples so the file is self-documenting; inert until keyterms_enabled.
DEFAULT_KEYTERMS = [
    "VoiceDrop",
    "ElevenLabs",
    "Scribe",
    "Anthropic",
    "PyInstaller",
]


class KeyTerms:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (BASE_DIR / "keyterms.json")
        self._raw: Optional[str] = None
        self._terms: List[str] = []
        if not self.path.exists():
            self._seed_defaults()
        self._load()

    def _seed_defaults(self):
        payload = {
            "_comment": (
                "Список ключевых терминов для подсказки распознаванию (имена, "
                "бренды, технические слова). Работает ТОЛЬКО если в settings.json "
                "keyterms_enabled=true. ВНИМАНИЕ: включение keyterms добавляет +20% "
                "к стоимости распознавания. Лимиты: батч до 1000 терминов по 50 "
                "символов, realtime до 50 по 20. Не более 5 слов в термине; символы "
                "< > { } [ ] \\ запрещены."
            ),
            "keyterms": DEFAULT_KEYTERMS,
        }
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logging.info(f"Seeded default keyterms: {self.path}")
        except Exception as e:
            logging.error(f"Could not seed keyterms file: {e}")

    def _load(self):
        """(Re)load terms if the file content changed (content compare, not mtime)."""
        raw: Optional[str] = None
        if self.path.exists():
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception as e:
                logging.error(f"Failed to read keyterms.json: {e}")
                raw = None
        if raw == self._raw:
            return
        self._raw = raw

        terms: List[str] = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    terms = data.get("keyterms", []) or []
                elif isinstance(data, list):
                    terms = data
            except Exception as e:
                logging.error(f"Failed to parse keyterms.json: {e}")
        self._terms = [t for t in (str(x).strip() for x in terms) if t]
        logging.info(f"Loaded {len(self._terms)} keyterm(s)")

    @staticmethod
    def _sanitize(term: str, max_len: int) -> Optional[str]:
        """Clean one term to ElevenLabs' rules, or None if nothing usable remains."""
        term = _UNSUPPORTED.sub("", term).strip()
        if not term:
            return None
        # Collapse whitespace and cap at 5 words.
        words = term.split()
        if len(words) > _MAX_WORDS:
            words = words[:_MAX_WORDS]
        term = " ".join(words)[:max_len].strip()
        return term or None

    def get(self, max_terms: int, max_len: int) -> List[str]:
        """Return the sanitized, de-duplicated keyterm list within limits.

        ``max_terms`` / ``max_len`` are the API limits for the target path
        (batch 1000/50, realtime 50/20). Hot-reloads the file on each call.
        """
        self._load()
        out: List[str] = []
        seen = set()
        for raw_term in self._terms:
            term = self._sanitize(raw_term, max_len)
            if not term:
                continue
            key = term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
            if len(out) >= max_terms:
                break
        return out
