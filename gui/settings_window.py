"""
Settings Window - Application settings including microphone and hotkey configuration
"""
import json
import os
import sys
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional, Set
import threading

import customtkinter as ctk
from pynput import keyboard

from core.audio_recorder import AudioRecorder


def get_app_dir():
    """Get application directory that works for both development and PyInstaller"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).parent.parent

APP_DIR = get_app_dir()
SETTINGS_FILE = APP_DIR / "settings.json"
ICON_PATH = str(APP_DIR / 'assets' / 'icon.ico')

# Default hotkey: Ctrl + Shift + Space
DEFAULT_HOTKEY = {
    'keys': [162, 160, 32],  # VK codes: Left Ctrl, Left Shift, Space
    'display': 'Ctrl + Shift + Space'
}

# Virtual key code to name mapping
VK_NAMES = {
    # Modifiers
    162: 'Ctrl', 163: 'Ctrl',  # Left/Right Ctrl
    160: 'Shift', 161: 'Shift',  # Left/Right Shift
    164: 'Alt', 165: 'Alt',  # Left/Right Alt
    91: 'Win', 92: 'Win',  # Left/Right Win
    # Special keys
    32: 'Space',
    13: 'Enter',
    9: 'Tab',
    27: 'Esc',
    8: 'Backspace',
    46: 'Delete',
    36: 'Home',
    35: 'End',
    33: 'PageUp',
    34: 'PageDown',
    37: 'Left', 38: 'Up', 39: 'Right', 40: 'Down',
    # Function keys
    112: 'F1', 113: 'F2', 114: 'F3', 115: 'F4',
    116: 'F5', 117: 'F6', 118: 'F7', 119: 'F8',
    120: 'F9', 121: 'F10', 122: 'F11', 123: 'F12',
    # Numbers (top row)
    48: '0', 49: '1', 50: '2', 51: '3', 52: '4',
    53: '5', 54: '6', 55: '7', 56: '8', 57: '9',
    # Letters (A-Z = 65-90)
    **{i: chr(i) for i in range(65, 91)},
    # Numpad
    96: 'Num0', 97: 'Num1', 98: 'Num2', 99: 'Num3', 100: 'Num4',
    101: 'Num5', 102: 'Num6', 103: 'Num7', 104: 'Num8', 105: 'Num9',
}


def get_key_name(vk: int) -> str:
    """Get display name for a virtual key code"""
    return VK_NAMES.get(vk, f'Key{vk}')


def load_settings() -> dict:
    """Load settings from file"""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(settings: dict):
    """Save settings to file atomically (temp file + os.replace).

    Atomic write stops a concurrent reader (e.g. the tray re-rendering its
    language radio while we save) from seeing a half-written file, falling back
    to defaults, and showing two language items checked at once.
    """
    tmp = SETTINGS_FILE.parent / (SETTINGS_FILE.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_FILE)


class SettingsWindow(ctk.CTkToplevel):
    def __init__(
        self,
        parent=None,
        audio_recorder: Optional[AudioRecorder] = None,
        on_settings_changed: Optional[Callable[[dict], None]] = None
    ):
        super().__init__(parent)

        self.audio_recorder = audio_recorder
        self.on_settings_changed = on_settings_changed
        self.settings = load_settings()

        self.title("VoiceDrop - Настройки")
        self.geometry("500x600")
        self.minsize(400, 560)
        self.resizable(False, False)

        # Set window icon (need to wait for window to be created)
        self.after(200, self._set_icon)

        # Hotkey recording state
        self._is_recording_hotkey = False
        self._recorded_keys: Set[int] = set()
        self._hotkey_listener: Optional[keyboard.Listener] = None

        # Configure grid
        self.grid_columnconfigure(0, weight=1)

        # Header
        self.header_label = ctk.CTkLabel(
            self,
            text="Настройки",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        self.header_label.grid(row=0, column=0, pady=(20, 10), padx=20, sticky="w")

        # ===== Microphone section =====
        self.mic_frame = ctk.CTkFrame(self)
        self.mic_frame.grid(row=1, column=0, pady=10, padx=20, sticky="ew")
        self.mic_frame.grid_columnconfigure(1, weight=1)

        self.mic_label = ctk.CTkLabel(
            self.mic_frame,
            text="Микрофон:",
            font=ctk.CTkFont(size=14)
        )
        self.mic_label.grid(row=0, column=0, pady=15, padx=15, sticky="w")

        # Get available microphones
        self.microphones = []
        self.mic_names = []
        self._load_microphones()

        self.mic_dropdown = ctk.CTkComboBox(
            self.mic_frame,
            values=self.mic_names,
            width=280,
            state="readonly"
        )
        self.mic_dropdown.grid(row=0, column=1, pady=15, padx=15, sticky="ew")

        # Set current microphone selection
        self._set_current_microphone()

        # Refresh button
        self.refresh_btn = ctk.CTkButton(
            self.mic_frame,
            text="↻",
            width=40,
            command=self._refresh_microphones
        )
        self.refresh_btn.grid(row=0, column=2, pady=15, padx=(0, 15))

        # Test microphone button
        self.test_frame = ctk.CTkFrame(self)
        self.test_frame.grid(row=2, column=0, pady=5, padx=20, sticky="ew")

        self.test_btn = ctk.CTkButton(
            self.test_frame,
            text="🎤 Проверить микрофон",
            command=self._test_microphone
        )
        self.test_btn.pack(pady=10, padx=15, side="left")

        self.test_status = ctk.CTkLabel(
            self.test_frame,
            text="",
            font=ctk.CTkFont(size=12)
        )
        self.test_status.pack(pady=10, padx=15, side="left")

        # ===== Language section =====
        self.lang_frame = ctk.CTkFrame(self)
        self.lang_frame.grid(row=3, column=0, pady=10, padx=20, sticky="ew")
        self.lang_frame.grid_columnconfigure(1, weight=1)

        self.lang_label = ctk.CTkLabel(
            self.lang_frame,
            text="Язык распознавания:",
            font=ctk.CTkFont(size=14)
        )
        self.lang_label.grid(row=0, column=0, pady=15, padx=15, sticky="w")

        # Language options: Auto, Russian, Ukrainian, English
        self.language_options = {
            "Автоопределение": None,
            "Русский": "ru",
            "Українська": "uk",
            "English": "en"
        }
        self.lang_names = list(self.language_options.keys())

        self.lang_dropdown = ctk.CTkComboBox(
            self.lang_frame,
            values=self.lang_names,
            width=280,
            state="readonly"
        )
        self.lang_dropdown.grid(row=0, column=1, pady=15, padx=15, sticky="ew")

        # Set current language selection
        current_lang = self.settings.get('language_code', None)
        lang_name = "Автоопределение"
        for name, code in self.language_options.items():
            if code == current_lang:
                lang_name = name
                break
        self.lang_dropdown.set(lang_name)

        # ===== Hotkey section =====
        self.hotkey_frame = ctk.CTkFrame(self)
        self.hotkey_frame.grid(row=4, column=0, pady=10, padx=20, sticky="ew")
        self.hotkey_frame.grid_columnconfigure(1, weight=1)

        self.hotkey_label = ctk.CTkLabel(
            self.hotkey_frame,
            text="Горячие клавиши:",
            font=ctk.CTkFont(size=14)
        )
        self.hotkey_label.grid(row=0, column=0, pady=15, padx=15, sticky="w")

        # Current hotkey display
        current_hotkey = self.settings.get('hotkey', DEFAULT_HOTKEY)
        self.hotkey_display = ctk.CTkLabel(
            self.hotkey_frame,
            text=current_hotkey.get('display', DEFAULT_HOTKEY['display']),
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color=("gray85", "gray25"),
            corner_radius=6,
            padx=15,
            pady=8
        )
        self.hotkey_display.grid(row=0, column=1, pady=15, padx=10, sticky="w")

        # Record hotkey button
        self.record_hotkey_btn = ctk.CTkButton(
            self.hotkey_frame,
            text="Изменить",
            width=100,
            command=self._start_hotkey_recording
        )
        self.record_hotkey_btn.grid(row=0, column=2, pady=15, padx=(0, 15))

        # Hotkey hint
        self.hotkey_hint = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray"
        )
        self.hotkey_hint.grid(row=4, column=0, pady=(0, 5), padx=20, sticky="w")

        # ===== History retention section =====
        self.retention_frame = ctk.CTkFrame(self)
        self.retention_frame.grid(row=5, column=0, pady=10, padx=20, sticky="ew")
        self.retention_frame.grid_columnconfigure(1, weight=1)

        self.retention_label = ctk.CTkLabel(
            self.retention_frame,
            text="Хранить историю:",
            font=ctk.CTkFont(size=14)
        )
        self.retention_label.grid(row=0, column=0, pady=15, padx=15, sticky="w")

        self.retention_options = {"24 часа": 24, "7 дней": 168, "30 дней": 720, "Бессрочно": 0}
        self.retention_names = list(self.retention_options.keys())
        self.retention_dropdown = ctk.CTkComboBox(
            self.retention_frame,
            values=self.retention_names,
            width=280,
            state="readonly"
        )
        self.retention_dropdown.grid(row=0, column=1, pady=15, padx=15, sticky="ew")
        self._set_retention_selection()

        # Save button
        self.save_btn = ctk.CTkButton(
            self,
            text="Сохранить",
            font=ctk.CTkFont(size=14),
            height=40,
            command=self._save_settings
        )
        self.save_btn.grid(row=6, column=0, pady=20, padx=20, sticky="ew")

        # Hide instead of destroy on close
        self.protocol("WM_DELETE_WINDOW", self.hide)

    def _set_current_microphone(self):
        """Set dropdown to current microphone from settings (match BY NAME)."""
        current_name = self.settings.get('microphone_name')
        current_hostapi = self.settings.get('microphone_hostapi')
        current_id = self.settings.get('microphone_id')

        # 1) exact name + host API (most precise)
        if current_name:
            for i, mic in enumerate(self.microphones):
                if mic['name'] == current_name and (
                    not current_hostapi or mic.get('hostapi') == current_hostapi
                ):
                    self.mic_dropdown.set(self.mic_names[i])
                    return
            # 2) name only
            for i, mic in enumerate(self.microphones):
                if mic['name'] == current_name:
                    self.mic_dropdown.set(self.mic_names[i])
                    return
        # 3) legacy id hint
        if current_id is not None:
            for i, mic in enumerate(self.microphones):
                if mic['id'] == current_id:
                    self.mic_dropdown.set(self.mic_names[i])
                    return
        # 4) system default
        if self.mic_names:
            for i, mic in enumerate(self.microphones):
                if mic.get('default'):
                    self.mic_dropdown.set(self.mic_names[i])
                    return
            self.mic_dropdown.set(self.mic_names[0])

    def _set_retention_selection(self):
        """Select the current history-retention option from settings."""
        cur = self.settings.get('history_retention_hours', 24)
        name = next((n for n, h in self.retention_options.items() if h == cur), "24 часа")
        self.retention_dropdown.set(name)

    def _load_microphones(self):
        """Load available microphones"""
        if self.audio_recorder:
            self.microphones = self.audio_recorder.get_available_devices()
        else:
            temp_recorder = AudioRecorder()
            self.microphones = temp_recorder.get_available_devices()

        self.mic_names = []
        for mic in self.microphones:
            name = mic['name']
            ha = mic.get('hostapi')
            if ha:
                name += f" [{ha}]"  # disambiguate the same mic across host APIs
            if mic.get('default'):
                name += " (по умолчанию)"
            self.mic_names.append(name)

        if not self.mic_names:
            self.mic_names = ["Микрофон не найден"]

    def _refresh_microphones(self):
        """Refresh microphone list"""
        self._load_microphones()
        self.mic_dropdown.configure(values=self.mic_names)
        self._set_current_microphone()
        self.test_status.configure(text="Список обновлен")
        self.after(2000, lambda: self.test_status.configure(text=""))

    def _get_selected_mic_index(self) -> int:
        """Get index of currently selected microphone"""
        selected_value = self.mic_dropdown.get()
        try:
            return self.mic_names.index(selected_value)
        except ValueError:
            return -1

    def _test_microphone(self):
        """Test selected microphone"""
        self.test_status.configure(text="Проверка...")
        self.update()

        try:
            import sounddevice as sd
            import numpy as np

            selected_idx = self._get_selected_mic_index()
            if selected_idx >= 0 and selected_idx < len(self.microphones):
                device_id = self.microphones[selected_idx]['id']
            else:
                device_id = None

            duration = 0.5
            recording = sd.rec(
                int(duration * 16000),
                samplerate=16000,
                channels=1,
                device=device_id,
                dtype=np.float32
            )
            sd.wait()

            max_amplitude = np.max(np.abs(recording))
            if max_amplitude > 0.01:
                self.test_status.configure(text="✓ Микрофон работает!", text_color="green")
            else:
                self.test_status.configure(text="⚠ Сигнал слабый", text_color="orange")

        except Exception as e:
            self.test_status.configure(text=f"✗ Ошибка: {str(e)[:30]}", text_color="red")

        self.after(3000, lambda: self.test_status.configure(text="", text_color="white"))

    # ===== Hotkey recording =====

    def _get_vk_from_key(self, key) -> Optional[int]:
        """Extract virtual key code from pynput key"""
        if hasattr(key, 'vk') and key.vk is not None:
            return key.vk
        if hasattr(key, 'value') and hasattr(key.value, 'vk'):
            return key.value.vk
        # Handle special keys
        if isinstance(key, keyboard.Key):
            key_vk_map = {
                keyboard.Key.ctrl_l: 162, keyboard.Key.ctrl_r: 163,
                keyboard.Key.shift: 160, keyboard.Key.shift_l: 160, keyboard.Key.shift_r: 161,
                keyboard.Key.alt_l: 164, keyboard.Key.alt_r: 165,
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

    def _on_hotkey_press(self, key):
        """Handle key press during hotkey recording"""
        if not self._is_recording_hotkey:
            return

        vk = self._get_vk_from_key(key)
        if vk:
            self._recorded_keys.add(vk)
            self._update_hotkey_display()

    def _on_hotkey_release(self, key):
        """Handle key release during hotkey recording"""
        if not self._is_recording_hotkey:
            return

        # If we have at least 2 keys (modifier + key), finish recording
        if len(self._recorded_keys) >= 2:
            self._stop_hotkey_recording()

    def _update_hotkey_display(self):
        """Update the hotkey display with current keys"""
        if not self._recorded_keys:
            return

        # Sort: modifiers first, then other keys
        modifiers = {162, 163, 160, 161, 164, 165, 91, 92}
        sorted_keys = sorted(self._recorded_keys, key=lambda k: (k not in modifiers, k))

        display = ' + '.join(get_key_name(vk) for vk in sorted_keys)
        self.hotkey_display.configure(text=display)

    def _start_hotkey_recording(self):
        """Start recording a new hotkey"""
        self._is_recording_hotkey = True
        self._recorded_keys = set()

        self.hotkey_display.configure(text="Нажмите комбинацию...", fg_color=("orange", "darkorange"))
        self.hotkey_hint.configure(text="Нажмите и удерживайте клавиши, затем отпустите")
        self.record_hotkey_btn.configure(text="Отмена", command=self._cancel_hotkey_recording)

        # Start listener
        self._hotkey_listener = keyboard.Listener(
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release
        )
        self._hotkey_listener.start()

    def _stop_hotkey_recording(self):
        """Stop recording and save the hotkey"""
        self._is_recording_hotkey = False

        if self._hotkey_listener:
            self._hotkey_listener.stop()
            self._hotkey_listener = None

        # Save to temp settings
        if self._recorded_keys:
            modifiers = {162, 163, 160, 161, 164, 165, 91, 92}
            sorted_keys = sorted(self._recorded_keys, key=lambda k: (k not in modifiers, k))
            display = ' + '.join(get_key_name(vk) for vk in sorted_keys)

            self.settings['hotkey'] = {
                'keys': sorted_keys,
                'display': display
            }

        self.hotkey_display.configure(fg_color=("gray85", "gray25"))
        self.hotkey_hint.configure(text="")
        self.record_hotkey_btn.configure(text="Изменить", command=self._start_hotkey_recording)

    def _cancel_hotkey_recording(self):
        """Cancel hotkey recording"""
        self._is_recording_hotkey = False

        if self._hotkey_listener:
            self._hotkey_listener.stop()
            self._hotkey_listener = None

        # Restore previous hotkey display
        current_hotkey = self.settings.get('hotkey', DEFAULT_HOTKEY)
        self.hotkey_display.configure(
            text=current_hotkey.get('display', DEFAULT_HOTKEY['display']),
            fg_color=("gray85", "gray25")
        )
        self.hotkey_hint.configure(text="")
        self.record_hotkey_btn.configure(text="Изменить", command=self._start_hotkey_recording)

    def _save_settings(self):
        """Save settings"""
        # Save microphone
        selected_idx = self._get_selected_mic_index()
        if selected_idx >= 0 and selected_idx < len(self.microphones):
            self.settings['microphone_id'] = self.microphones[selected_idx]['id']
            self.settings['microphone_name'] = self.microphones[selected_idx]['name']
            self.settings['microphone_hostapi'] = self.microphones[selected_idx].get('hostapi')

        # Save language
        selected_lang = self.lang_dropdown.get()
        self.settings['language_code'] = self.language_options.get(selected_lang, None)
        self.settings['language_name'] = selected_lang

        # Save history retention
        selected_retention = self.retention_dropdown.get()
        self.settings['history_retention_hours'] = self.retention_options.get(selected_retention, 24)

        # Hotkey is already saved in self.settings during recording

        save_settings(self.settings)

        # Apply settings
        if self.on_settings_changed:
            self.on_settings_changed(self.settings)

        # Show confirmation and close
        self.save_btn.configure(text="✓ Сохранено!")
        self.after(2000, self.hide)

    def _set_icon(self):
        """Set window icon"""
        try:
            if os.path.exists(ICON_PATH):
                self.iconbitmap(ICON_PATH)
        except Exception:
            pass  # Ignore icon errors

    def show(self):
        """Show the window"""
        self.save_btn.configure(text="Сохранить")
        self.settings = load_settings()  # Reload settings

        # Update hotkey display
        current_hotkey = self.settings.get('hotkey', DEFAULT_HOTKEY)
        self.hotkey_display.configure(text=current_hotkey.get('display', DEFAULT_HOTKEY['display']))

        # Update language display
        current_lang = self.settings.get('language_code', None)
        lang_name = "Автоопределение"
        for name, code in self.language_options.items():
            if code == current_lang:
                lang_name = name
                break
        self.lang_dropdown.set(lang_name)

        # Update retention display
        self._set_retention_selection()

        self._refresh_microphones()
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self):
        """Hide the window"""
        # Stop any ongoing recording
        if self._is_recording_hotkey:
            self._cancel_hotkey_recording()
        self.withdraw()
