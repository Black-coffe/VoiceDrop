"""
Windows autostart - run VoiceDrop on login via the HKCU Run key.

Uses the per-user registry key (no admin needed). The value points at the
currently running executable, so it's only meaningful for the frozen exe.
"""
import logging
import sys
import winreg

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "VoiceDrop"


def _command() -> str:
    # Quote the path in case it contains spaces.
    return f'"{sys.executable}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError as e:
        logging.debug(f"autostart is_enabled check failed: {e}")
        return False


def set_enabled(enabled: bool) -> bool:
    """Add or remove the Run-key value. Returns True on success."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _command())
                logging.info(f"Autostart enabled -> {_command()}")
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                    logging.info("Autostart disabled")
                except FileNotFoundError:
                    pass  # already absent
        return True
    except Exception as e:
        logging.error(f"Failed to set autostart={enabled}: {e}")
        return False
