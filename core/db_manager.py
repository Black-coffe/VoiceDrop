"""
Database Manager - SQLite storage for voice recordings history
"""
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import DB_PATH, HISTORY_RETENTION_HOURS


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
        """Initialize database schema"""
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
                (cutoff.isoformat(), limit)
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
        """Search recordings whose text contains query (case-insensitive)."""
        conn = self._get_connection()
        cursor = conn.execute(
            """
            SELECT id, text, created_at, audio_duration_ms, was_inserted
            FROM recordings WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?
            """,
            (f"%{query}%", limit)
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
        conn.execute("DELETE FROM recordings WHERE created_at < ?", (cutoff.isoformat(),))
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
