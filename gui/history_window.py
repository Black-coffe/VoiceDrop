"""
History Window - Shows recording history with copy / search / period filter.
"""
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime, date, time, timedelta
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


ICON_PATH = str(get_app_dir() / 'assets' / 'icon.ico')

# Locale month/weekday names for the day-group headers. Hard-coded rather
# than pulling in Babel because it would add ~9 MB to the PyInstaller bundle.
_MONTHS_RU = [
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]
_WEEKDAYS_RU_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']

# Period presets shown in the "Период" dropdown. Order matters for UX.
# Each value is (label, day-window). day-window=None means no time bound
# ("Всё время"); day-window=-1 is a sentinel for "Вчера" handled separately
# because it needs an upper bound, not just a lower one.
_PERIOD_PRESETS: list[tuple[str, Optional[int]]] = [
    ("Сегодня", 0),
    ("Вчера", -1),
    ("7 дней", 7),
    ("30 дней", 30),
    ("100 дней", 100),
    ("Всё время", None),
]
_DEFAULT_PERIOD = "Всё время"

# How many rows to fetch when the user looks at "Всё время" or runs a search.
# Each row turns into a few CTk widgets, so we cap this rather than letting
# the window try to render an unbounded list.
_LIST_LIMIT = 2000


def _format_day_header(d: date) -> str:
    """Friendly day label for a group header (Сегодня / Вчера / DD месяц YYYY · вс)."""
    today = date.today()
    delta_days = (today - d).days
    if delta_days == 0:
        return "Сегодня"
    if delta_days == 1:
        return "Вчера"
    weekday = _WEEKDAYS_RU_SHORT[d.weekday()]
    return f"{d.day} {_MONTHS_RU[d.month - 1]} {d.year} · {weekday}"


def _period_to_range(label: str) -> tuple[Optional[datetime], Optional[datetime]]:
    """Map a preset label to (start_dt, end_dt). Either side may be None."""
    now = datetime.now()
    today_start = datetime.combine(now.date(), time.min)
    if label == "Сегодня":
        return today_start, None
    if label == "Вчера":
        return today_start - timedelta(days=1), today_start
    if label == "7 дней":
        return now - timedelta(days=7), None
    if label == "30 дней":
        return now - timedelta(days=30), None
    if label == "100 дней":
        return now - timedelta(days=100), None
    return None, None  # "Всё время" / unknown


