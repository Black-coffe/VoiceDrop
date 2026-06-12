"""Tests for the KeyTerms loader (roadmap B2).

keyterms.json is hot-reloaded (content compare) and sanitized to ElevenLabs'
rules before being sent: drop unsupported chars (< > { } [ ] \\), at most 5
words per term, length cap (batch 50 / realtime 20), de-dupe, and cap the count.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.keyterms import KeyTerms


def _kt(payload) -> KeyTerms:
    p = Path(tempfile.mkdtemp(prefix="vd_kt_")) / "keyterms.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return KeyTerms(path=p)


class KeyTermsTests(unittest.TestCase):
    def test_seeds_defaults_when_missing(self):
        p = Path(tempfile.mkdtemp(prefix="vd_kt_")) / "keyterms.json"
        kt = KeyTerms(path=p)
        self.assertTrue(p.exists())  # seeded
        terms = kt.get(max_terms=1000, max_len=50)
        self.assertIn("VoiceDrop", terms)

    def test_strips_unsupported_chars(self):
        kt = _kt({"keyterms": ["Voice<Drop>", "a[b]c", "ok\\term"]})
        terms = kt.get(max_terms=1000, max_len=50)
        self.assertIn("VoiceDrop", terms)
        self.assertIn("abc", terms)
        self.assertIn("okterm", terms)
        for t in terms:
            self.assertNotRegex(t, r"[<>{}\[\]\\]")

    def test_caps_five_words(self):
        kt = _kt({"keyterms": ["one two three four five six seven"]})
        terms = kt.get(max_terms=1000, max_len=50)
        self.assertEqual(terms, ["one two three four five"])

    def test_realtime_length_and_count_limits(self):
        long_term = "оченьдлинныйтерминкоторыйточнопревышаетлимит"  # >20 chars
        kt = _kt({"keyterms": [long_term] + [f"t{i}" for i in range(60)]})
        terms = kt.get(max_terms=50, max_len=20)
        self.assertLessEqual(len(terms), 50)
        self.assertTrue(all(len(t) <= 20 for t in terms))

    def test_dedupe_case_insensitive(self):
        kt = _kt({"keyterms": ["Scribe", "scribe", "SCRIBE", "Anthropic"]})
        terms = kt.get(max_terms=1000, max_len=50)
        lowers = [t.lower() for t in terms]
        self.assertEqual(len(lowers), len(set(lowers)))
        self.assertEqual(sum(1 for t in terms if t.lower() == "scribe"), 1)

    def test_accepts_bare_list_json(self):
        kt = _kt(["Alpha", "Beta"])
        terms = kt.get(max_terms=1000, max_len=50)
        self.assertEqual(set(terms), {"Alpha", "Beta"})

    def test_empty_after_sanitize_dropped(self):
        kt = _kt({"keyterms": ["<>", "   ", "[]"]})
        self.assertEqual(kt.get(max_terms=1000, max_len=50), [])

    def test_hot_reload_on_content_change(self):
        p = Path(tempfile.mkdtemp(prefix="vd_kt_")) / "keyterms.json"
        p.write_text(json.dumps({"keyterms": ["First"]}), encoding="utf-8")
        kt = KeyTerms(path=p)
        self.assertEqual(kt.get(1000, 50), ["First"])
        p.write_text(json.dumps({"keyterms": ["Second", "Third"]}), encoding="utf-8")
        self.assertEqual(set(kt.get(1000, 50)), {"Second", "Third"})


if __name__ == "__main__":
    unittest.main()
