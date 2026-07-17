import json
import os
import sqlite3
import threading
import time


PERSISTED_FIELDS = (
    "status",
    "url",
    "format",
    "format_id",
    "video_range_mode",
    "title",
    "preset",
    "options",
    "progress",
    "file",
    "filename",
    "error",
    "created_at",
    "updated_at",
    "completed_at",
)


class JobStore:
    """Small SQLite-backed task journal with one short connection per operation."""

    def __init__(self, path):
        self.path = os.path.realpath(path)
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self):
        with self.lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_updated_at ON jobs(updated_at DESC)"
            )

    def save(self, job_id, job):
        now = time.time()
        payload = {field: job.get(field) for field in PERSISTED_FIELDS if field in job}
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(id, payload, status, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (job_id, encoded, payload.get("status", "unknown"), payload["created_at"], now),
            )

    def load_recent(self, limit=200):
        limit = max(1, min(int(limit), 1000))
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            payload["id"] = row["id"]
            result.append(payload)
        return result

    def delete(self, job_id):
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def trim(self, keep=200):
        keep = max(20, min(int(keep), 1000))
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                DELETE FROM jobs WHERE id IN (
                    SELECT id FROM jobs ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (keep,),
            )
