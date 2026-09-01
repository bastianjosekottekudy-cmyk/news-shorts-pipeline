"""SQLite run history for the Shorts dashboard."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import PROJECT_ROOT, get_env

DB_PATH = Path(get_env("DB_PATH", str(PROJECT_ROOT / "runs.db")))
_db_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_code TEXT NOT NULL,
                section_name TEXT NOT NULL,
                run_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                finished_at TEXT,
                news_title TEXT,
                news_json TEXT,
                script_path TEXT,
                video_path TEXT,
                youtube_video_id TEXT,
                error_message TEXT,
                steps_log TEXT,
                upload_status TEXT NOT NULL DEFAULT 'none',
                upload_error TEXT,
                batch_id INTEGER,
                news_link TEXT
            )
            """
        )
        for column, decl in (
            ("section_code", "TEXT"),
            ("section_name", "TEXT"),
            ("news_title", "TEXT"),
            ("news_json", "TEXT"),
            ("batch_id", "INTEGER"),
            ("news_link", "TEXT"),
            ("upload_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("upload_error", "TEXT"),
        ):
            _ensure_column(conn, "runs", column, decl)
        conn.execute(
            """
            UPDATE runs
            SET upload_status = 'uploaded'
            WHERE youtube_video_id IS NOT NULL
              AND youtube_video_id != ''
              AND youtube_video_id != 'skipped'
              AND (upload_status IS NULL OR upload_status = 'none' OR upload_status = '')
            """
        )
        conn.commit()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    with _db_lock:
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            conn = None
            try:
                conn = _connect()
                yield conn
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                if "locked" in str(exc).lower() and attempt < max_attempts:
                    time.sleep(0.15 * attempt)
                    continue
                raise
            except Exception:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise
            finally:
                if conn is not None:
                    conn.close()


def next_batch_id() -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(batch_id), 0) + 1 AS next_id FROM runs"
        ).fetchone()
        return int(row["next_id"])


def create_run(
    section_code: str,
    section_name: str,
    run_date: str,
    *,
    batch_id: int | None = None,
    news_title: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (
                section_code, section_name, run_date, status,
                started_at, steps_log, upload_status, batch_id, news_title
            )
            VALUES (?, ?, ?, 'running', ?, '[]', 'none', ?, ?)
            """,
            (
                section_code.lower(),
                section_name,
                run_date,
                now,
                batch_id,
                news_title,
            ),
        )
        return int(cur.lastrowid)


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [run_id]
    with db() as conn:
        conn.execute(f"UPDATE runs SET {columns} WHERE id = ?", values)


def append_step_log(run_id: int, step: str, detail: str = "") -> None:
    with db() as conn:
        row = conn.execute("SELECT steps_log FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return
        log: list[dict[str, str]] = json.loads(row["steps_log"] or "[]")
        log.append(
            {
                "step": step,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        conn.execute(
            "UPDATE runs SET steps_log = ? WHERE id = ?",
            (json.dumps(log), run_id),
        )


def finish_run(run_id: int, status: str, error_message: str | None = None) -> None:
    update_run(
        run_id,
        status=status,
        finished_at=datetime.now(timezone.utc).isoformat(),
        error_message=error_message,
    )


def set_upload_status(
    run_id: int,
    upload_status: str,
    *,
    youtube_video_id: str | None = None,
    upload_error: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "upload_status": upload_status,
        "upload_error": upload_error,
    }
    if youtube_video_id is not None:
        fields["youtube_video_id"] = youtube_video_id
    update_run(run_id, **fields)


def fail_orphaned_runs(
    error_message: str = "Interrupted by app restart",
) -> list[int]:
    now = datetime.now(timezone.utc).isoformat()
    failed_ids: list[int] = []
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM runs WHERE status = 'running'"
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            conn.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE status = 'running'
                """,
                (now, error_message),
            )
            failed_ids.extend(ids)

        upload_rows = conn.execute(
            "SELECT id FROM runs WHERE upload_status = 'uploading'"
        ).fetchall()
        upload_ids = [int(row["id"]) for row in upload_rows]
        if upload_ids:
            conn.execute(
                """
                UPDATE runs
                SET upload_status = 'failed',
                    upload_error = ?
                WHERE upload_status = 'uploading'
                """,
                (error_message,),
            )
            failed_ids.extend(upload_ids)

        for run_id in sorted(set(failed_ids)):
            row = conn.execute(
                "SELECT steps_log FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not row:
                continue
            log: list[dict[str, str]] = json.loads(row["steps_log"] or "[]")
            log.append(
                {
                    "step": "interrupted",
                    "detail": error_message,
                    "at": now,
                }
            )
            conn.execute(
                "UPDATE runs SET steps_log = ? WHERE id = ?",
                (json.dumps(log), run_id),
            )
    return sorted(set(failed_ids))


def get_run(run_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def delete_run(run_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return cur.rowcount > 0


def list_runs(
    section_code: str | None = None,
    run_date: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM runs"
    clauses: list[str] = []
    params: list[Any] = []
    if section_code:
        clauses.append("section_code = ?")
        params.append(section_code.lower())
    if run_date:
        clauses.append("run_date = ?")
        params.append(run_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY run_date DESC, section_code ASC, id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_failed_uploads(limit: int = 10) -> list[dict[str, Any]]:
    """Runs whose YouTube upload failed and still need a retry (capped to limit, default 10)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE upload_status = 'failed'
              AND status = 'success'
              AND video_path IS NOT NULL
              AND video_path != ''
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_failed_uploads() -> int:
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE upload_status = 'failed'
              AND status = 'success'
              AND video_path IS NOT NULL
              AND video_path != ''
            """
        ).fetchone()
        return int(row[0])


def list_run_dates() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC"
        ).fetchall()
        return [row[0] for row in rows]


def count_runs_today() -> dict[str, int]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_date = ?", (today,)
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_date = ? AND status = 'failed'",
            (today,),
        ).fetchone()[0]
        success = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_date = ? AND status = 'success'",
            (today,),
        ).fetchone()[0]
        running = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'running'", ()
        ).fetchone()[0]
        uploading = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE upload_status = 'uploading'", ()
        ).fetchone()[0]
    return {
        "today_total": total,
        "today_success": success,
        "today_failed": failed,
        "running": running,
        "uploading": uploading,
    }
