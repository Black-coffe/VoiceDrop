"""
History Window - Shows recording history with copy functionality
"""
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from core.db_manager import DatabaseManager

def get_app_dir():
    """Get application directory that works for both development and PyInstaller"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    else:
        return Path(__file__).parent.parent

# Get icon path
ICON_PATH = str(get_app_dir() / 'assets' / 'icon.ico')


class HistoryWindow(ctk.CTkToplevel):
    def __init__(self, parent=None, on_copy_callback: Optional[Callable[[str], None]] = None):
        super().__init__(parent)

        self.on_copy_callback = on_copy_callback
        self.db = DatabaseManager()

        self.title("VoiceDrop - История записей")
        self.geometry("800x600")
        self.minsize(500, 400)

        # Set window icon (need to wait for window to be created)
        self.after(200, self._set_icon)

        # Configure grid
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Create widgets
        self._create_widgets()

    def _set_icon(self):
        """Set window icon"""
        try:
            if os.path.exists(ICON_PATH):
                self.iconbitmap(ICON_PATH)
        except Exception:
            pass  # Ignore icon errors

    def _create_widgets(self):
        """Create window widgets"""
        # Header
        self.header_frame = ctk.CTkFrame(self)
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        self.header_frame.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.header_frame,
            text="История записей",
            font=ctk.CTkFont(size=16, weight="bold")
        )
        self.title_label.grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))

        self.refresh_btn = ctk.CTkButton(
            self.header_frame,
            text="Обновить",
            width=100,
            command=self.refresh_list
        )
        self.refresh_btn.grid(row=0, column=1, padx=10, pady=(10, 5))

        # Search + export row
        self.search_entry = ctk.CTkEntry(
            self.header_frame,
            placeholder_text="Поиск по тексту…"
        )
        self.search_entry.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.search_entry.bind("<KeyRelease>", lambda e: self.refresh_list())

        self.export_btn = ctk.CTkButton(
            self.header_frame,
            text="Экспорт",
            width=100,
            command=self._export
        )
        self.export_btn.grid(row=1, column=1, padx=10, pady=(0, 10))

        self.clear_btn = ctk.CTkButton(
            self.header_frame,
            text="Очистить",
            width=100,
            fg_color="#8B2E2E",
            hover_color="#A33A3A",
            command=self._clear_history
        )
        self.clear_btn.grid(row=1, column=2, padx=(0, 10), pady=(0, 10))

        # Scrollable frame for recordings
        self.scroll_frame = ctk.CTkScrollableFrame(self)
        self.scroll_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(5, 10))
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        # Status bar
        self.status_label = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(size=12)
        )
        self.status_label.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        # Load recordings
        self.refresh_list()

        # Hide instead of destroy on close
        self.protocol("WM_DELETE_WINDOW", self.hide)

    def refresh_list(self):
        """Refresh the recordings list"""
        # Clear existing items
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        # Get recordings from database (search if there's a query)
        query = self.search_entry.get().strip() if hasattr(self, 'search_entry') else ""
        if query:
            recordings = self.db.search_recordings(query, limit=300)
        else:
            recordings = self.db.get_recent_recordings(limit=300)

        if not recordings:
            no_data_label = ctk.CTkLabel(
                self.scroll_frame,
                text="Ничего не найдено" if query else "Нет записей",
                font=ctk.CTkFont(size=14)
            )
            no_data_label.grid(row=0, column=0, pady=50)
            self.status_label.configure(text="0 записей")
            return

        # Add recording items
        for i, recording in enumerate(recordings):
            self._create_recording_item(i, recording)

        suffix = f" по запросу «{query}»" if query else ""
        self.status_label.configure(text=f"{len(recordings)} записей{suffix}")

    def _create_recording_item(self, index: int, recording: dict):
        """Create a single recording item widget"""
        is_latest = (index == 0)  # First item is the latest recording

        frame = ctk.CTkFrame(self.scroll_frame)
        frame.grid(row=index, column=0, sticky="ew", pady=(5 if is_latest else 2))
        frame.grid_columnconfigure(1, weight=1)

        # Time label
        try:
            created_at = datetime.fromisoformat(recording['created_at'])
            time_str = created_at.strftime("%d.%m %H:%M")
        except (ValueError, TypeError):
            time_str = "??:??"

        time_label = ctk.CTkLabel(
            frame,
            text=time_str,
            font=ctk.CTkFont(size=11),
            width=90
        )
        time_label.grid(row=0, column=0, padx=(10, 5), pady=8, sticky="n" if is_latest else "")

        # Text label - show more text for latest recording
        text = recording.get('text', '')
        if is_latest:
            # Latest recording: show up to 400 chars, multi-line
            max_chars = 400
            display_text = text[:max_chars] + "..." if len(text) > max_chars else text
        else:
            # Other recordings: truncate to 100 chars
            display_text = text[:100] + "..." if len(text) > 100 else text

        text_label = ctk.CTkLabel(
            frame,
            text=display_text,
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
            wraplength=550 if is_latest else 0  # Enable wrapping for latest
        )
        text_label.grid(row=0, column=1, sticky="ew", padx=5, pady=(10 if is_latest else 8))

        # Copy button
        copy_btn = ctk.CTkButton(
            frame,
            text="Копировать",
            width=90,
            height=28,
            command=lambda t=text: self._copy_text(t)
        )
        copy_btn.grid(row=0, column=2, padx=10, pady=8, sticky="n" if is_latest else "")

    def _clear_history(self):
        """Delete all recordings after confirmation."""
        if messagebox.askyesno("Очистить историю",
                               "Удалить ВСЕ записи из истории? Это необратимо.",
                               parent=self):
            self.db.clear_all()
            self.refresh_list()
            self.status_label.configure(text="История очищена")

    def _export(self):
        """Export all stored recordings to a .md or .txt file."""
        recordings = self.db.get_all_recordings(limit=10000)
        if not recordings:
            self.status_label.configure(text="Нечего экспортировать")
            return
        path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt")],
            initialfile=f"voicedrop_history_{datetime.now():%Y%m%d_%H%M}.md",
        )
        if not path:
            return
        is_md = path.lower().endswith(".md")
        try:
            blocks = []
            for r in recordings:
                ts = r.get('created_at', '')
                try:
                    ts = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    pass
                text = r.get('text', '')
                blocks.append(f"### {ts}\n\n{text}\n" if is_md else f"[{ts}]\n{text}\n")
            with open(path, 'w', encoding='utf-8') as f:
                f.write("\n".join(blocks))
            self.status_label.configure(
                text=f"Экспортировано {len(recordings)} → {os.path.basename(path)}"
            )
        except Exception as e:
            self.status_label.configure(text=f"Ошибка экспорта: {e}")

    def _copy_text(self, text: str):
        """Copy text to clipboard"""
        if self.on_copy_callback:
            self.on_copy_callback(text)
        self.status_label.configure(text="Скопировано в буфер обмена!")
        self.after(2000, lambda: self.status_label.configure(text=""))

    def show(self):
        """Show the window"""
        self.refresh_list()
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self):
        """Hide the window"""
        self.withdraw()
