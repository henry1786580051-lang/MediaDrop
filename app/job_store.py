import json
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager


ACTIVE_STATUSES = frozenset(("queued", "starting", "downloading", "paused", "interrupted"))


def is_recoverable_job(job):
    return job.get("status") in ACTIVE_STATUSES or (
        job.get("status") == "error" and bool(job.get("resumable"))
    )


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
    "error_code",
    "error_detail",
    "resumable",
    "attempts",
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

    @contextmanager
    def _connection(self):
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    def _initialize(self):
        with self.lock, self._connection() as connection:
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
        with self.lock, self._connection() as connection:
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
        with self.lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return self._decode_rows(rows)

    @staticmethod
    def _decode_rows(rows):
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            payload["id"] = row["id"]
            result.append(payload)
        return result

    def load_for_restore(self, history_limit=200):
        history_limit = max(1, min(int(history_limit), 1000))
        with self.lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM jobs ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        history_count = 0
        for job in self._decode_rows(rows):
            if is_recoverable_job(job):
                result.append(job)
            elif history_count < history_limit:
                result.append(job)
                history_count += 1
        return result

    def delete(self, job_id):
        with self.lock, self._connection() as connection:
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def trim(self, keep=200):
        keep = max(20, min(int(keep), 1000))
        with self.lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT id, payload FROM jobs ORDER BY updated_at DESC"
            ).fetchall()
            terminal = [job for job in self._decode_rows(rows) if not is_recoverable_job(job)]
            connection.executemany(
                "DELETE FROM jobs WHERE id = ?",
                [(job["id"],) for job in terminal[keep:]],
            )
