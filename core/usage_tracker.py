"""
Usage tracker - persists ElevenLabs STT usage across runs in usage.json.

We can measure the real billable unit (audio time) and request count exactly;
ElevenLabs STT is billed by audio duration and the API returns no price, so cost
is an ESTIMATE from a configurable per-hour rate. Chars (output length) are tracked
for interest, not billing.
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from config import BASE_DIR


class UsageTracker:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (BASE_DIR / "usage.json")
        self._data = {"total_requests": 0, "total_audio_ms": 0, "total_chars": 0, "days": {}}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k in ("total_requests", "total_audio_ms", "total_chars"):
                    self._data[k] = int(d.get(k, 0) or 0)
                days = d.get("days")
                self._data["days"] = days if isinstance(days, dict) else {}
        except Exception as e:
            logging.error(f"Failed to read usage.json: {e}")

    def _save(self):
        tmp = self.path.parent / (self.path.name + ".tmp")
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as e:
            logging.error(f"Failed to save usage.json: {e}")

    def record(self, audio_ms: int, chars: int):
        """Record one successful (billed) transcription."""
        try:
            audio_ms = int(audio_ms or 0)
            chars = int(chars or 0)
            self._data["total_requests"] += 1
            self._data["total_audio_ms"] += audio_ms
            self._data["total_chars"] += chars
            today = date.today().isoformat()
            day = self._data["days"].setdefault(today, {"requests": 0, "audio_ms": 0, "chars": 0})
            day["requests"] += 1
            day["audio_ms"] += audio_ms
            day["chars"] += chars
            self._save()
        except Exception as e:
            logging.error(f"usage record failed: {e}")

    def summary(self, cost_per_hour: float = 0.40) -> dict:
        today = date.today().isoformat()
        d = self._data["days"].get(today, {"requests": 0, "audio_ms": 0, "chars": 0})

        def cost(ms: int) -> float:
            return (ms / 3_600_000.0) * cost_per_hour

        return {
            "today_requests": d.get("requests", 0),
            "today_min": d.get("audio_ms", 0) / 60000.0,
            "today_cost": cost(d.get("audio_ms", 0)),
            "total_requests": self._data["total_requests"],
            "total_min": self._data["total_audio_ms"] / 60000.0,
            "total_cost": cost(self._data["total_audio_ms"]),
            "total_chars": self._data["total_chars"],
        }

    def month_summary(self, cost_per_hour: float = 0.40) -> dict:
        """Aggregate usage for the current calendar month so far (LOCAL date)."""
        prefix = date.today().strftime("%Y-%m-")
        m_requests = 0
        m_audio_ms = 0
        for day_key, day in self._data["days"].items():
            if not day_key.startswith(prefix):
                continue
            m_requests += int(day.get("requests", 0) or 0)
            m_audio_ms += int(day.get("audio_ms", 0) or 0)
        return {
            "month_requests": m_requests,
            "month_min": m_audio_ms / 60000.0,
            "month_cost": (m_audio_ms / 3_600_000.0) * cost_per_hour,
        }
