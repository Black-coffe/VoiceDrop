"""
Recording Overlay - Visual indicator during voice recording
Shows real-time audio visualization, recording timer, and (in realtime mode)
live subtitles of what is being recognized.
"""
import ctypes
import logging
import math
import time
import tkinter as tk
from typing import Optional, Callable, Tuple
from collections import deque


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


# Base (no subtitles) and expanded (with the 2-line subtitle strip) heights.
_BASE_H = 80
_SUBTITLE_H = 48
_BOTTOM_MARGIN = 12   # gap above the taskbar / work-area bottom
# Coalesce the rapid realtime partials into at most one overlay update per this
# many ms — partials can arrive faster than the eye (or Tk) wants to repaint.
_SUBTITLE_DEBOUNCE_MS = 150
# Overlay animation frame interval (ms). 80 ms ≈ 12.5 FPS — smooth enough for
# the waveform/timer while cutting per-frame work vs the old 50 ms / 20 FPS.
# The old rate, combined with delete+recreate of ~43 canvas items per frame,
# leaked Windows USER objects until the process hit the ~10k ceiling
# (WinError 1158 → tray icon stuck, system freeze). See _draw_wave.
_ANIM_INTERVAL_MS = 80


class RecordingOverlay:
    def __init__(self):
        self._window: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None
        self._timer_label: Optional[tk.Label] = None
        self._is_visible = False
        self._animation_id: Optional[str] = None
        self._phase = 0.0
        self._t0 = time.monotonic()  # for time-based (frame-drop-immune) pulse

        # Audio level callback
        self._get_audio_level: Optional[Callable[[], float]] = None
        self._get_duration: Optional[Callable[[], float]] = None

        # Audio level history for waveform
        self._level_history: deque = deque(maxlen=40)
        self._peak: float = 0.0  # decaying peak-hold level (G2)

        # Wave parameters
        self._wave_points = 40
        self._wave_height = 30
        self._wave_width = 220
        self._window_height = _BASE_H
        self._window_width = 280
        self._cur_height = _BASE_H  # tracks current geometry height

        # Persistent waveform canvas items — created ONCE in _create_window and
        # only updated each frame (coords/itemconfig), never delete+recreate.
        # Recreating them every frame was the WinError 1158 USER-handle leak.
        self._wave_bars: list = []
        self._center_line = None
        self._peak_lines: tuple = ()

        # Live subtitles (G1)
        self._subtitle_label: Optional[tk.Label] = None
        self._subtitle_expanded = False
        self._pending_subtitle: Optional[str] = None
        self._subtitle_after_id: Optional[str] = None

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

        # Position at the bottom-center of the work area (taskbar-aware) of the
        # monitor holding the active window — see _reposition (G2).
        self._reposition(_BASE_H)

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

        # Pre-create the waveform items ONCE — _draw_wave only updates their
        # coords/colour each frame. (Delete+recreate of all of these every
        # frame at 20 FPS was leaking USER handles → WinError 1158.)
        center_y = self._wave_height
        bar_width = self._wave_width / self._wave_points
        self._wave_bars = []
        for i in range(self._wave_points):
            x = i * bar_width
            bar = self._canvas.create_rectangle(
                x + 1, center_y - 3, x + bar_width - 1, center_y + 3,
                fill='#2d8a7e', outline='',
            )
            self._wave_bars.append(bar)
        self._center_line = self._canvas.create_line(
            0, center_y, self._wave_width, center_y, fill='#3d5a80', width=1,
        )
        self._peak_lines = (
            self._canvas.create_line(
                0, center_y, self._wave_width, center_y, fill='#7fe0d6', width=1,
            ),
            self._canvas.create_line(
                0, center_y, self._wave_width, center_y, fill='#7fe0d6', width=1,
            ),
        )

        # Live-subtitle strip (G1) — hidden until realtime partials arrive.
        # wraplength keeps it to ~2 lines within the overlay width.
        self._subtitle_label = tk.Label(
            frame,
            text="",
            font=('Segoe UI', 10),
            fg='#8a93b2',          # dimmed: it's provisional, not the final text
            bg='#1a1a2e',
            wraplength=self._window_width - 24,
            justify='left',
            anchor='w',
        )
        # Not packed yet — _apply_subtitle packs it when there's text.

        # Initially hidden
        self._window.withdraw()

    def _target_work_area(self) -> Tuple[int, int, int, int]:
        """(left, top, right, bottom) of the work area (excludes taskbar) of the
        monitor holding the foreground window — that's where inserted text goes,
        so the overlay should live there too (G2). Falls back to the primary
        work area, then to the full Tk screen, never raising."""
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                hmon = user32.MonitorFromWindow(hwnd, 2)  # NEAREST
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    w = mi.rcWork
                    if w.right > w.left and w.bottom > w.top:
                        return (w.left, w.top, w.right, w.bottom)
        except Exception as e:
            logging.debug(f"overlay monitor work-area lookup failed: {e}")
        # Fallback: primary monitor work area via SPI_GETWORKAREA.
        try:
            r = _RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                if r.right > r.left and r.bottom > r.top:
                    return (r.left, r.top, r.right, r.bottom)
        except Exception as e:
            logging.debug(f"overlay SPI_GETWORKAREA failed: {e}")
        # Final fallback: full Tk screen (old behaviour).
        sw = self._window.winfo_screenwidth() if self._window else 1920
        sh = self._window.winfo_screenheight() if self._window else 1080
        return (0, 0, sw, sh)

    def _reposition(self, height: int):
        """Place the window bottom-centered in the target work area, just above
        the taskbar, at the given height. Keeps the bottom edge anchored so the
        overlay grows UPWARD when subtitles expand it."""
        if not self._window:
            return
        left, top, right, bottom = self._target_work_area()
        wa_w = right - left
        x = left + (wa_w - self._window_width) // 2
        y = bottom - height - _BOTTOM_MARGIN
        # Clamp so we never land above the work-area top on tiny screens.
        if y < top:
            y = top
        self._cur_height = height
        try:
            self._window.geometry(f"{self._window_width}x{height}+{x}+{y}")
        except Exception as e:
            logging.debug(f"overlay reposition failed: {e}")

    @staticmethod
    def _subtitle_display(text: str, max_chars: int = 90) -> str:
        """Tail of the recognized text to show (~last 2 lines), whitespace
        collapsed, with a leading ellipsis when truncated. Pure — unit-tested."""
        s = " ".join((text or "").split())
        if len(s) <= max_chars:
            return s
        return "…" + s[-(max_chars - 1):]

    @staticmethod
    def _decay_peak(prev_peak: float, level: float, decay: float = 0.92) -> float:
        """Peak-hold: jump up to a new louder level instantly, otherwise decay
        the held peak slowly toward the current level. Pure — unit-tested."""
        return level if level >= prev_peak else max(level, prev_peak * decay)

    def _draw_wave(self):
        """Update the audio waveform from real audio levels.

        Reuses the persistent canvas items (created once in _create_window) —
        only their coords/colour change. We never delete+recreate items here:
        doing that ~43×/frame at 20 FPS leaked Windows USER objects until the
        process hit the ~10k ceiling and the whole window manager started
        failing (WinError 1158) — stuck tray icon, system freeze.
        """
        if not self._canvas or not self._is_visible or not self._wave_bars:
            return

        # Get current audio level
        current_level = 0.0
        if self._get_audio_level:
            current_level = self._get_audio_level()

        # Add to history
        self._level_history.append(current_level)

        # Fill history if not enough points
        while len(self._level_history) < self._wave_points:
            self._level_history.appendleft(0.0)

        # Peak-hold (G2): instant rise, slow decay.
        self._peak = self._decay_peak(self._peak, current_level)

        center_y = self._wave_height
        bar_width = self._wave_width / self._wave_points

        # Update bars (responsive to audio) — move + recolour, don't recreate.
        for i, bar in enumerate(self._wave_bars):
            level = self._level_history[i] if i < len(self._level_history) else 0.0
            height = max(3, level * self._wave_height * 1.5)
            if level > 0.5:
                color = '#4ecdc4'  # Bright teal for loud
            elif level > 0.2:
                color = '#45b7aa'  # Medium teal
            else:
                color = '#2d8a7e'  # Dim teal for quiet
            x = i * bar_width
            self._canvas.coords(
                bar, x + 1, center_y - height, x + bar_width - 1, center_y + height
            )
            self._canvas.itemconfig(bar, fill=color)

        # Peak-hold markers (G2): faint cap lines at the held peak amplitude,
        # mirrored above/below centre, so brief loud moments stay visible.
        peak_h = max(3, self._peak * self._wave_height * 1.5)
        peak_h = min(peak_h, self._wave_height)  # keep within the canvas
        for line, sign in zip(self._peak_lines, (-1, 1)):
            yy = center_y + sign * peak_h
            self._canvas.coords(line, 0, yy, self._wave_width, yy)

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

        self._draw_wave()
        self._update_timer()

        # Pulse the recording dot. Drive it by WALL-CLOCK time, not a per-frame
        # increment, so a dropped frame (the old jerk source) doesn't make the
        # pulse jump — it stays smooth regardless of actual frame timing (G2).
        if self._dot_canvas:
            pulse = (math.sin((time.monotonic() - self._t0) * 3.2) + 1) / 2
            g = int(71 + pulse * 50)
            b = int(87 + pulse * 50)
            color = f'#ff{g:02x}{b:02x}'
            self._dot_canvas.itemconfig(self._dot, fill=color)
            # Subtle radius "breathing" so the pulse reads even at a glance.
            cx, cy, base_r = 7, 7, 5
            r = base_r - 1 + pulse * 1.5
            self._dot_canvas.coords(self._dot, cx - r, cy - r, cx + r, cy + r)

        if self._window:
            self._animation_id = self._window.after(_ANIM_INTERVAL_MS, self._animate)

    def show(self, parent: tk.Tk):
        """Show the overlay"""
        if self._window is None:
            self._create_window(parent)

        self._is_visible = True
        self._phase = 0.0
        self._t0 = time.monotonic()
        self._level_history.clear()
        self._peak = 0.0

        # Reset status (also reset colour after a previous error state)
        if self._status_label:
            self._status_label.config(text="Запись...", fg='#888888')

        # Collapse any leftover subtitle from a previous clip and re-anchor at
        # the base height on the (possibly different) active monitor.
        self._reset_subtitle()
        self._reposition(_BASE_H)

        if self._window:
            self._window.deiconify()
            self._window.lift()
            # Cancel any still-pending animation tick so a fast re-show can't
            # leave two _animate loops running at once (they'd double the
            # per-frame redraw rate).
            if self._animation_id:
                try:
                    self._window.after_cancel(self._animation_id)
                except Exception:
                    pass
                self._animation_id = None
            self._animate()

    def _reset_subtitle(self):
        """Clear and un-pack the subtitle strip, collapsing the window."""
        if self._subtitle_after_id and self._window:
            try:
                self._window.after_cancel(self._subtitle_after_id)
            except Exception:
                pass
        self._subtitle_after_id = None
        self._pending_subtitle = None
        was_expanded = self._subtitle_expanded
        if was_expanded and self._subtitle_label is not None:
            try:
                self._subtitle_label.pack_forget()
            except Exception:
                pass
        self._subtitle_expanded = False
        if was_expanded:
            self._reposition(_BASE_H)  # shrink back to the compact size

    def show_subtitle(self, text: str):
        """Live subtitles (G1): show what's being recognized DURING recording.

        Called from the realtime partial_transcript callback. Coalesced to one
        repaint per ~150 ms. Visual-only — never affects the inserted text."""
        if self._subtitle_label is None or self._window is None:
            return
        self._pending_subtitle = text
        if self._subtitle_after_id is None:
            try:
                self._subtitle_after_id = self._window.after(
                    _SUBTITLE_DEBOUNCE_MS, self._apply_subtitle
                )
            except Exception:
                self._subtitle_after_id = None

    def _apply_subtitle(self):
        """Debounced subtitle paint: set the tail text and expand the window."""
        self._subtitle_after_id = None
        if self._subtitle_label is None or not self._is_visible:
            return
        text = self._subtitle_display(self._pending_subtitle or "")
        try:
            self._subtitle_label.config(text=text)
            if text and not self._subtitle_expanded:
                self._subtitle_label.pack(fill='x', padx=2, pady=(4, 0))
                self._subtitle_expanded = True
                self._reposition(_BASE_H + _SUBTITLE_H)
        except Exception:
            pass

    def hide(self):
        """Hide the overlay"""
        self._is_visible = False

        if self._animation_id and self._window:
            self._window.after_cancel(self._animation_id)
            self._animation_id = None

        self._reset_subtitle()
        if self._window:
            self._window.withdraw()

    def show_processing(self):
        """Show processing state"""
        # Recording is over — drop the live subtitles; the status label now
        # carries the polish stream / result.
        self._reset_subtitle()
        if self._status_label:
            self._status_label.config(text="Обработка...")

    def show_connecting(self, parent: tk.Tk):
        """Show a 'reconnecting microphone' state during mic open retries (A4).

        The stream isn't open yet (so the waveform stays flat); this just tells
        the user the press registered and we're waiting on the device to wake.
        """
        if self._window is None:
            self._create_window(parent)
        self._is_visible = True
        if self._window:
            self._window.deiconify()
            self._window.lift()
        if self._status_label:
            try:
                self._status_label.config(text="Подключаю микрофон…", fg='#ffd166')
            except Exception:
                pass

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
