"""Tests for UsageTracker batch/realtime split + legacy migration (roadmap B4).

Batch and realtime bill at different rates, so minutes are tracked per-mode and
cost is the sum. A usage.json written before the split (only audio_ms, no
per-mode keys) must migrate cleanly with all past minutes counted as BATCH.
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.usage_tracker import UsageTracker


def _tmp_json(payload) -> Path:
    p = Path(tempfile.mkdtemp(prefix="vd_usage_")) / "usage.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


class UsageSplitTests(unittest.TestCase):
    def test_record_splits_by_mode(self):
        p = Path(tempfile.mkdtemp(prefix="vd_usage_")) / "usage.json"
        u = UsageTracker(path=p)
        u.record(60_000, 10, mode="batch")      # 1 min batch
        u.record(120_000, 20, mode="realtime")  # 2 min realtime
        s = u.summary(cost_batch=0.22, cost_realtime=0.39)
        self.assertAlmostEqual(s["total_min_batch"], 1.0, places=3)
        self.assertAlmostEqual(s["total_min_realtime"], 2.0, places=3)
        self.assertAlmostEqual(s["total_min"], 3.0, places=3)
        # cost = 1/60*0.22 + 2/60*0.39
        expected = (60_000/3_600_000)*0.22 + (120_000/3_600_000)*0.39
        self.assertAlmostEqual(s["total_cost"], expected, places=6)
        self.assertEqual(s["total_requests"], 2)

    def test_unknown_mode_defaults_to_batch(self):
        p = Path(tempfile.mkdtemp(prefix="vd_usage_")) / "usage.json"
        u = UsageTracker(path=p)
        u.record(60_000, 5, mode="weird")
        s = u.summary()
        self.assertAlmostEqual(s["total_min_batch"], 1.0, places=3)
        self.assertEqual(s["total_min_realtime"], 0.0)

    def test_persists_and_reloads_split(self):
        p = Path(tempfile.mkdtemp(prefix="vd_usage_")) / "usage.json"
        u = UsageTracker(path=p)
        u.record(60_000, 5, mode="realtime")
        u2 = UsageTracker(path=p)  # reload from disk
        s = u2.summary()
        self.assertAlmostEqual(s["total_min_realtime"], 1.0, places=3)

    def test_legacy_file_migrates_minutes_as_batch(self):
        today = date.today().isoformat()
        legacy = {
            "total_requests": 3,
            "total_audio_ms": 180_000,   # 3 min, no per-mode keys
            "total_chars": 100,
            "days": {today: {"requests": 3, "audio_ms": 180_000, "chars": 100}},
        }
        p = _tmp_json(legacy)
        u = UsageTracker(path=p)
        s = u.summary(cost_batch=0.22, cost_realtime=0.39)
        # All legacy minutes counted as batch — nothing lost, realtime zero.
        self.assertAlmostEqual(s["total_min_batch"], 3.0, places=3)
        self.assertEqual(s["total_min_realtime"], 0.0)
        self.assertAlmostEqual(s["today_min_batch"], 3.0, places=3)
        # Cost uses the batch rate only.
        self.assertAlmostEqual(s["total_cost"], (180_000/3_600_000)*0.22, places=6)

    def test_legacy_then_new_record_accumulates_correctly(self):
        today = date.today().isoformat()
        legacy = {
            "total_requests": 1, "total_audio_ms": 60_000, "total_chars": 10,
            "days": {today: {"requests": 1, "audio_ms": 60_000, "chars": 10}},
        }
        p = _tmp_json(legacy)
        u = UsageTracker(path=p)
        u.record(60_000, 10, mode="realtime")  # add 1 min realtime
        s = u.summary()
        self.assertAlmostEqual(s["total_min_batch"], 1.0, places=3)      # legacy
        self.assertAlmostEqual(s["total_min_realtime"], 1.0, places=3)   # new
        self.assertAlmostEqual(s["today_min"], 2.0, places=3)

    def test_month_summary_split(self):
        p = Path(tempfile.mkdtemp(prefix="vd_usage_")) / "usage.json"
        u = UsageTracker(path=p)
        u.record(60_000, 5, mode="batch")
        u.record(60_000, 5, mode="realtime")
        m = u.month_summary(cost_batch=0.22, cost_realtime=0.39)
        self.assertAlmostEqual(m["month_min_batch"], 1.0, places=3)
        self.assertAlmostEqual(m["month_min_realtime"], 1.0, places=3)
        self.assertAlmostEqual(m["month_min"], 2.0, places=3)


if __name__ == "__main__":
    unittest.main()
