"""
System Tray Icon - Background application interface
"""
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as Item

from config import APP_NAME, TRAY_TOOLTIP


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
        on_settings: Optional[Callable[[], None]] = None
    ):
        self.on_show_history = on_show_history
        self.on_quit = on_quit
        self.on_settings = on_settings

        self._icon: Optional[pystray.Icon] = None
        self._icon_normal = create_icon_image("#4CAF50")  # Green
        self._icon_recording = create_icon_image("#F44336")  # Red
        self._is_recording = False

    def _create_menu(self):
        """Create the tray menu"""
        return pystray.Menu(
            Item("Открыть историю", self._on_show_history, default=True),
            Item("Настройки", self._on_settings),
            pystray.Menu.SEPARATOR,
            Item("Выход", self._on_quit)
        )

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
        """Update icon to show recording state"""
        self._is_recording = is_recording
        if self._icon:
            if is_recording:
                self._icon.icon = self._icon_recording
                self._icon.title = f"{APP_NAME} - Запись..."
            else:
                self._icon.icon = self._icon_normal
                self._icon.title = TRAY_TOOLTIP

    def show_notification(self, title: str, message: str):
        """Show a notification"""
        if self._icon:
            self._icon.notify(message, title)
