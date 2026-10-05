"""Tests for AudioMuter mute / unmute / force_unmute (roadmap D2 + F coverage).

force_unmute_all is the startup safety net: if a previous instance was killed
mid-recording, the non-whitelisted apps it muted stay muted forever. On launch
we must unmute exactly those (muted, non-whitelisted) and leave whitelisted /
already-unmuted sessions untouched.

pycaw is mocked — no real Windows audio sessions are touched.
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio_muter import AudioMuter


class FakeVolume:
    def __init__(self, muted=False, master=0.8):
        self._muted = muted
        self._master = master
        self.set_calls = []

    def GetMute(self):
        return self._muted

    def SetMute(self, value, _ctx):
        self._muted = bool(value)
        self.set_calls.append(bool(value))

    def GetMasterVolume(self):
        return self._master


class _Ctl:
    def __init__(self, volume):
        self._volume = volume

    def QueryInterface(self, _iface):
        return self._volume


class FakeProcess:
    def __init__(self, pid, name):
        self.pid = pid
        self._name = name

    def name(self):
        return self._name


class FakeSession:
    def __init__(self, pid, name, muted=False):
        self.Process = FakeProcess(pid, name)
        self.volume = FakeVolume(muted=muted)
        self._ctl = _Ctl(self.volume)


def _muter_with_whitelist(whitelist):
    m = AudioMuter()
    # Don't touch the real mute_whitelist.json during tests.
    m._whitelist = set(whitelist)
    m._load_whitelist = lambda: None
    return m


class ForceUnmuteTests(unittest.TestCase):
    def test_unmutes_muted_nonwhitelisted_skips_whitelisted(self):
        game = FakeSession(100, "game.exe", muted=True)        # we muted this
        chrome = FakeSession(200, "chrome.exe", muted=True)    # whitelisted: leave
        idle = FakeSession(300, "player.exe", muted=False)     # not muted: leave
        m = _muter_with_whitelist({"chrome.exe"})
        with patch("core.audio_muter.AudioUtilities.GetAllSessions",
                   return_value=[game, chrome, idle]):
            ok = m.force_unmute_all()
        self.assertTrue(ok)
        self.assertFalse(game.volume._muted)        # unmuted
        self.assertTrue(chrome.volume._muted)       # untouched (whitelisted)
        self.assertEqual(chrome.volume.set_calls, [])
        self.assertEqual(idle.volume.set_calls, [])  # untouched (already unmuted)

    def test_returns_true_with_no_sessions(self):
        m = _muter_with_whitelist(set())
        with patch("core.audio_muter.AudioUtilities.GetAllSessions", return_value=[]):
            self.assertTrue(m.force_unmute_all())


class MuteUnmuteTests(unittest.TestCase):
    def test_mute_all_mutes_nonwhitelisted_only(self):
        game = FakeSession(100, "game.exe", muted=False)
        chrome = FakeSession(200, "chrome.exe", muted=False)
        m = _muter_with_whitelist({"chrome.exe"})
        with patch("core.audio_muter.AudioUtilities.GetAllSessions",
                   return_value=[game, chrome]):
            ok = m.mute_all()
        self.assertTrue(ok)
        self.assertTrue(game.volume._muted)      # muted
        self.assertFalse(chrome.volume._muted)   # whitelisted: not muted
        self.assertTrue(m.is_muted)

    def test_unmute_all_restores_saved_state(self):
        game = FakeSession(100, "game.exe", muted=False)
        m = _muter_with_whitelist(set())
        with patch("core.audio_muter.AudioUtilities.GetAllSessions",
                   return_value=[game]):
            m.mute_all()
            self.assertTrue(game.volume._muted)
            m.unmute_all()
        self.assertFalse(game.volume._muted)     # restored to original (unmuted)
        self.assertFalse(m.is_muted)


class ComThreadTests(unittest.TestCase):
    """All pycaw/COM work must run on ONE long-lived thread, whatever thread
    the caller is on — pointers released from other threads/apartments
    crashed the app with 0xc0000005 (2026-10-05)."""

    def test_calls_from_many_threads_run_on_one_com_thread(self):
        seen = []

        def sessions():
            seen.append(threading.current_thread())
            return [FakeSession(100, "game.exe")]

        m = _muter_with_whitelist(set())
        with patch("core.audio_muter.AudioUtilities.GetAllSessions",
                   side_effect=sessions):
            m.force_unmute_all()                      # from the test thread
            for fn in (m.mute_all, m.unmute_all):     # from short-lived threads
                t = threading.Thread(target=fn)
                t.start()
                t.join()
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(seen[0].name, "audio-muter-com")
        self.assertIsNot(seen[0], threading.current_thread())

    def test_timed_out_mute_still_lands_before_queued_unmute(self):
        game = FakeSession(100, "game.exe", muted=False)
        gate = threading.Event()
        calls = []

        def sessions():
            calls.append(len(calls))
            if len(calls) == 1:
                gate.wait(5)  # first call (mute) is "slow"
            return [game]

        m = _muter_with_whitelist(set())
        with patch("core.audio_muter._COM_CALL_TIMEOUT_SEC", 0.05),              patch("core.audio_muter.AudioUtilities.GetAllSessions",
                   side_effect=sessions):
            self.assertFalse(m.mute_all())  # timed out, still queued
            unmute = threading.Thread(target=m.unmute_all)
            unmute.start()
            gate.set()
            unmute.join(5)
        self.assertEqual(game.volume.set_calls, [True, False])
        self.assertFalse(game.volume._muted)
        self.assertFalse(m.is_muted)


if __name__ == "__main__":
    unittest.main()
