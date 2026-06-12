"""
Text Inserter - Inserts text at cursor position
Uses Windows API for reliable key simulation
"""
import logging
import time
import ctypes
from ctypes import wintypes
import pyperclip

# Windows API constants
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002

# Load user32.dll
user32 = ctypes.windll.user32


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class TextInserter:
    def __init__(self):
        pass

    def _has_paste_target(self) -> bool:
        """True if the foreground window has a control with keyboard focus —
        i.e. somewhere Ctrl+V can actually land (D1).

        If there's no foreground window or no focused control, a paste would be
        silently lost (read-only context, desktop, a window that took no focus),
        so the caller keeps the text in the clipboard and tells the user instead
        of losing it. Best-effort: on any API hiccup we assume a target exists
        (never block a paste that might have worked).
        """
        try:
            fg = user32.GetForegroundWindow()
            if not fg:
                return False
            tid = user32.GetWindowThreadProcessId(fg, None)
            gti = _GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                return bool(gti.hwndFocus)
            # API failed — don't block the paste.
            return True
        except Exception as e:
            logging.debug(f"paste-target check failed (assuming ok): {e}")
            return True

    def _send_key(self, vk_code: int, key_up: bool = False):
        """Send a key event using Windows API"""
        flags = KEYEVENTF_KEYUP if key_up else 0
        user32.keybd_event(vk_code, 0, flags, 0)

    def _paste(self):
        """Simulate Ctrl+V using Windows API"""
        # Press Ctrl
        self._send_key(VK_CONTROL, key_up=False)
        time.sleep(0.02)

        # Press V
        self._send_key(VK_V, key_up=False)
        time.sleep(0.02)

        # Release V
        self._send_key(VK_V, key_up=True)
        time.sleep(0.02)

        # Release Ctrl
        self._send_key(VK_CONTROL, key_up=True)

    def insert_text(self, text: str, press_enter: bool = False) -> bool:
        """
        Insert text at current cursor position using clipboard paste.

        Args:
            text: text to paste
            press_enter: send Enter after pasting (auto-send in chats)

        Returns:
            True if the text was pasted into a focused field; False if there was
            no paste target (text is left in the clipboard for manual paste) or
            on error. The caller surfaces the False case to the user (D1).
        """
        if not text:
            return False

        try:
            # Copy text to clipboard FIRST — so even if there's no paste target,
            # the text is safe and the user can Ctrl+V it manually.
            pyperclip.copy(text)

            # Small delay to ensure clipboard is updated
            time.sleep(0.1)

            # D1: if nothing is focused, Ctrl+V would be silently lost. Don't
            # send it (no point, and it avoids any chance of a stray paste);
            # report failure so the caller notifies the user. The text is
            # already in the clipboard above.
            if not self._has_paste_target():
                logging.info("No paste target (no focused field) — text left in clipboard")
                return False

            # Simulate Ctrl+V to paste
            self._paste()

            # Small delay after paste
            time.sleep(0.05)

            if press_enter:
                time.sleep(0.05)
                self._send_key(VK_RETURN, key_up=False)
                time.sleep(0.02)
                self._send_key(VK_RETURN, key_up=True)

            return True

        except Exception as e:
            logging.error(f"Error inserting text: {e}", exc_info=True)
            return False

    def copy_to_clipboard(self, text: str) -> bool:
        """Copy text to clipboard"""
        try:
            pyperclip.copy(text)
            return True
        except Exception as e:
            print(f"Error copying to clipboard: {e}")
            return False

    def get_clipboard_text(self) -> str:
        """Get current clipboard text"""
        try:
            return pyperclip.paste()
        except Exception:
            return ""
