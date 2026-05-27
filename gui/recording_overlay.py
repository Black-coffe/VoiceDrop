"""
Recording Overlay - Visual indicator during voice recording
Shows real-time audio visualization and recording timer
"""
import math
import tkinter as tk
from typing import Optional, Callable
from collections import deque


class RecordingOverlay:
    def __init__(self):
        self._window: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None
        self._timer_label: Optional[tk.Label] = None
        self._is_visible = False
        self._animation_id: Optional[str] = None
        self._phase = 0.0

        # Audio level callback
        self._get_audio_level: Optional[Callable[[], float]] = None
        self._get_duration: Optional[Callable[[], float]] = None

        # Audio level history for waveform
        self._level_history: deque = deque(maxlen=40)

        # Wave parameters
        self._wave_points = 40
        self._wave_height = 30
        self._wave_width = 220
        self._window_height = 80
        self._window_width = 280

    def set_audio_callbacks(
        self,
        get_level: Callable[[], float],
        get_duration: Callable[[], float]
    ):
        """Set callbacks to get real-time audio data"""
        self._get_audio_level = get_level
        self._get_duration = get_duration

    def _create_window(self, parent: tk.Tk):
        """Create the overlay window"""
        self._window = tk.Toplevel(parent)

        # Remove window decorations and make it topmost
        self._window.overrideredirect(True)
        self._window.attributes('-topmost', True)
        self._window.attributes('-alpha', 0.9)

        # Dark background
        self._window.configure(bg='#1a1a2e')

        # Position at bottom center of screen
        screen_width = self._window.winfo_screenwidth()
        screen_height = self._window.winfo_screenheight()
        x = (screen_width - self._window_width) // 2
        y = screen_height - self._window_height - 100

        self._window.geometry(f"{self._window_width}x{self._window_height}+{x}+{y}")

        # Main frame
        frame = tk.Frame(self._window, bg='#1a1a2e')
        frame.pack(expand=True, fill='both', padx=10, pady=8)

        # Top row: recording dot + timer
        top_frame = tk.Frame(frame, bg='#1a1a2e')
        top_frame.pack(fill='x', pady=(0, 5))

        # Recording indicator dot
        self._dot_canvas = tk.Canvas(
            top_frame,
            width=14,
            height=14,
            bg='#1a1a2e',
            highlightthickness=0
        )
        self._dot_canvas.pack(side='left', padx=(5, 8))
        self._dot = self._dot_canvas.create_oval(2, 2, 12, 12, fill='#ff4757', outline='')

        # Timer label
        self._timer_label = tk.Label(
            top_frame,
            text="00:00",
            font=('Consolas', 14, 'bold'),
            fg='#ffffff',
            bg='#1a1a2e'
        )
        self._timer_label.pack(side='left')

        # Status label (will show "Запись..." or word count)
        self._status_label = tk.Label(
            top_frame,
            text="Запись...",
            font=('Segoe UI', 10),
            fg='#888888',
            bg='#1a1a2e'
        )
        self._status_label.pack(side='right', padx=10)

        # Canvas for wave animation
        self._canvas = tk.Canvas(
            frame,
            width=self._wave_width,
            height=self._wave_height * 2,
            bg='#1a1a2e',
            highlightthickness=0
        )
        self._canvas.pack()

        # Initially hidden
        self._window.withdraw()

    def _draw_wave(self):
        """Draw audio waveform based on real audio levels"""
        if not self._canvas or not self._is_visible:
            return

        self._canvas.delete('wave')

        # Get current audio level
        current_level = 0.0
        if self._get_audio_level:
            current_level = self._get_audio_level()

        # Add to history
        self._level_history.append(current_level)

        # Fill history if not enough points
        while len(self._level_history) < self._wave_points:
            self._level_history.appendleft(0.0)

        center_y = self._wave_height

        # Draw bars visualization (more responsive to audio)
        bar_width = self._wave_width / self._wave_points
        for i, level in enumerate(self._level_history):
            x = i * bar_width
            # Add some minimum height and animate
            height = max(3, level * self._wave_height * 1.5)

            # Color based on level
            if level > 0.5:
                color = '#4ecdc4'  # Bright teal for loud
            elif level > 0.2:
                color = '#45b7aa'  # Medium teal
            else:
                color = '#2d8a7e'  # Dim teal for quiet

            # Draw bar (centered)
            self._canvas.create_rectangle(
                x + 1,
                center_y - height,
                x + bar_width - 1,
                center_y + height,
                fill=color,
                outline='',
                tags='wave'
            )

        # Draw center line
        self._canvas.create_line(
            0, center_y, self._wave_width, center_y,
            fill='#3d5a80',
            width=1,
            tags='wave'
        )

    def _update_timer(self):
        """Update the timer display"""
        if self._timer_label and self._get_duration:
            duration = self._get_duration()
            minutes = int(duration // 60)
            seconds = int(duration % 60)
            self._timer_label.config(text=f"{minutes:02d}:{seconds:02d}")

    def _animate(self):
        """Animation loop"""
        if not self._is_visible:
            return

        self._phase += 0.15
        self._draw_wave()
        self._update_timer()

        # Pulse the recording dot
        if self._dot_canvas:
            pulse = (math.sin(self._phase * 2) + 1) / 2
            r = int(255)
            g = int(71 + pulse * 50)
            b = int(87 + pulse * 50)
            color = f'#{r:02x}{g:02x}{b:02x}'
            self._dot_canvas.itemconfig(self._dot, fill=color)

        if self._window:
            self._animation_id = self._window.after(50, self._animate)  # 20 FPS

    def show(self, parent: tk.Tk):
        """Show the overlay"""
        if self._window is None:
            self._create_window(parent)

        self._is_visible = True
        self._phase = 0.0
        self._level_history.clear()

        # Reset status (also reset colour after a previous error state)
        if self._status_label:
            self._status_label.config(text="Запись...", fg='#888888')

        if self._window:
            self._window.deiconify()
            self._window.lift()
            self._animate()

    def hide(self):
        """Hide the overlay"""
        self._is_visible = False

        if self._animation_id and self._window:
            self._window.after_cancel(self._animation_id)
            self._animation_id = None

        if self._window:
            self._window.withdraw()

    def show_processing(self):
        """Show processing state"""
        if self._status_label:
            self._status_label.config(text="Обработка...")

    def show_polish_partial(self, partial_text: str):
        """Show the tail of the polished text as it streams in.

        Visual-only — the actual paste still happens once on the FINAL text
        in _process_audio, so this can never affect what gets inserted into
        the user's window. Safe to call on a destroyed overlay (no-op).
        """
        if self._status_label is None:
            return
        tail = (partial_text or "").strip().replace("\n", " ")
        if len(tail) > 38:
            tail = "…" + tail[-37:]
        try:
            self._status_label.config(text=tail or "Обработка...", fg='#888888')
        except Exception:
            # Tk widget can be torn down between threads; ignore.
            pass

    def show_result(self, word_count: int, char_count: int):
        """Show result after transcription"""
        if self._status_label:
            self._status_label.config(text=f"{word_count} слов, {char_count} символов")

    def show_error(self, message: str):
        """Show an error/offline state in red (caller schedules hide)."""
        self._is_visible = True
        if self._status_label:
            short = (message[:38] + '…') if len(message) > 38 else message
            self._status_label.config(text=f"⚠ {short}", fg='#ff6b6b')

    def destroy(self):
        """Destroy the overlay window"""
        self.hide()
        if self._window:
            self._window.destroy()
            self._window = None
