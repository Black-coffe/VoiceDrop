"""
System Tray Icon - Background application interface
"""
import logging
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item

from config import APP_NAME, TRAY_TOOLTIP

# Coalesce rapid recording-state changes so we don't hammer Shell_NotifyIcon
# (rapid icon swaps from overlapping hotkey threads make the tray icon vanish).
_TRAY_DEBOUNCE_SEC = 0.15


def create_icon_image(color: str = "#4CAF50", size: int = 64) -> Image.Image:
    """Create a simple microphone icon"""
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Draw microphone body (rounded rectangle)
    margin = size // 6
    mic_width = size // 3
    mic_height = size // 2
    x1 = (size - mic_width) // 2
    y1 = margin
    x2 = x1 + mic_width
    y2 = y1 + mic_height

    # Microphone body
    draw.rounded_rectangle([x1, y1, x2, y2], radius=mic_width // 2, fill=color)

    # Stand base
    stand_y = y2 + margin // 2
    stand_width = size // 2
    stand_x1 = (size - stand_width) // 2
    stand_x2 = stand_x1 + stand_width

    # Stand arc
    draw.arc([stand_x1, y2 - margin, stand_x2, stand_y + margin],
             start=0, end=180, fill=color, width=3)

    # Stand line
    center_x = size // 2
    draw.line([center_x, stand_y, center_x, size - margin], fill=color, width=3)

    # Base line
    base_width = size // 3
    draw.line([center_x - base_width // 2, size - margin,
               center_x + base_width // 2, size - margin], fill=color, width=3)

    return image


class TrayIcon:
    def __init__(
        self,
        on_show_history: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        on_settings: Optional[Callable[[], None]] = None,
        on_set_language: Optional[Callable[[Optional[str]], None]] = None,
        get_language: Optional[Callable[[], Optional[str]]] = None,
        on_toggle_polish: Optional[Callable[[], None]] = None,
        get_polish_enabled: Optional[Callable[[], bool]] = None,
        on_set_mode: Optional[Callable[[str], None]] = None,
        get_mode: Optional[Callable[[], str]] = None,
        on_retranscribe: Optional[Callable[[str], None]] = None,
        on_copy_last: Optional[Callable[[], None]] = None,
        on_show_usage: Optional[Callable[[], None]] = None,
        on_set_insert_mode: Optional[Callable[[str], None]] = None,
        get_insert_mode: Optional[Callable[[], str]] = None,
        on_toggle_autostart: Optional[Callable[[], None]] = None,
        get_autostart: Optional[Callable[[], bool]] = None,
        on_toggle_save_history: Optional[Callable[[], None]] = None,
        get_save_history: Optional[Callable[[], bool]] = None,
    ):
        self.on_show_history = on_show_history
        self.on_quit = on_quit
        self.on_settings = on_settings
        self.on_set_language = on_set_language
        self.get_language = get_language
        self.on_toggle_polish = on_toggle_polish
        self.get_polish_enabled = get_polish_enabled
        self.on_set_mode = on_set_mode
        self.get_mode = get_mode
        self.on_retranscribe = on_retranscribe
        self.on_copy_last = on_copy_last
        self.on_show_usage = on_show_usage
        self.on_set_insert_mode = on_set_insert_mode
        self.get_insert_mode = get_insert_mode
        self.on_toggle_autostart = on_toggle_autostart
        self.get_autostart = get_autostart
        self.on_toggle_save_history = on_toggle_save_history
        self.get_save_history = get_save_history

        self._icon: Optional[pystray.Icon] = None
        self._icon_normal = create_icon_image("#4CAF50")  # Green
        self._icon_recording = create_icon_image("#F44336")  # Red
        self._is_recording = False

        # Debounced tray-state updates (see set_recording)
        self._state_lock = threading.Lock()
        self._pending_state: Optional[bool] = None
        self._apply_timer: Optional[threading.Timer] = None

    # Language quick-toggle: label -> language_code (None = auto-detect)
    LANGUAGE_OPTIONS = [
        ("Авто", None),
        ("Русский", "ru"),
        ("Українська", "uk"),
        ("English", "en"),
    ]

    # Dictation mode: label -> setting value
    MODE_OPTIONS = [
        ("Авто (по приложению)", "auto"),
        ("Текст", "text"),
        ("Код", "code"),
    ]

    # Insert mode: label -> setting value
    INSERT_OPTIONS = [
        ("В окно", "window"),
        ("Только в буфер", "clipboard"),
        ("В окно + Enter", "enter"),
    ]

    def _create_menu(self):
        """Create the tray menu"""
        return pystray.Menu(
            Item("Открыть историю", self._on_show_history, default=True),
            Item("Режим", pystray.Menu(
                *[self._mode_item(label, value) for label, value in self.MODE_OPTIONS]
            )),
            Item("Вставка", pystray.Menu(
                *[self._insert_item(label, value) for label, value in self.INSERT_OPTIONS]
            )),
            Item("Язык", pystray.Menu(
                *[self._lang_item(label, code) for label, code in self.LANGUAGE_OPTIONS]
            )),
            Item("Скопировать последнее", lambda icon, item: self._handle_copy_last()),
            Item("Переписать последнее", pystray.Menu(
                Item("Русский", lambda icon, item: self._handle_retranscribe("ru")),
                Item("Українська", lambda icon, item: self._handle_retranscribe("uk")),
                Item("English", lambda icon, item: self._handle_retranscribe("en")),
            )),
            Item(
                "Полировка текста (LLM)",
                lambda icon, item: self._handle_toggle_polish(),
                checked=lambda item: self._current_polish(),
            ),
            Item(
                "Автозапуск с Windows",
                lambda icon, item: self._handle_toggle_autostart(),
                checked=lambda item: self._current_autostart(),
            ),
            Item("Расход ElevenLabs", lambda icon, item: self._handle_show_usage()),
            Item(
                "Сохранять историю",
                lambda icon, item: self._handle_toggle_save_history(),
                checked=lambda item: self._current_save_history(),
            ),
            Item("Настройки", self._on_settings),
            pystray.Menu.SEPARATOR,
            Item("Выход", self._on_quit)
        )

    def _current_language(self) -> Optional[str]:
        """Currently active language code (None = auto)."""
        if self.get_language:
            try:
                return self.get_language()
            except Exception:
                return None
        return None

    def _lang_item(self, label: str, code: Optional[str]) -> Item:
        """Build a radio menu item for a language choice."""
        return Item(
            label,
            lambda icon, item: self._handle_set_language(code),
            checked=lambda item: self._current_language() == code,
            radio=True,
        )

    def _handle_set_language(self, code: Optional[str]):
        """Apply a language choice (off the tray thread so the menu stays snappy)."""
        if self.on_set_language:
            threading.Thread(target=self.on_set_language, args=(code,), daemon=True).start()

    def _current_mode(self) -> str:
        """Current dictation mode setting (for the radio checkmark)."""
        if self.get_mode:
            try:
                return self.get_mode()
            except Exception:
                return "auto"
        return "auto"

    def _mode_item(self, label: str, value: str) -> Item:
        return Item(
            label,
            lambda icon, item: self._handle_set_mode(value),
            checked=lambda item: self._current_mode() == value,
            radio=True,
        )

    def _handle_set_mode(self, value: str):
        if self.on_set_mode:
            threading.Thread(target=self.on_set_mode, args=(value,), daemon=True).start()

    def _handle_retranscribe(self, language: str):
        """Re-transcribe the last recording in a forced language (off the tray thread)."""
        if self.on_retranscribe:
            threading.Thread(target=self.on_retranscribe, args=(language,), daemon=True).start()

    def _handle_copy_last(self):
        if self.on_copy_last:
            threading.Thread(target=self.on_copy_last, daemon=True).start()

    def _handle_show_usage(self):
        if self.on_show_usage:
            threading.Thread(target=self.on_show_usage, daemon=True).start()

    def _current_save_history(self) -> bool:
        if self.get_save_history:
            try:
                return bool(self.get_save_history())
            except Exception:
                return True
        return True

    def _handle_toggle_save_history(self):
        if self.on_toggle_save_history:
            threading.Thread(target=self.on_toggle_save_history, daemon=True).start()

    def _current_insert_mode(self) -> str:
        if self.get_insert_mode:
            try:
                return self.get_insert_mode()
            except Exception:
                return "window"
        return "window"

    def _insert_item(self, label: str, value: str) -> Item:
        return Item(
            label,
            lambda icon, item: self._handle_set_insert_mode(value),
            checked=lambda item: self._current_insert_mode() == value,
            radio=True,
        )

    def _handle_set_insert_mode(self, value: str):
        if self.on_set_insert_mode:
            threading.Thread(target=self.on_set_insert_mode, args=(value,), daemon=True).start()

    def _current_polish(self) -> bool:
        """Whether LLM polish is currently enabled (for the menu checkmark)."""
        if self.get_polish_enabled:
            try:
                return bool(self.get_polish_enabled())
            except Exception:
                return False
        return False

    def _handle_toggle_polish(self):
        """Toggle LLM polish (off the tray thread)."""
        if self.on_toggle_polish:
            threading.Thread(target=self.on_toggle_polish, daemon=True).start()

    def _current_autostart(self) -> bool:
        if self.get_autostart:
            try:
                return bool(self.get_autostart())
            except Exception:
                return False
        return False

    def _handle_toggle_autostart(self):
        if self.on_toggle_autostart:
            threading.Thread(target=self.on_toggle_autostart, daemon=True).start()

    def _on_show_history(self, icon, item):
        """Handle show history menu click"""
        if self.on_show_history:
            threading.Thread(target=self.on_show_history, daemon=True).start()

    def _on_settings(self, icon, item):
        """Handle settings menu click"""
        if self.on_settings:
            threading.Thread(target=self.on_settings, daemon=True).start()

    def _on_quit(self, icon, item):
        """Handle quit menu click"""
        if self.on_quit:
            self.on_quit()
        self.stop()

    def start(self):
        """Start the tray icon"""
        self._icon = pystray.Icon(
            APP_NAME,
            self._icon_normal,
            TRAY_TOOLTIP,
            menu=self._create_menu()
        )
        self._icon.run()

    def stop(self):
        """Stop the tray icon"""
        if self._icon:
            self._icon.stop()

    def set_recording(self, is_recording: bool):
        """Request a recording-state icon update (debounced & thread-safe).

        Called from overlapping hotkey threads. We coalesce rapid changes and
        apply only the latest after a short quiet period, so quick taps don't
        hammer Shell_NotifyIcon and make the tray icon disappear.
        """
        with self._state_lock:
            self._pending_state = is_recording
            if self._apply_timer is None:
                self._apply_timer = threading.Timer(_TRAY_DEBOUNCE_SEC, self._apply_pending_state)
                self._apply_timer.daemon = True
                self._apply_timer.start()

    def _apply_pending_state(self):
        """Apply the latest requested recording state to the tray icon."""
        with self._state_lock:
            self._apply_timer = None
            state = self._pending_state
            self._pending_state = None
            if state is None or state == self._is_recording:
                return  # nothing actually changed (e.g. a quick tap that toggled back)
            self._is_recording = state

        if not self._icon:
            return
        try:
            if state:
                self._icon.icon = self._icon_recording
                self._icon.title = f"{APP_NAME} - Запись..."
            else:
                self._icon.icon = self._icon_normal
                self._icon.title = TRAY_TOOLTIP
        except Exception as e:
            logging.error(f"Tray icon update failed: {e}", exc_info=True)

    def show_notification(self, title: str, message: str):
        """Show a notification"""
        if self._icon:
            self._icon.notify(message, title)
