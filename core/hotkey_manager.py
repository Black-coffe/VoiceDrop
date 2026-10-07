"""
Hotkey Manager - Global hotkey handling for recording
Uses virtual key codes for language-independent operation
"""
import logging
import queue
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

# Right-hand modifier VKs. If a hotkey intentionally uses any of these, we match
# EXACTLY (no left/right merging) so e.g. "Right Ctrl" never fires on the
# constantly-used Left Ctrl. Hotkeys without a right modifier keep flexible L/R
# matching (so Ctrl+Shift+Space still works with either side).
RIGHT_MODIFIER_VKS = {163, 161, 165, 92}

# A callback still running after this is treated as hung (D8): the dispatcher
# stops waiting, reports it via on_hang and moves on, so a mic wedged in the
# driver can't silently swallow every later press. Press covers the A4 open
# retries (~2.6 s backoff + opens) and the first rt-loop start (<=2 s);
# release normally takes ~0.1 s.
PRESS_HANG_TIMEOUT_SEC = 8.0
RELEASE_HANG_TIMEOUT_SEC = 4.0


class HotkeyManager:
    def __init__(
        self,
        on_press_callback: Callable[[], None],
        on_release_callback: Callable[[], None],
        hotkey_vks: Optional[Set[int]] = None,
        modifier_vks: Optional[Set[int]] = None,
        on_hang: Optional[Callable[[str], None]] = None,
        press_timeout_sec: float = PRESS_HANG_TIMEOUT_SEC,
        release_timeout_sec: float = RELEASE_HANG_TIMEOUT_SEC,
    ):
        """
        Initialize hotkey manager

        Args:
            on_press_callback: Called when hotkey is pressed
            on_release_callback: Called when hotkey is released
            hotkey_vks: Set of virtual key codes for hotkey
            modifier_vks: Extra "mode" keys; if any are held during the active
                window, modifier_was_held() returns True (e.g. Right Shift =
                dictate this clip in code mode). Not part of the trigger itself.
            on_hang: Called with the callback's name when a press/release
                callback outlives its timeout and the dispatcher moves on.
        """
        self.on_press_callback = on_press_callback
        self.on_release_callback = on_release_callback
        self.hotkey_vks = hotkey_vks or DEFAULT_HOTKEY_VKS.copy()
        self._modifier_vks: Set[int] = set(modifier_vks) if modifier_vks else set()

        self._current_vks: Set[int] = set()
        self._is_hotkey_active = False
        self._modifier_latched = False  # was a modifier key held during this activation
        self._listener: Optional[keyboard.Listener] = None
        self._lock = threading.Lock()

        # Press/release callbacks run IN ORDER on one dispatch thread (D8).
        # They used to get a fresh thread each, so a chord bounce (press →
        # release ~150 ms → press) let the release run while the press was
        # still opening the mic: two InputStreams ended up open, one orphaned,
        # and the next GC freed its callback under PortAudio → 0xc0000005.
        self._dispatch_queue: Optional[queue.Queue] = None
        self._dispatch_thread: Optional[threading.Thread] = None
        self.on_hang = on_hang
        self._press_timeout_sec = press_timeout_sec
        self._release_timeout_sec = release_timeout_sec

    def _dispatch(self, callback: Callable[[], None], timeout: float):
        """Queue a callback for the dispatch thread (started lazily).

        Must be called with self._lock held. The listener never blocks, and
        callbacks never overlap: a release waits until its press is done —
        unless the press hangs past its timeout (see _dispatch_loop).
        """
        if self._dispatch_thread is None:
            self._dispatch_queue = queue.Queue()
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_loop,
                args=(self._dispatch_queue,),
                daemon=True,
                name="hotkey-dispatch",
            )
            self._dispatch_thread.start()
        self._dispatch_queue.put((callback, timeout))

    def _dispatch_loop(self, q: queue.Queue):
        while True:
            item = q.get()
            if item is None:  # stop() sentinel
                return
            callback, timeout = item
            # Each callback runs on its own worker so a hung one (mic stuck in
            # Pa_OpenStream / stop()) can't block the queue forever. Normal
            # callbacks finish well inside the timeout, so order is unchanged.
            worker = threading.Thread(
                target=self._run_callback, args=(callback,),
                daemon=True, name="hotkey-callback",
            )
            worker.start()
            worker.join(timeout)
            if worker.is_alive():
                name = getattr(callback, "__name__", repr(callback))
                logging.error(
                    f"Hotkey callback {name} still running after {timeout:g} s "
                    f"— treating it as hung and moving on"
                )
                if self.on_hang is not None:
                    try:
                        self.on_hang(name)
                    except Exception as e:
                        logging.error(f"on_hang failed: {e}", exc_info=True)

    @staticmethod
    def _run_callback(callback: Callable[[], None]):
        try:
            callback()
        except Exception as e:
            # Logged here so a failing callback never takes the dispatcher down.
            logging.error(f"Hotkey callback failed: {e}", exc_info=True)

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

    def _exact_match_mode(self) -> bool:
        """Match the hotkey exactly (no L/R normalization) when it intentionally
        uses a right-hand modifier — so a right-side PTT key isn't triggered by
        its left-side twin used for normal shortcuts."""
        return any(vk in RIGHT_MODIFIER_VKS for vk in self.hotkey_vks)

    def _check_hotkey_match(self) -> bool:
        """Check if current keys match the hotkey combination"""
        if self._exact_match_mode():
            return self.hotkey_vks.issubset(self._current_vks)
        # Normalize both sets for comparison (flexible left/right)
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
                # Latch whether a "mode" modifier is already held at activation
                self._modifier_latched = bool(self._modifier_vks & self._current_vks)
                # Off the listener thread, but ordered after any earlier release
                self._dispatch(self.on_press_callback, self._press_timeout_sec)
            elif self._is_hotkey_active and vk in self._modifier_vks:
                # Modifier added mid-recording -> latch it for this clip
                self._modifier_latched = True

    def _on_release(self, key):
        """Handle key release"""
        vk = self._get_vk_from_key(key)
        if vk is None:
            return

        with self._lock:
            # Check if we should trigger release callback
            if self._is_hotkey_active:
                # If any key from the hotkey combination is released, stop.
                if self._exact_match_mode():
                    released_in_hotkey = vk in self.hotkey_vks
                else:
                    normalized_vk = self._normalize_vk(vk)
                    hotkey_normalized = {self._normalize_vk(v) for v in self.hotkey_vks}
                    released_in_hotkey = normalized_vk in hotkey_normalized

                if released_in_hotkey:
                    self._is_hotkey_active = False
                    # Runs only after the press callback has finished
                    self._dispatch(self.on_release_callback, self._release_timeout_sec)

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
        with self._lock:
            if self._dispatch_thread is not None:
                self._dispatch_queue.put(None)  # finish queued callbacks, then exit
                self._dispatch_thread = None
                self._dispatch_queue = None

    def is_running(self) -> bool:
        """Check if listener is running"""
        return self._listener is not None and self._listener.is_alive()

    def modifier_was_held(self) -> bool:
        """True if a mode-modifier key was held during the current/last activation."""
        return self._modifier_latched

    def set_hotkey(self, vk_codes: List[int]):
        """Update hotkey combination using VK codes"""
        with self._lock:
            self.hotkey_vks = set(vk_codes)
            self._current_vks.clear()
            self._is_hotkey_active = False