class HistoryWindow(ctk.CTkToplevel):
    def __init__(self, parent=None, on_copy_callback: Optional[Callable[[str], None]] = None):
        super().__init__(parent)

        self.on_copy_callback = on_copy_callback
        self.db = DatabaseManager()

        self.title("VoiceDrop - История записей")
        self.geometry("820x640")
        self.minsize(560, 420)

        self.after(200, self._set_icon)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._create_widgets()

    def _set_icon(self):
        try:
            if os.path.exists(ICON_PATH):
                self.iconbitmap(ICON_PATH)
        except Exception:
            pass

    def _create_widgets(self):
        # ── Header ────────────────────────────────────────────────────────
        self.header_frame = ctk.CTkFrame(self)
        self.header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        self.header_frame.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            self.header_frame,
            text="История записей",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.title_label.grid(row=0, column=0, sticky="w", padx=10, pady=(10, 5))

        self.refresh_btn = ctk.CTkButton(
            self.header_frame, text="Обновить", width=100, command=self.refresh_list
        )
        self.refresh_btn.grid(row=0, column=2, padx=10, pady=(10, 5))

        # Search row
        self.search_entry = ctk.CTkEntry(
            self.header_frame, placeholder_text="Поиск по тексту… (полнотекстовый)"
        )
        self.search_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(10, 5), pady=(0, 5))
        self.search_entry.bind("<KeyRelease>", lambda e: self._on_search_change())

        self.export_btn = ctk.CTkButton(
            self.header_frame, text="Экспорт", width=100, command=self._export
        )
        self.export_btn.grid(row=1, column=2, padx=10, pady=(0, 5))

        # Period row — preset dropdown plus explicit "от/до" date entries.
        # Both are visible at once; if the date entries parse cleanly they
        # override the preset, otherwise the preset wins. Keeps the UI flat
        # without modal dialogs.
        period_row = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        period_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

        ctk.CTkLabel(period_row, text="Период:").grid(row=0, column=0, padx=(0, 8))

        self.period_var = ctk.StringVar(value=_DEFAULT_PERIOD)
        self.period_menu = ctk.CTkOptionMenu(
            period_row,
            variable=self.period_var,
            values=[label for label, _ in _PERIOD_PRESETS],
            command=self._on_preset_change,
            width=130,
        )
        self.period_menu.grid(row=0, column=1)

        ctk.CTkLabel(period_row, text="  или с").grid(row=0, column=2, padx=(8, 4))
        self.from_entry = ctk.CTkEntry(period_row, width=100, placeholder_text="дд.мм.гггг")
        self.from_entry.grid(row=0, column=3)
        self.from_entry.bind("<KeyRelease>", lambda e: self.refresh_list())

        ctk.CTkLabel(period_row, text="по").grid(row=0, column=4, padx=(8, 4))
        self.to_entry = ctk.CTkEntry(period_row, width=100, placeholder_text="дд.мм.гггг")
        self.to_entry.grid(row=0, column=5)
        self.to_entry.bind("<KeyRelease>", lambda e: self.refresh_list())

        self.clear_btn = ctk.CTkButton(
            self.header_frame,
            text="Очистить",
            width=100,
            fg_color="#8B2E2E",
            hover_color="#A33A3A",
            command=self._clear_history,
        )
        self.clear_btn.grid(row=2, column=2, padx=10, pady=(0, 10))

        # ── Scrollable list ───────────────────────────────────────────────
        self.scroll_frame = ctk.CTkScrollableFrame(self)
        self.scroll_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(5, 10))
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        # ── Status bar ────────────────────────────────────────────────────
        self.status_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=12))
        self.status_label.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.refresh_list()
        self.protocol("WM_DELETE_WINDOW", self.hide)

    # ── Data loading ──────────────────────────────────────────────────────
    def _on_search_change(self):
        # When the user is searching, the period filter is bypassed so they
        # don't accidentally hide a matching record outside the window.
        # The period dropdown stays visible but becomes a no-op until the
        # search field is cleared again.
        self.refresh_list()

    def _on_preset_change(self, _value: str):
        # Picking a preset means the user wants that — clear any half-typed
        # explicit dates so the result lines up with their visible choice.
        if hasattr(self, 'from_entry'):
            self.from_entry.delete(0, tk.END)
        if hasattr(self, 'to_entry'):
            self.to_entry.delete(0, tk.END)
        self.refresh_list()

    def _parse_user_date(self, s: str, end_of_day: bool = False) -> Optional[datetime]:
        """Parse DD.MM.YYYY (or DD.MM, or DD-MM-YYYY). None if unparseable."""
        s = (s or "").strip()
        if not s:
            return None
        for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m"):
            try:
                dt = datetime.strptime(s, fmt)
                if "%Y" not in fmt:
                    dt = dt.replace(year=datetime.now().year)
                if end_of_day:
                    dt = dt.replace(hour=23, minute=59, second=59)
                return dt
            except ValueError:
                continue
        return None

    def _fetch_records(self) -> tuple[list[dict], str]:
        """Return (records, subtitle) for the current search/period/date state."""
        query = self.search_entry.get().strip() if hasattr(self, 'search_entry') else ""
        if query:
            records = self.db.search_recordings(query, limit=_LIST_LIMIT)
            return records, f"найдено по «{query}»"

        # Explicit date entries beat the preset combo when at least one parses.
        from_dt = self._parse_user_date(
            self.from_entry.get() if hasattr(self, 'from_entry') else ""
        )
        to_dt = self._parse_user_date(
            self.to_entry.get() if hasattr(self, 'to_entry') else "",
            end_of_day=True,
        )
        if from_dt or to_dt:
            records = self.db.get_recordings_in_range(
                start=from_dt, end=to_dt, limit=_LIST_LIMIT
            )
            parts = []
            if from_dt:
                parts.append(f"с {from_dt:%d.%m.%Y}")
            if to_dt:
                parts.append(f"по {to_dt:%d.%m.%Y}")
            return records, "произвольный · " + " ".join(parts)

        period = self.period_var.get() if hasattr(self, 'period_var') else _DEFAULT_PERIOD
        start, end = _period_to_range(period)
        if start is None and end is None:
            records = self.db.get_recordings_in_range(limit=_LIST_LIMIT)
            return records, "все записи"
        records = self.db.get_recordings_in_range(start=start, end=end, limit=_LIST_LIMIT)
        return records, f"период: {period.lower()}"

    def refresh_list(self):
        """Refresh the recordings list with current search/period filters."""
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        records, subtitle = self._fetch_records()

        if not records:
            empty = ctk.CTkLabel(
                self.scroll_frame,
                text="Нет записей по текущему фильтру",
                font=ctk.CTkFont(size=14),
            )
            empty.grid(row=0, column=0, pady=50)
            self.status_label.configure(text=f"0 записей · {subtitle}")
            return

        # Walk the records (already DESC by created_at) and emit a day-group
        # header whenever the day rolls over. _create_recording_item handles
        # the highlighting of the newest item.
        grid_row = 0
        last_day: Optional[date] = None
        for idx, rec in enumerate(records):
            try:
                rec_dt = datetime.fromisoformat(rec['created_at'])
                rec_day = rec_dt.date()
            except (ValueError, TypeError):
                rec_day = None

            if rec_day is not None and rec_day != last_day:
                self._create_day_header(grid_row, rec_day, idx, records)
                grid_row += 1
                last_day = rec_day

            self._create_recording_item(grid_row, rec, is_latest=(idx == 0))
            grid_row += 1

        self.status_label.configure(text=f"{len(records)} записей · {subtitle}")

    # ── Item / header widgets ────────────────────────────────────────────
    def _create_day_header(self, grid_row: int, day: date, start_idx: int, records: list[dict]):
        """Insert a slim day-divider row above the first record of `day`."""
        # Count how many records fall on this day (contiguous from start_idx).
        count = 0
        for r in records[start_idx:]:
            try:
                if datetime.fromisoformat(r['created_at']).date() == day:
                    count += 1
                else:
                    break
            except (ValueError, TypeError):
                break

        header = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
        header.grid(row=grid_row, column=0, sticky="ew", pady=(12 if grid_row else 2, 4))
        header.grid_columnconfigure(0, weight=1)

        label = ctk.CTkLabel(
            header,
            text=f"{_format_day_header(day)}  ·  {count}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#9BB8E0",
            anchor="w",
        )
        label.grid(row=0, column=0, sticky="w", padx=6)

    def _create_recording_item(self, grid_row: int, recording: dict, is_latest: bool):
        frame = ctk.CTkFrame(self.scroll_frame)
        frame.grid(row=grid_row, column=0, sticky="ew", pady=(5 if is_latest else 2))
        frame.grid_columnconfigure(1, weight=1)

        try:
            created_at = datetime.fromisoformat(recording['created_at'])
            time_str = created_at.strftime("%d.%m %H:%M")
        except (ValueError, TypeError):
            time_str = "??:??"

        time_label = ctk.CTkLabel(
            frame, text=time_str, font=ctk.CTkFont(size=11), width=90
        )
        time_label.grid(
            row=0, column=0, padx=(10, 5), pady=8, sticky="n" if is_latest else ""
        )

        text = recording.get('text', '')
        if is_latest:
            max_chars = 400
            display_text = text[:max_chars] + "..." if len(text) > max_chars else text
        else:
            display_text = text[:100] + "..." if len(text) > 100 else text

        text_label = ctk.CTkLabel(
            frame,
            text=display_text,
            font=ctk.CTkFont(size=12),
            anchor="w",
            justify="left",
            wraplength=550 if is_latest else 0,
        )
        text_label.grid(row=0, column=1, sticky="ew", padx=5, pady=(10 if is_latest else 8))

        copy_btn = ctk.CTkButton(
            frame,
            text="Копировать",
            width=90,
            height=28,
            command=lambda t=text: self._copy_text(t),
        )
        copy_btn.grid(row=0, column=2, padx=10, pady=8, sticky="n" if is_latest else "")

    # ── Actions ───────────────────────────────────────────────────────────
    def _clear_history(self):
        if messagebox.askyesno(
            "Очистить историю",
            "Удалить ВСЕ записи из истории? Это необратимо.",
            parent=self,
        ):
            self.db.clear_all()
            self.refresh_list()
            self.status_label.configure(text="История очищена")

    def _export(self):
        recordings = self.db.get_all_recordings(limit=100000)
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
        if self.on_copy_callback:
            self.on_copy_callback(text)
        self.status_label.configure(text="Скопировано в буфер обмена!")
        self.after(2000, lambda: self.status_label.configure(text=""))

    def show(self):
        self.refresh_list()
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide(self):
        self.withdraw()
