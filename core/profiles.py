"""
Dictation profiles — Text vs Code mode.

- Text mode: LLM polish (if enabled) + full punctuation.
- Code mode: NO polish (verbatim); also drops a trailing sentence period and
  lowercases a Latin Title-case first word ("Git status." -> "git status").
Dictionary and voice commands run in BOTH modes.

Mode setting is "auto" | "text" | "code" (settings.dictation_mode). In "auto" the
mode is chosen from the foreground app's process name against `code_apps` in
profiles.json (editable, hot-reloaded). `code_apps` ships EMPTY on purpose — the
user dictates prose into terminals/IDEs too, so auto defaults to Text until they
opt specific apps in. The tray "Режим" radio (Авто/Текст/Код) overrides.
"""
import ctypes
import json
import logging
import re
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Set

from config import BASE_DIR


class ProfileManager:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (BASE_DIR / "profiles.json")
        self._raw: Optional[str] = None
        self._code_apps: Set[str] = set()
        if not self.path.exists():
            self._seed_defaults()
        self._load()

    def _seed_defaults(self):
        payload = {
            "_comment": ("Авто-режим Код/Текст по активному окну. В code_apps укажи "
                         "процессы (в нижнем регистре, с .exe), для которых нужен "
                         "КОД-режим (без полировки, verbatim). Пусто = всегда Текст. "
                         "Файл подхватывается на лету. Примеры — в _examples."),
            "_examples": ["cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
                          "wt.exe", "conhost.exe", "code.exe", "pycharm64.exe"],
            "code_apps": [],
        }
        try:
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logging.info(f"Seeded default profiles: {self.path}")
        except Exception as e:
            logging.error(f"Could not seed profiles file: {e}")

    def _load(self):
        """Reload code_apps if profiles.json content changed."""
        raw: Optional[str] = None
        if self.path.exists():
            try:
                raw = self.path.read_text(encoding="utf-8")
            except Exception as e:
                logging.error(f"Failed to read profiles.json: {e}")
        if raw == self._raw:
            return
        self._raw = raw
        apps = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    apps = data.get("code_apps", []) or []
            except Exception as e:
                logging.error(f"Failed to parse profiles.json: {e}")
        self._code_apps = {str(a).strip().lower() for a in apps if str(a).strip()}
        logging.info(f"Loaded {len(self._code_apps)} code-mode app(s)")

    def _foreground_process_name(self) -> Optional[str]:
        """Process name (lowercase) of the foreground window, or None."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return None
            import psutil
            return psutil.Process(pid.value).name().lower()
        except Exception as e:
            logging.debug(f"foreground app lookup failed: {e}")
            return None

    def effective_mode(self, setting: Optional[str]) -> str:
        """Resolve setting ('auto'/'text'/'code') to 'text' or 'code'."""
        setting = (setting or "auto").lower()
        if setting in ("text", "code"):
            return setting
        # auto
        self._load()
        if not self._code_apps:
            return "text"
        name = self._foreground_process_name()
        if name and name in self._code_apps:
            logging.info(f"Auto profile: '{name}' -> code mode")
            return "code"
        return "text"

    @staticmethod
    def apply_code_style(text: str) -> str:
        """Code-mode tweaks: drop a single trailing period; lowercase a Latin
        Title-case first word (protects ALL-CAPS like API and CamelCase terms,
        and Cyrillic prose)."""
        if not text:
            return text
        if text.endswith(".") and not text.endswith(".."):
            text = text[:-1].rstrip()
        # Only a simple Latin Title-case first word: "Git " -> "git ".
        if re.match(r"[A-Z][a-z]+\b", text):
            text = text[0].lower() + text[1:]
        return text
