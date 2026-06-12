"""
Usage tracker - persists ElevenLabs STT usage across runs in usage.json.

We can measure the real billable unit (audio time) and request count exactly;
ElevenLabs STT is billed by audio duration and the API returns no price, so cost
is an ESTIMATE from configurable per-hour rates. Batch and realtime are billed
at DIFFERENT rates (batch ~$0.22/h, realtime ~$0.39/h), so audio minutes are
tracked per-mode and cost is the sum of the two. Chars (output length) are
tracked for interest, not billing.

Migration: usage.json written before the batch/realtime split had only
``audio_ms`` (no per-mode breakdown). Those legacy minutes are counted as
BATCH (the only path that existed when they were recorded) so no history is
lost — see _load().
"""
import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from config import BASE_DIR

# Default per-hour rates (overage, all tiers, verified June 2026).
DEFAULT_COST_BATCH = 0.22
DEFAULT_COST_REALTIME = 0.39


def _norm_mode(mode: Optional[str]) -> str:
    return "realtime" if str(mode or "").lower() == "realtime" else "batch"


class UsageTracker:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (BASE_DIR / "usage.json")
        self._data = {
            "total_requests": 0,
            "total_audio_ms": 0,            # batch + realtime (back-compat)
            "total_audio_ms_batch": 0,
            "total_audio_ms_realtime": 0,
            "total_chars": 0,
            "days": {},
        }
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if not isinstance(d, dict):
                return
            for k in ("total_requests", "total_audio_ms", "total_chars"):
                self._data[k] = int(d.get(k, 0) or 0)

            if "total_audio_ms_batch" in d or "total_audio_ms_realtime" in d:
                self._data["total_audio_ms_batch"] = int(d.get("total_audio_ms_batch", 0) or 0)
                self._data["total_audio_ms_realtime"] = int(d.get("total_audio_ms_realtime", 0) or 0)
            else:
                # Legacy file: everything recorded so far was batch.
                self._data["total_audio_ms_batch"] = self._data["total_audio_ms"]
                self._data["total_audio_ms_realtime"] = 0

            days = d.get("days")
            days = days if isinstance(days, dict) else {}
            for day in days.values():
                if not isinstance(day, dict):
                    continue
                if "audio_ms_batch" not in day and "audio_ms_realtime" not in day:
                    day["audio_ms_batch"] = int(day.get("audio_ms", 0) or 0)
                    day["audio_ms_realtime"] = 0
                else:
                    day["audio_ms_batch"] = int(day.get("audio_ms_batch", 0) or 0)
                    day["audio_ms_realtime"] = int(day.get("audio_ms_realtime", 0) or 0)
            self._data["days"] = days
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

    def record(self, audio_ms: int, chars: int, mode: str = "batch"):
        """Record one successful (billed) transcription under ``mode``."""
        try:
            audio_ms = int(audio_ms or 0)
            chars = int(chars or 0)
            mode = _norm_mode(mode)
            self._data["total_requests"] += 1
            self._data["total_audio_ms"] += audio_ms
            self._data[f"total_audio_ms_{mode}"] += audio_ms
            self._data["total_chars"] += chars
            today = date.today().isoformat()
            day = self._data["days"].setdefault(
                today,
                {"requests": 0, "audio_ms": 0, "audio_ms_batch": 0,
                 "audio_ms_realtime": 0, "chars": 0},
            )
            day["requests"] += 1
            day["audio_ms"] += audio_ms
            day[f"audio_ms_{mode}"] = int(day.get(f"audio_ms_{mode}", 0) or 0) + audio_ms
            day["chars"] += chars
            self._save()
        except Exception as e:
            logging.error(f"usage record failed: {e}")

    @staticmethod
    def _cost(batch_ms: int, realtime_ms: int,
              cost_batch: float, cost_realtime: float) -> float:
        return (batch_ms / 3_600_000.0) * cost_batch + \
               (realtime_ms / 3_600_000.0) * cost_realtime

    def summary(self, cost_batch: float = DEFAULT_COST_BATCH,
                cost_realtime: float = DEFAULT_COST_REALTIME) -> dict:
        today = date.today().isoformat()
        d = self._data["days"].get(today, {})
        tb = int(d.get("audio_ms_batch", 0) or 0)
        tr = int(d.get("audio_ms_realtime", 0) or 0)
        TB = self._data["total_audio_ms_batch"]
        TR = self._data["total_audio_ms_realtime"]
        return {
            "today_requests": int(d.get("requests", 0) or 0),
            "today_min": (tb + tr) / 60000.0,
            "today_min_batch": tb / 60000.0,
            "today_min_realtime": tr / 60000.0,
            "today_cost": self._cost(tb, tr, cost_batch, cost_realtime),
            "total_requests": self._data["total_requests"],
            "total_min": self._data["total_audio_ms"] / 60000.0,
            "total_min_batch": TB / 60000.0,
            "total_min_realtime": TR / 60000.0,
            "total_cost": self._cost(TB, TR, cost_batch, cost_realtime),
            "total_chars": self._data["total_chars"],
        }

    def month_summary(self, cost_batch: float = DEFAULT_COST_BATCH,
                      cost_realtime: float = DEFAULT_COST_REALTIME) -> dict:
        """Aggregate usage for the current calendar month so far (LOCAL date)."""
        prefix = date.today().strftime("%Y-%m-")
        m_requests = 0
        m_batch = 0
        m_realtime = 0
        for day_key, day in self._data["days"].items():
            if not day_key.startswith(prefix):
                continue
            m_requests += int(day.get("requests", 0) or 0)
            m_batch += int(day.get("audio_ms_batch", 0) or 0)
            m_realtime += int(day.get("audio_ms_realtime", 0) or 0)
        return {
            "month_requests": m_requests,
            "month_min": (m_batch + m_realtime) / 60000.0,
            "month_min_batch": m_batch / 60000.0,
            "month_min_realtime": m_realtime / 60000.0,
            "month_cost": self._cost(m_batch, m_realtime, cost_batch, cost_realtime),
        }
