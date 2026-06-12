"""Tests for TextPolisher repeated-failure surfacing (roadmap A5).

Polish is best-effort and returns the original text on any error. The field bug:
Anthropic "credit balance too low" failed 16× in a row over a day and text was
pasted un-polished with zero signal. Now: after 2 consecutive failures we fire
on_repeated_failure(reason) exactly once; the first success fires on_recovered
and resets the counter. API calls that merely return empty/odd text are NOT
failures (the service worked).
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.text_polisher import TextPolisher


class FakeResponse:
    def __init__(self, status_code, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {
            "content": [{"type": "text", "text": "очищенный текст"}]
        }

    def json(self):
        return self._payload


_CREDIT_BODY = ('{"type":"error","error":{"type":"invalid_request_error",'
                '"message":"Your credit balance is too low to access the '
                'Anthropic API."}}')

_LONG_INPUT = "это достаточно длинный надиктованный фрагмент текста для полировки"


def _polisher_with_post(post_mock):
    p = TextPolisher(api_key="test-key")
    http = MagicMock()
    http.post = post_mock
    http.is_closed = False
    p._client = http
    return p


class PolishFailureTests(unittest.TestCase):
    def test_single_failure_does_not_notify(self):
        p = _polisher_with_post(MagicMock(return_value=FakeResponse(400, _CREDIT_BODY)))
        on_fail = MagicMock()
        p.on_repeated_failure = on_fail
        out = p.polish(_LONG_INPUT)
        self.assertEqual(out, _LONG_INPUT)  # original returned
        on_fail.assert_not_called()
        self.assertEqual(p._consecutive_failures, 1)

    def test_two_failures_notify_once_with_credit_reason(self):
        p = _polisher_with_post(MagicMock(return_value=FakeResponse(400, _CREDIT_BODY)))
        on_fail = MagicMock()
        p.on_repeated_failure = on_fail
        p.polish(_LONG_INPUT)
        p.polish(_LONG_INPUT)
        p.polish(_LONG_INPUT)  # third failure must NOT notify again
        on_fail.assert_called_once()
        reason = on_fail.call_args[0][0]
        self.assertIn("кредит", reason.lower())

    def test_recovery_resets_and_can_notify_again(self):
        responses = [
            FakeResponse(400, _CREDIT_BODY),   # fail 1
            FakeResponse(400, _CREDIT_BODY),   # fail 2 -> notify
            FakeResponse(200),                 # success -> recovered
            FakeResponse(500, "server error"), # fail 1 (new run)
            FakeResponse(500, "server error"), # fail 2 -> notify again
        ]
        p = _polisher_with_post(MagicMock(side_effect=responses))
        on_fail = MagicMock()
        on_recovered = MagicMock()
        p.on_repeated_failure = on_fail
        p.on_recovered = on_recovered
        for _ in range(5):
            p.polish(_LONG_INPUT)
        self.assertEqual(on_fail.call_count, 2)
        on_recovered.assert_called_once()
        # After the success the counter reset, so the 4th/5th calls rebuilt it.
        self.assertTrue(p._failure_notified)

    def test_successful_polish_returns_cleaned_and_no_failure(self):
        p = _polisher_with_post(MagicMock(return_value=FakeResponse(200)))
        on_fail = MagicMock()
        p.on_repeated_failure = on_fail
        out = p.polish(_LONG_INPUT)
        self.assertEqual(out, "очищенный текст")
        on_fail.assert_not_called()
        self.assertEqual(p._consecutive_failures, 0)

    def test_network_exception_counts_as_failure(self):
        import httpx
        post = MagicMock(side_effect=httpx.ConnectError("offline"))
        p = _polisher_with_post(post)
        on_fail = MagicMock()
        p.on_repeated_failure = on_fail
        p.polish(_LONG_INPUT)
        p.polish(_LONG_INPUT)
        on_fail.assert_called_once()
        self.assertIn("связи", on_fail.call_args[0][0].lower())

    def test_reason_classification(self):
        self.assertIn("кредит", TextPolisher._reason_from_status(400, _CREDIT_BODY).lower())
        self.assertIn("ключ", TextPolisher._reason_from_status(401, "unauthorized").lower())
        self.assertIn("429", TextPolisher._reason_from_status(429, "rate limited"))


if __name__ == "__main__":
    unittest.main()
