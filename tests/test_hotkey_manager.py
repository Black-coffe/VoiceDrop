"""Tests for HotkeyManager VK matching + state machine (roadmap F2).

The VK logic was complex and never covered: left/right modifier normalization,
EXACT matching when the hotkey intentionally uses a right-hand modifier (so a
right-side PTT key isn't triggered by its constantly-used left twin), the
press/release activation state machine, and the per-clip code-mode modifier
latch (modifier_was_held).

We don't start a real global listener; we drive _on_press/_on_release with fake
key objects (anything exposing a .vk works, matching pynput's contract).
"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.hotkey_manager import HotkeyManager


class FakeKey:
    """Minimal stand-in for a pynput key — _get_vk_from_key reads .vk."""
    def __init__(self, vk):
        self.vk = vk


# VK constants for readability.
LCTRL, RCTRL = 162, 163
LSHIFT, RSHIFT = 160, 161
SPACE = 32


def _mgr(hotkey=None, modifiers=None):
    press = threading.Event()
    release = threading.Event()
    m = HotkeyManager(
        on_press_callback=press.set,
        on_release_callback=release.set,
        hotkey_vks=set(hotkey) if hotkey else None,
        modifier_vks=set(modifiers) if modifiers else None,
    )
    return m, press, release


class NormalizeTests(unittest.TestCase):
    def test_right_modifiers_map_to_left(self):
        m, _, _ = _mgr()
        self.assertEqual(m._normalize_vk(RCTRL), LCTRL)
        self.assertEqual(m._normalize_vk(RSHIFT), LSHIFT)
        self.assertEqual(m._normalize_vk(165), 164)  # alt
        self.assertEqual(m._normalize_vk(92), 91)     # win
        self.assertEqual(m._normalize_vk(SPACE), SPACE)


class MatchModeTests(unittest.TestCase):
    def test_flexible_match_accepts_either_side(self):
        # Default hotkey = Left Ctrl+Shift+Space, but pressing the RIGHT
        # ctrl/shift must still match (no right modifier in the hotkey itself).
        m, press, _ = _mgr(hotkey={LCTRL, LSHIFT, SPACE})
        self.assertFalse(m._exact_match_mode())
        for vk in (RCTRL, RSHIFT, SPACE):
            m._on_press(FakeKey(vk))
        self.assertTrue(press.wait(1.0))
        self.assertTrue(m._is_hotkey_active)

    def test_exact_match_right_hotkey_not_fired_by_left(self):
        # Hotkey = Right Ctrl + Right Shift → exact match; the left twins must
        # NOT trigger it (they're used for normal shortcuts all day).
        m, press, _ = _mgr(hotkey={RCTRL, RSHIFT})
        self.assertTrue(m._exact_match_mode())
        m._on_press(FakeKey(LCTRL))
        m._on_press(FakeKey(LSHIFT))
        self.assertFalse(press.wait(0.2))
        self.assertFalse(m._is_hotkey_active)
        # Now the real right-side combo fires it.
        m._on_press(FakeKey(RCTRL))
        m._on_press(FakeKey(RSHIFT))
        self.assertTrue(press.wait(1.0))
        self.assertTrue(m._is_hotkey_active)


class ActivationTests(unittest.TestCase):
    def test_press_then_release_fires_both(self):
        m, press, release = _mgr(hotkey={LCTRL, LSHIFT, SPACE})
        for vk in (LCTRL, LSHIFT, SPACE):
            m._on_press(FakeKey(vk))
        self.assertTrue(press.wait(1.0))
        # Releasing a key that's part of the hotkey ends the activation.
        m._on_release(FakeKey(SPACE))
        self.assertTrue(release.wait(1.0))
        self.assertFalse(m._is_hotkey_active)

    def test_partial_combo_does_not_fire(self):
        m, press, _ = _mgr(hotkey={LCTRL, LSHIFT, SPACE})
        m._on_press(FakeKey(LCTRL))
        m._on_press(FakeKey(LSHIFT))
        self.assertFalse(press.wait(0.2))  # Space missing
        self.assertFalse(m._is_hotkey_active)

    def test_no_refire_while_held(self):
        m, press, _ = _mgr(hotkey={LCTRL, SPACE})
        m._on_press(FakeKey(LCTRL))
        m._on_press(FakeKey(SPACE))
        self.assertTrue(press.wait(1.0))
        press.clear()
        m._on_press(FakeKey(SPACE))  # repeat key event while already active
        self.assertFalse(press.wait(0.2))


class ModifierLatchTests(unittest.TestCase):
    def test_modifier_held_before_activation_latches(self):
        m, press, _ = _mgr(hotkey={LCTRL, SPACE}, modifiers={RSHIFT})
        m._on_press(FakeKey(RSHIFT))          # mode modifier first
        m._on_press(FakeKey(LCTRL))
        m._on_press(FakeKey(SPACE))
        self.assertTrue(press.wait(1.0))
        self.assertTrue(m.modifier_was_held())

    def test_modifier_added_mid_activation_latches(self):
        m, press, _ = _mgr(hotkey={LCTRL, SPACE}, modifiers={RSHIFT})
        m._on_press(FakeKey(LCTRL))
        m._on_press(FakeKey(SPACE))
        self.assertTrue(press.wait(1.0))
        self.assertFalse(m.modifier_was_held())
        m._on_press(FakeKey(RSHIFT))          # added during recording
        self.assertTrue(m.modifier_was_held())

    def test_no_modifier_not_latched(self):
        m, press, _ = _mgr(hotkey={LCTRL, SPACE}, modifiers={RSHIFT})
        m._on_press(FakeKey(LCTRL))
        m._on_press(FakeKey(SPACE))
        self.assertTrue(press.wait(1.0))
        self.assertFalse(m.modifier_was_held())


class SetHotkeyTests(unittest.TestCase):
    def test_set_hotkey_resets_state(self):
        m, _, _ = _mgr(hotkey={LCTRL, SPACE})
        m._on_press(FakeKey(LCTRL))
        m.set_hotkey([RCTRL, RSHIFT])
        self.assertEqual(m.hotkey_vks, {RCTRL, RSHIFT})
        self.assertEqual(m._current_vks, set())
        self.assertFalse(m._is_hotkey_active)


if __name__ == "__main__":
    unittest.main()
