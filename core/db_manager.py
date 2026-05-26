"""
Database Manager - SQLite storage for voice recordings history
"""
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config import DB_PATH, HISTORY_RETENTION_HOURS


def _to_db_string(dt: datetime) -> str:
    """Convert a (presumed-local-naive) datetime to the DB's UTC text format.

    The recordings table stores `created_at` as `CURRENT_TIMESTAMP` which
    SQLite writes as UTC text `'YYYY-MM-DD HH:MM:SS'` (space separator).
    Python's `datetime.isoformat()` uses `'T'` instead — so comparing the
    two as strings is broken at midnight boundaries (`' '` < `'T'`).
    Always run user-facing range bounds through this helper.
    """
    if dt.tzinfo is None:
        # Naive — treat as local time by attaching the system tz.
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class DatabaseManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.db_path = DB_PATH
        self.retention_hours = HISTORY_RETENTION_HOURS  # 0/None = keep forever
        self._local = threading.local()
        self._init_db()

    def set_retention(self, hours):
        """Set history retention in hours (0 or None = keep forever)."""
        try:
            self.retention_hours = int(hours)
        except (TypeError, ValueError):
            self.retention_hours = HISTORY_RETENTION_HOURS

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection"""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    def _init_db(self):
        """Initialize database schema, FTS5 mirror, and one-time backfill."""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recordings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                audio_duration_ms INTEGER,
                was_inserted BOOLEAN DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON recordings(created_at)
        """)
        # FTS5 contentless-external index over recordings.text. Uses unicode61
        # tokenizer with diacritic-folding so RU/UK search works regardless of
        # 'й' vs 'и', accent marks, etc. Kept in sync with the base table via
        # triggers below; on first run the existing rows are backfilled.
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS recordings_fts USING fts5(
                text,
                content='recordings',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS recordings_ai_fts AFTER INSERT ON recordings BEGIN
                INSERT INTO recordings_fts(rowid, text) VALUES (new.id, new.text);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS recordings_ad_fts AFTER DELETE ON recordings BEGIN
                INSERT INTO recordings_fts(recordings_fts, rowid, text) VALUES('delete', old.id, old.text);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS recordings_au_fts AFTER UPDATE ON recordings BEGIN
                INSERT INTO recordings_fts(recordings_fts, rowid, text) VALUES('delete', old.id, old.text);
                INSERT INTO recordings_fts(rowid, text) VALUES (new.id, new.text);
            END
        """)
        # One-time backfill on schema upgrade: if FTS has fewer docs than the
        # base table (e.g. we just added FTS), trigger a full rebuild. The
        # 'rebuild' command is the documented way to (re)populate an
        # external-content FTS5 table — manual bulk INSERTs register the
        # rowids but don't actually tokenize the text.
        fts_count = conn.execute("SELECT COUNT(*) FROM recordings_fts").fetchone()[0]
        rec_count = conn.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]
        if fts_count < rec_count:
            conn.execute(
                "INSERT INTO recordings_fts(recordings_fts) VALUES ('rebuild')"
            )
            logging.info(f"FTS5 rebuilt for {rec_count} recordings (was {fts_count})")
        conn.commit()

    def save_recording(self, text: str, audio_duration_ms: int = 0, was_inserted: bool = True) -> int:
        """Save a new recording to the database"""
        conn = self._get_connection()
        cursor = conn.execute(
            "INSERT INTO recordings (text, audio_duration_ms, was_inserted) VALUES (?, ?, ?)",
            (text, audio_duration_ms, was_inserted)
        )
        conn.commit()
        return cursor.lastrowid

    def get_recent_recordings(self, limit: int = 50) -> list[dict]:
        """Get recent recordings (within retention window, or all if unlimited)."""
        conn = self._get_connection()
        if self.retention_hours and self.retention_hours > 0:
            cutoff = datetime.now() - timedelta(hours=self.retention_hours)
            cursor = conn.execute(
                """
                SELECT id, text, created_at, audio_duration_ms, was_inserted
                FROM recordings WHERE created_at > ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (_to_db_string(cutoff), limit)
            )
        else:
            cursor = conn.execute(
                """
                SELECT id, text, created_at, audio_duration_ms, was_inserted
                FROM recordings ORDER BY created_at DESC LIMIT ?
                """,
                (limit,)
            )
        return [dict(row) for row in cursor.fetchall()]

    def search_recordings(self, query: str, limit: int = 300) -> list[dict]:
        """Full-text search using FTS5; LIKE fallback for queries FTS can't parse.

        Each whitespace-separated token gets a `*` prefix-match, joined by
        implicit AND. So "бэкап goog" matches "...бэкап на Google Диск...".
        Empty/whitespace-only query returns no results.
        """
        # Tokenize: keep only word chars (covers Cyrillic via \w + UNICODE).
        # This also strips any FTS operator characters (", *, OR, NOT) that
        # would otherwise let a stray quote crash the parser.
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            return []
        match_query = " ".join(f"{t}*" for t in tokens)

        conn = self._get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT r.id, r.text, r.created_at, r.audio_duration_ms, r.was_inserted
                FROM recordings r
                JOIN recordings_fts f ON f.rowid = r.id
                WHERE recordings_fts MATCH ?
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (match_query, limit)
            )
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError as e:
            # FTS5 not available, or a query the parser rejected despite the
            # sanitisation. Fall back to plain LIKE so the user still gets
            # SOMETHING rather than an opaque empty result.
            logging.warning(f"FTS search failed, falling back to LIKE: {e}")
            cursor = conn.execute(
                """
                SELECT id, text, created_at, audio_duration_ms, was_inserted
                FROM recordings WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?
                """,
                (f"%{query}%", limit)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_recordings_in_range(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Recordings whose created_at is within [start, end]. None = open-ended.

        Bounds are assumed local (naive == local) and converted to the DB's
        UTC text format before comparing — see _to_db_string.
        """
        conn = self._get_connection()
        clauses = []
        params: list = []
        if start is not None:
            clauses.append("created_at >= ?")
            params.append(_to_db_string(start))
        if end is not None:
            clauses.append("created_at <= ?")
            params.append(_to_db_string(end))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = conn.execute(
            f"""
            SELECT id, text, created_at, audio_duration_ms, was_inserted
            FROM recordings {where}
            ORDER BY created_at DESC LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_all_recordings(self, limit: int = 10000) -> list[dict]:
        """All stored recordings, newest first (for export)."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT id, text, created_at, audio_duration_ms, was_inserted
            FROM recordings ORDER BY created_at DESC LIMIT ?
            """,
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_last_recording(self) -> Optional[dict]:
        """Get the most recent recording"""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT id, text, created_at, audio_duration_ms, was_inserted FROM recordings ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def cleanup_old_recordings(self):
        """Delete recordings older than retention period (skip if unlimited)."""
        if not self.retention_hours or self.retention_hours <= 0:
            return  # keep forever
        conn = self._get_connection()
        cutoff = datetime.now() - timedelta(hours=self.retention_hours)
        conn.execute("DELETE FROM recordings WHERE created_at < ?", (_to_db_string(cutoff),))
        conn.commit()

    def clear_all(self):
        """Delete all recordings (privacy / manual clear)."""
        conn = self._get_connection()
        conn.execute("DELETE FROM recordings")
        conn.commit()

    def delete_recording(self, recording_id: int):
        """Delete a specific recording"""
        conn = self._get_connection()
        conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        conn.commit()

    def close(self):
        """Close database connection"""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None
