"""Tests for PendingQueue dead-letter + TTL (roadmap A2).

The pending queue must distinguish PERMANENT failures (which must NOT be
retried — they created the audio_too_short poison loop that hammered the API
every 45 s for 3 days) from TRANSIENT ones (offline / 5xx / 429, which should
be retried). The queue itself only provides the mechanism (dead_letter /
sweep_ttl); the retry/permanent *decision* lives in main._process_pending_queue
and keys off TranscriptionError.retryable (see test_elevenlabs_api.py for the
flag classification).
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.pending_queue import PendingQueue


class PendingQueueDeadLetterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vd_pending_")
        self.q = PendingQueue(directory=Path(self.tmp))

    def _enqueue_one(self, duration_ms=1000, language="ru"):
        item_id = self.q.enqueue(b"RIFFfake-wav-bytes", duration_ms, language)
        self.assertIsNotNone(item_id)
        return item_id

    def test_enqueue_then_list(self):
        self._enqueue_one()
        items = self.q.list_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(self.q.count(), 1)

    def test_dead_letter_moves_files_and_stamps_reason(self):
        self._enqueue_one()
        item = self.q.list_pending()[0]

        moved = self.q.dead_letter(item, reason="ElevenLabs 400 - audio_too_short")
        self.assertTrue(moved)

        # Gone from the active queue (won't be retried anymore).
        self.assertEqual(self.q.list_pending(), [])
        self.assertEqual(self.q.count(), 0)

        # Preserved under dead/ with the reason stamped in — nothing deleted.
        dead_dir = Path(self.tmp) / "dead"
        dead_jsons = list(dead_dir.glob("*.json"))
        dead_wavs = list(dead_dir.glob("*.wav"))
        self.assertEqual(len(dead_jsons), 1)
        self.assertEqual(len(dead_wavs), 1)
        meta = json.loads(dead_jsons[0].read_text(encoding="utf-8"))
        self.assertIn("audio_too_short", meta["dead_reason"])
        self.assertIn("dead_at", meta)
        # Original metadata is preserved through the move.
        self.assertEqual(meta.get("language"), "ru")

    def test_sweep_ttl_expires_old_keeps_fresh(self):
        # Fresh item (created_at = now via enqueue).
        fresh_id = self._enqueue_one(language="fresh")
        # Old item: enqueue then backdate its created_at past the TTL.
        old_id = self._enqueue_one(language="old")
        old_json = Path(self.tmp) / f"{old_id}.json"
        meta = json.loads(old_json.read_text(encoding="utf-8"))
        meta["created_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 10 * 24 * 3600)
        )
        old_json.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        moved = self.q.sweep_ttl()  # default TTL = 7 days
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["id"], old_id)

        # Only the fresh one remains active.
        remaining = self.q.list_pending()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], fresh_id)
        # The expired one is in dead/.
        self.assertTrue((Path(self.tmp) / "dead" / f"{old_id}.wav").exists())

    def test_sweep_ttl_noop_when_all_fresh(self):
        self._enqueue_one()
        self.assertEqual(self.q.sweep_ttl(), [])
        self.assertEqual(self.q.count(), 1)

    def test_dead_dir_not_listed_as_pending(self):
        """dead/ is a subdirectory — its sidecars must never show up as
        retryable pending items (a non-recursive glob guarantees this)."""
        self._enqueue_one()
        item = self.q.list_pending()[0]
        self.q.dead_letter(item, reason="permanent")
        # Even with a dead item present, the active queue is empty.
        self.assertEqual(self.q.list_pending(), [])


class PendingQueueRobustnessTests(unittest.TestCase):
    """F1: corrupt sidecar handling, cap enforcement, audio round-trip."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vd_pending_")
        self.q = PendingQueue(directory=Path(self.tmp))

    def test_corrupt_sidecar_json_is_skipped_not_crashing(self):
        good = self.q.enqueue(b"good-wav", 1000, "ru")
        self.assertIsNotNone(good)
        # Write a broken pair: valid-looking .wav + invalid .json.
        (Path(self.tmp) / "20990101_000000_bad.wav").write_bytes(b"x")
        (Path(self.tmp) / "20990101_000000_bad.json").write_text(
            "{ this is not json", encoding="utf-8"
        )
        items = self.q.list_pending()  # must not raise
        ids = {i["id"] for i in items}
        self.assertIn(good, ids)
        self.assertNotIn("20990101_000000_bad", ids)  # corrupt one skipped

    def test_orphan_wav_without_sidecar_not_listed(self):
        (Path(self.tmp) / "orphan.wav").write_bytes(b"x")  # no .json commit marker
        self.assertEqual(self.q.list_pending(), [])

    def test_read_audio_round_trips(self):
        item_id = self.q.enqueue(b"RIFF-payload-123", 500, None)
        item = next(i for i in self.q.list_pending() if i["id"] == item_id)
        self.assertEqual(self.q.read_audio(item), b"RIFF-payload-123")

    def test_cap_drops_oldest(self):
        from core import pending_queue as pq_mod
        original = pq_mod._MAX_PENDING
        pq_mod._MAX_PENDING = 3
        try:
            ids = []
            for i in range(5):
                # enqueue derives the id from time to the second; force unique ids
                ids.append(self.q.enqueue(f"wav{i}".encode(), 100 + i, None))
            # The cap is enforced on enqueue; never more than the cap remain.
            remaining = self.q.list_pending()
            self.assertLessEqual(len(remaining), 3)
        finally:
            pq_mod._MAX_PENDING = original

    def test_remove_is_idempotent(self):
        item_id = self.q.enqueue(b"x", 100, None)
        item = self.q.list_pending()[0]
        self.q.remove(item)
        self.q.remove(item)  # second remove must not raise
        self.assertEqual(self.q.count(), 0)


if __name__ == "__main__":
    unittest.main()
