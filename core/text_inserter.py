"""
Text Inserter - Inserts text at cursor position
Uses Windows API for reliable key simulation
"""
import time
import ctypes
from ctypes import wintypes
import pyperclip

# Windows API constants
VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002

# Load user32.dll
user32 = ctypes.windll.user32


class TextInserter:
    def __init__(self):
        pass

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

    def insert_text(self, text: str) -> bool:
        """
        Insert text at current cursor position using clipboard paste

        Returns:
            True if successful, False otherwise
        """
        if not text:
            return False

        try:
            # Copy text to clipboard
            pyperclip.copy(text)

            # Small delay to ensure clipboard is updated
            time.sleep(0.1)

            # Simulate Ctrl+V to paste
            self._paste()

            # Small delay after paste
            time.sleep(0.05)

            return True

        except Exception as e:
            print(f"Error inserting text: {e}")
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
