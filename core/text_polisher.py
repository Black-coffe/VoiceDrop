"""
Text Polisher - optional LLM cleanup of dictated text via Claude Haiku.

Removes speech fillers (э, ну, вот, как бы…), fixes punctuation/casing and
obvious mishears, while preserving meaning, language (RU/UK/EN) and all
substantive words — including literal command phrases like "новая строка", which
a later pass turns into formatting.

Best-effort: any failure, timeout, or empty result returns the ORIGINAL text
unchanged — dictation is never lost to polishing. Calls the Anthropic Messages
API directly over httpx (no SDK dependency, keeps the PyInstaller build lean).
"""
import logging
import time
from typing import Optional

import httpx

from config import ANTHROPIC_API_KEY, POLISH_MODEL

_API_URL = "https://api.anthropic.com/v1/messages"
# Short timeouts: polishing must never hang the dictation pipeline.
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
_MIN_CHARS = 12      # skip ultra-short utterances (not worth latency/cost)
_MAX_TOKENS = 2048

_SYSTEM_PROMPT = (
    "Ты — редактор надиктованного текста. Тебе дают сырой результат "
    "распознавания речи (русский, украинский или английский). Очисти его:\n"
    "- убери слова-паразиты и заполнители речи (э, ээ, эм, мм, аа, ну, вот, "
    "как бы, типа, значит), фальстарты и повторы;\n"
    "- исправь пунктуацию, заглавные буквы и очевидные ошибки распознавания;\n"
    "- СОХРАНИ исходный язык (НЕ переводи), смысл и все значимые слова;\n"
    "- НЕ добавляй ничего от себя, не дописывай и не сокращай содержание;\n"
    "- НЕ добавляй переносы строк и форматирование;\n"
    "- служебные фразы вроде «новая строка», «новый абзац», «код блок», "
    "«новый пункт» оставляй ДОСЛОВНО как есть (их обработают позже).\n"
    "Верни ТОЛЬКО очищенный текст — без кавычек, пояснений и преамбулы."
)


class TextPolisher:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key if api_key is not None else ANTHROPIC_API_KEY
        self.model = model or POLISH_MODEL
        self._client: Optional[httpx.Client] = None
        self._warned_no_key = False

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(timeout=_TIMEOUT)
        return self._client

    def polish(self, text: str, language: Optional[str] = None) -> str:
        """Return cleaned text, or the original on any problem (never raises)."""
        if not text or len(text.strip()) < _MIN_CHARS:
            return text
        if not self.api_key:
            if not self._warned_no_key:
                logging.warning("Polish enabled but ANTHROPIC_API_KEY is not set; skipping.")
                self._warned_no_key = True
            return text

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            # System prompt as a cacheable block (no effect below Haiku's 4096-token
            # cache minimum, but harmless and future-proof).
            "system": [{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{"role": "user", "content": text}],
        }

        try:
            t0 = time.time()
            resp = self._get_client().post(_API_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                logging.warning(f"Polish API {resp.status_code}: {resp.text[:200]}")
                return text
            data = resp.json()
            cleaned = "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text"
            ).strip()
            if not cleaned:
                return text
            logging.info(f"Polished in {time.time() - t0:.2f}s")
            return cleaned
        except Exception as e:
            logging.warning(f"Polish failed ({e}); using original text")
            return text

    def close(self):
        if self._client and not self._client.is_closed:
            self._client.close()
