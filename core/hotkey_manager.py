"""
Hotkey Manager - Global hotkey handling for recording
Uses virtual key codes for language-independent operation
"""
import threading
from typing import Callable, Optional, Set, List

from pynput import keyboard


# Default hotkey VK codes: Ctrl + Shift + Space
DEFAULT_HOTKEY_VKS = {162, 160, 32}  # Left Ctrl, Left Shift, Space

# Modifier VK codes (left and right variants treated as same)
MODIFIER_VKS = {
    162, 163,  # Ctrl
    160, 161,  # Shift
    164, 165,  # Alt
    91, 92,    # Win
}


class HotkeyManager:
    def __init__(
        self,
        on_press_callback: Callable[[], None],
        on_release_callback: Callable[[], None],
        hotkey_vks: Optional[Set[int]] = None
    ):
        """
        Initialize hotkey manager

        Args:
            on_press_callback: Called when hotkey is pressed
            on_release_callback: Called when hotkey is released
            hotkey_vks: Set of virtual key codes for hotkey
        """
        self.on_press_callback = on_press_callback
        self.on_release_callback = on_release_callback
        self.hotkey_vks = hotkey_vks or DEFAULT_HOTKEY_VKS.copy()

        self._current_vks: Set[int] = set()
        self._is_hotkey_active = False
        self._listener: Optional[keyboard.Listener] = None
        self._lock = threading.Lock()

    def _get_vk_from_key(self, key) -> Optional[int]:
        """Extract virtual key code from pynput key"""
        # Try to get vk directly
        if hasattr(key, 'vk') and key.vk is not None:
            return key.vk
        if hasattr(key, 'value') and hasattr(key.value, 'vk'):
            return key.value.vk

        # Handle special keys by mapping
        if isinstance(key, keyboard.Key):
            key_vk_map = {
                keyboard.Key.ctrl_l: 162, keyboard.Key.ctrl_r: 163,
                keyboard.Key.shift: 160, keyboard.Key.shift_l: 160, keyboard.Key.shift_r: 161,
                keyboard.Key.alt_l: 164, keyboard.Key.alt_r: 165,
                keyboard.Key.cmd: 91, keyboard.Key.cmd_l: 91, keyboard.Key.cmd_r: 92,
                keyboard.Key.space: 32,
                keyboard.Key.enter: 13,
                keyboard.Key.tab: 9,
                keyboard.Key.esc: 27,
                keyboard.Key.backspace: 8,
                keyboard.Key.delete: 46,
                keyboard.Key.home: 36,
                keyboard.Key.end: 35,
                keyboard.Key.page_up: 33,
                keyboard.Key.page_down: 34,
                keyboard.Key.left: 37, keyboard.Key.up: 38,
                keyboard.Key.right: 39, keyboard.Key.down: 40,
                keyboard.Key.f1: 112, keyboard.Key.f2: 113, keyboard.Key.f3: 114,
                keyboard.Key.f4: 115, keyboard.Key.f5: 116, keyboard.Key.f6: 117,
                keyboard.Key.f7: 118, keyboard.Key.f8: 119, keyboard.Key.f9: 120,
                keyboard.Key.f10: 121, keyboard.Key.f11: 122, keyboard.Key.f12: 123,
            }
            return key_vk_map.get(key)

        return None

    def _normalize_vk(self, vk: int) -> int:
        """Normalize VK code (treat left/right modifiers as same)"""
        # Right Ctrl -> Left Ctrl
        if vk == 163:
            return 162
        # Right Shift -> Left Shift
        if vk == 161:
            return 160
        # Right Alt -> Left Alt
        if vk == 165:
            return 164
        # Right Win -> Left Win
        if vk == 92:
            return 91
        return vk

    def _check_hotkey_match(self) -> bool:
        """Check if current keys match the hotkey combination"""
        # Normalize both sets for comparison
        current_normalized = {self._normalize_vk(vk) for vk in self._current_vks}
        hotkey_normalized = {self._normalize_vk(vk) for vk in self.hotkey_vks}
        return hotkey_normalized.issubset(current_normalized)

    def _on_press(self, key):
        """Handle key press"""
        vk = self._get_vk_from_key(key)
        if vk is None:
            return

        with self._lock:
            self._current_vks.add(vk)

            # Check if hotkey combination is pressed
            if not self._is_hotkey_active and self._check_hotkey_match():
                self._is_hotkey_active = True
                # Run callback in separate thread to not block listener
                threading.Thread(target=self.on_press_callback, daemon=True).start()

    def _on_release(self, key):
        """Handle key release"""
        vk = self._get_vk_from_key(key)
        if vk is None:
            return

        with self._lock:
            # Check if we should trigger release callback
            if self._is_hotkey_active:
                # Normalize for comparison
                normalized_vk = self._normalize_vk(vk)
                hotkey_normalized = {self._normalize_vk(v) for v in self.hotkey_vks}

                # If any key from hotkey combination is released
                if normalized_vk in hotkey_normalized:
                    self._is_hotkey_active = False
                    # Run callback in separate thread
                    threading.Thread(target=self.on_release_callback, daemon=True).start()

            # Remove key from current keys
            self._current_vks.discard(vk)

    def start(self):
        """Start listening for hotkeys"""
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release
        )
        self._listener.start()

    def stop(self):
        """Stop listening for hotkeys"""
        if self._listener:
            self._listener.stop()
            self._listener = None

    def is_running(self) -> bool:
        """Check if listener is running"""
        return self._listener is not None and self._listener.is_alive()

    def set_hotkey(self, vk_codes: List[int]):
        """Update hotkey combination using VK codes"""
        with self._lock:
            self.hotkey_vks = set(vk_codes)
            self._current_vks.clear()
            self._is_hotkey_active = False
