"""FastAPI local Shorts library dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from src.config import (
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    GOOGLE_NEWS_TOPICS,
    OUTPUT_DIR,
    add_section,
    get_section,
    load_pipeline_concurrency,
    load_pipeline_config,
    load_sections,
    local_run_date,
    remove_section,
    update_pipeline_concurrency,
    update_section_schedule,
)
from src import job_control
from src.job_control import JobStoppedError, check_stop
from src.db import store
from src.naming import title_from_video_path
from src.pipeline import retry_single_short, run_section_batch
from src.scheduler import get_next_run_times, reload_section_jobs
from src.youtube.auth import (
    authorize_client_interactive,
    get_auth_session_status,
    probe_youtube_clients,
    start_auth_session,
    try_silent_refresh,
)

logger = logging.getLogger(__name__)


class SectionScheduleIn(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)


class NewSectionIn(BaseModel):
    name: str
    code: str = ""
    google_topic: str = ""
    search_query: str = ""
    rss_url: str = ""
    region: str = "US"
    news_count: int = Field(default=5, ge=1, le=15)
    schedule_enabled: bool = True
    schedule_hour: int = Field(default=DEFAULT_SCHEDULE_HOUR, ge=0, le=23)
    schedule_minute: int = Field(default=DEFAULT_SCHEDULE_MINUTE, ge=0, le=59)

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(title="News Shorts Library")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

_running_lock = threading.Lock()
_running_sections: set[str] = set()
_upload_lock = threading.Lock()
_uploading_runs: set[int] = set()

class ConcurrencyLimiter:
    """Thread-safe dynamic concurrency limiter supporting runtime limit updates."""

    def __init__(self, limit: int = 5):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._active = 0
        self._limit = max(1, int(limit))

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    def set_limit(self, new_limit: int) -> None:
        with self._cv:
            self._limit = max(1, int(new_limit))
            self._cv.notify_all()

    def acquire(self) -> None:
        with self._cv:
            while self._active >= self._limit:
                self._cv.wait()
            self._active += 1

    def release(self) -> None:
        with self._cv:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


_concurrency_init = load_pipeline_concurrency()
_generate_semaphore = ConcurrencyLimiter(_concurrency_init["effective_limit"])


def _youtube_enabled() -> bool:
    return bool(load_pipeline_config().get("youtube", {}).get("enabled", False))


def _parse_json_field(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _duration(started: str | None, finished: str | None) -> str:
    if not started:
        return "—"
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(finished) if finished else datetime.now(timezone.utc)
        secs = int((end - start).total_seconds())
        mins, s = divmod(secs, 60)
        return f"{mins}m {s}s"
    except ValueError:
        return "—"


def _video_exists(run: dict[str, Any]) -> bool:
    path = run.get("video_path")
    return bool(path and Path(path).is_file())


def _safe_video_path(run: dict[str, Any]) -> Path:
    raw = run.get("video_path")
    if not raw:
        raise HTTPException(status_code=404, detail="No video for this run")
    path = Path(raw).resolve()
    output_root = OUTPUT_DIR.resolve()
    try:
        path.relative_to(output_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid video path") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found on disk")
    return path


def _run_output_dir(run: dict[str, Any]) -> Path | None:
    output_root = OUTPUT_DIR.resolve()
    run_id = run.get("id")
    run_date = run.get("run_date")
    section = run.get("section_code")

    if run_id and run_date and section:
        candidate = (
            OUTPUT_DIR / str(run_date) / str(section).lower() / f"run_{run_id}"
        ).resolve()
        try:
            candidate.relative_to(output_root)
        except ValueError:
            return None
        if candidate.is_dir():
            return candidate

    video_path = run.get("video_path")
    if video_path:
        path = Path(video_path).resolve()
        try:
            path.relative_to(output_root)
        except ValueError:
            return None
        parent = path.parent
        if parent.name.startswith("run_"):
            return parent
    return None


def _other_runs_use_path(run_id: int, directory: Path) -> bool:
    directory = directory.resolve()
    for other in store.list_runs(limit=500):
        if other.get("id") == run_id:
            continue
        for key in ("video_path", "script_path"):
            raw = other.get(key)
            if not raw:
                continue
            try:
                Path(raw).resolve().relative_to(directory)
                return True
            except ValueError:
                continue
    return False


def _delete_run_artifacts(run: dict[str, Any]) -> list[str]:
    deleted: list[str] = []
    run_id = int(run["id"])
    out_dir = _run_output_dir(run)

    if out_dir and out_dir.is_dir():
        if _other_runs_use_path(run_id, out_dir):
            logger.warning(
                "Skip folder delete for run %s — other runs share %s",
                run_id,
                out_dir,
            )
        else:
            shutil.rmtree(out_dir)
            deleted.append(str(out_dir))
            for parent in (out_dir.parent, out_dir.parent.parent):
                try:
                    if parent.is_dir() and parent.resolve() != OUTPUT_DIR.resolve():
                        if not any(parent.iterdir()):
                            parent.rmdir()
                            deleted.append(str(parent))
                except OSError:
                    pass
            return deleted

    for key in ("video_path", "script_path"):
        raw = run.get(key)
        if not raw:
            continue
        path = Path(raw).resolve()
        try:
            path.relative_to(OUTPUT_DIR.resolve())
        except ValueError:
            continue
        if path.is_file():
            path.unlink()
            deleted.append(str(path))
    return deleted


def _normalize_upload_status(run: dict[str, Any]) -> str:
    status = (run.get("upload_status") or "none").strip().lower()
    if status in ("uploading", "failed", "uploaded"):
        return status
    yt_id = (run.get("youtube_video_id") or "").strip()
    if yt_id and yt_id != "skipped":
        return "uploaded"
    return "none"


def _enrich_run(run: dict[str, Any]) -> dict[str, Any]:
    run["duration"] = _duration(run.get("started_at"), run.get("finished_at"))
    run["has_video"] = _video_exists(run)
    news_title = run.get("news_title") or ""
    run["video_title"] = title_from_video_path(
        run.get("video_path"),
        section_name=str(run.get("section_name") or news_title),
        run_date=str(run.get("run_date") or ""),
    )
    run["batch_label"] = (
        f"batch_{run['batch_id']}" if run.get("batch_id") is not None else ""
    )
    upload_status = _normalize_upload_status(run)
    run["upload_status"] = upload_status
    yt_id = (run.get("youtube_video_id") or "").strip()
    run["is_uploaded"] = upload_status == "uploaded" and bool(yt_id) and yt_id != "skipped"
    run["youtube_url"] = (
        f"https://www.youtube.com/watch?v={yt_id}" if run["is_uploaded"] else ""
    )
    run["can_upload"] = bool(run["has_video"] and run.get("status") not in ("running", "queued"))
    run["can_stop"] = bool(run.get("status") in ("running", "queued"))
    run["can_retry"] = bool(run.get("status") not in ("running", "queued"))
    run["upload_label"] = (
        "Re-upload" if upload_status in ("uploaded", "failed") else "Upload"
    )

    if run.get("status") == "queued":
        run["display_status"] = "queued"
    elif run.get("status") == "stopped":
        run["display_status"] = "stopped"
    elif upload_status == "uploading":
        run["display_status"] = "uploading"
    elif run["is_uploaded"]:
        run["display_status"] = "uploaded"
    elif upload_status == "failed" and run["has_video"]:
        run["display_status"] = "upload-failed"
    elif run.get("status") == "success" and not run["has_video"]:
        run["display_status"] = "missing"
    elif run.get("status") == "success" and run["has_video"]:
        run["display_status"] = "ready"
    else:
        run["display_status"] = run.get("status")
    return run


def _upload_run_video(run_id: int) -> None:
    run = store.get_run(run_id)
    if not run:
        return
    try:
        path = _safe_video_path(run)
    except HTTPException as exc:
        store.set_upload_status(run_id, "failed", upload_error=str(exc.detail))
        store.append_step_log(run_id, "upload", f"Upload failed: {exc.detail}")
        return

    try:
        section = get_section(str(run["section_code"]))
    except ValueError as exc:
        store.set_upload_status(run_id, "failed", upload_error=str(exc))
        return

    news = _parse_json_field(run.get("news_json")) or []
    if isinstance(news, dict):
        news_items = [news]
    elif isinstance(news, list) and news:
        news_items = news
    else:
        news_items = [
            {
                "title": run.get("news_title") or path.stem,
                "link": run.get("news_link") or "",
                "summary": "",
            }
        ]

    from src.pipeline import _attempt_youtube_upload

    _attempt_youtube_upload(
        run_id,
        str(path),
        section,
        news_items,
        str(run.get("run_date") or local_run_date(section)),
    )


def _group_by_date(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for run in runs:
        date = run.get("run_date") or "unknown"
        grouped.setdefault(date, []).append(run)
    return [{"date": date, "runs": items} for date, items in grouped.items()]


def _scheduled_run(section_code: str) -> None:
    code = section_code.lower()
    try:
        section = next(s for s in load_sections() if s.code == code)
    except StopIteration:
        return

    with _running_lock:
        if code in _running_sections:
            logger.warning(
                "Skipping scheduled run for %s — already running or queued",
                code,
            )
            return
        _running_sections.add(code)

    r_date = local_run_date(section)
    b_id = store.next_batch_id()
    rid = store.create_run(
        section.code,
        section.name,
        r_date,
        batch_id=b_id,
        news_title=f"{section.name} Short",
        status="queued",
    )
    store.append_step_log(rid, "queued", f"Scheduled run queued for section {section.name}")

    try:
        check_stop(rid, code)
        with _generate_semaphore:
            check_stop(rid, code)
            store.update_run(rid, status="running")
            run_section_batch(
                code,
                run_date=r_date,
                skip_upload=not _youtube_enabled(),
                existing_run_id=rid,
                batch_id=b_id,
            )
    except JobStoppedError:
        logger.info("Scheduled run %s for %s stopped by user", rid, code)
        store.stop_run(rid, reason="Stopped by user")
    except Exception as exc:
        logger.exception("Scheduled run failed for %s", code)
        store.finish_run(rid, "failed", error_message=str(exc))
    finally:
        with _running_lock:
            _running_sections.discard(code)


def _retry_failed_uploads() -> None:
    """Re-attempt YouTube uploads that previously failed (capped to at most 10 per schedule)."""
    if not _youtube_enabled():
        from src.scheduler import sync_failed_upload_retry_job

        sync_failed_upload_retry_job()
        return

    failed = store.list_failed_uploads(limit=10)
    if not failed:
        from src.scheduler import sync_failed_upload_retry_job

        sync_failed_upload_retry_job()
        return

    logger.info("Retrying %s failed YouTube upload(s) (limit 10)", len(failed))
    for run in failed:
        run_id = int(run["id"])
        if run.get("status") == "running":
            continue
        if not _video_exists(run):
            logger.warning(
                "Skipping upload retry for run %s — video file missing",
                run_id,
            )
            continue

        with _upload_lock:
            if run_id in _uploading_runs:
                continue
            _uploading_runs.add(run_id)

        store.set_upload_status(run_id, "uploading", upload_error=None)
        store.append_step_log(run_id, "upload", "Scheduled 6h retry of failed upload")
        try:
            _upload_run_video(run_id)
        except Exception:
            logger.exception("Scheduled 6h upload retry failed for run %s", run_id)
            current = store.get_run(run_id)
            if current and (current.get("upload_status") or "") == "uploading":
                store.set_upload_status(
                    run_id,
                    "failed",
                    upload_error="6h retry crashed unexpectedly",
                )
        finally:
            with _upload_lock:
                _uploading_runs.discard(run_id)

    from src.scheduler import sync_failed_upload_retry_job

    # Keep or clear the job based on whether failures remain.
    sync_failed_upload_retry_job()


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    section: str | None = None,
    date: str | None = None,
    youtube_flash: str | None = None,
) -> HTMLResponse:
    runs = [
        _enrich_run(r)
        for r in store.list_runs(section_code=section, run_date=date)
    ]
    groups = _group_by_date(runs)
    stats = store.count_runs_today()
    sections = load_sections()
    available_dates = store.list_run_dates()
    next_runs = get_next_run_times()
    next_map = {str(item.get("section_code")): item for item in next_runs}
    schedule_rows = [
        {
            "code": s.code,
            "name": s.name,
            "enabled": s.schedule_enabled,
            "time_ist": s.schedule_time_ist,
            "next_run": (next_map.get(s.code) or {}).get("next_run") or "—",
            "schedule": (next_map.get(s.code) or {}).get("schedule") or "",
        }
        for s in sections
    ]
    has_running = any(r["status"] == "running" for r in runs) or stats.get("running", 0) > 0
    has_queued = any(r["status"] == "queued" for r in runs) or stats.get("queued", 0) > 0
    has_uploading = (
        any(r.get("upload_status") == "uploading" for r in runs)
        or stats.get("uploading", 0) > 0
    )
    youtube_on = _youtube_enabled()
    youtube_clients: list[dict[str, Any]] = []
    if youtube_on:
        try:
            youtube_clients = probe_youtube_clients(attempt_refresh=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube client probe failed: %s", exc)
            youtube_clients = []
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "groups": groups,
            "stats": stats,
            "sections": sections,
            "available_dates": available_dates,
            "next_runs": next_runs,
            "schedule_rows": schedule_rows,
            "filter_section": (section or "").lower(),
            "filter_date": date or "",
            "has_running": has_running,
            "has_queued": has_queued,
            "has_uploading": has_uploading,
            "youtube_enabled": youtube_on,
            "youtube_clients": youtube_clients,
            "youtube_auth_warning": any(
                c.get("status") != "ok" for c in youtube_clients
            ),
            "youtube_flash": youtube_flash or "",
            "google_news_topics": GOOGLE_NEWS_TOPICS,
            "default_schedule_time": f"{DEFAULT_SCHEDULE_HOUR:02d}:{DEFAULT_SCHEDULE_MINUTE:02d}",
            "concurrency_enabled": load_pipeline_concurrency()["concurrency_enabled"],
            "max_parallel_jobs": load_pipeline_concurrency()["max_parallel_jobs"],
            "effective_concurrency": _generate_semaphore.limit,
        },
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int) -> HTMLResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _enrich_run(run)
    run["news"] = _parse_json_field(run.get("news_json"))
    run["steps"] = _parse_json_field(run.get("steps_log")) or []
    script_content = ""
    script_path = run.get("script_path")
    if script_path and Path(script_path).exists():
        script_content = Path(script_path).read_text(encoding="utf-8")
    run["script_content"] = script_content
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run},
    )


@app.get("/api/youtube/status")
async def youtube_status() -> JSONResponse:
    if not _youtube_enabled():
        return JSONResponse({"enabled": False, "clients": []})
    return JSONResponse(
        {
            "enabled": True,
            "clients": probe_youtube_clients(attempt_refresh=False),
        }
    )


@app.post("/api/youtube/clients/{client_id}/refresh")
async def youtube_client_refresh(client_id: str) -> JSONResponse:
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        result = try_silent_refresh(client_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(result)


@app.get("/api/youtube/clients/{client_id}/auth-status")
async def youtube_client_auth_status(client_id: str) -> JSONResponse:
    """Check status of an active or recent interactive OAuth session."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        return JSONResponse(get_auth_session_status(client_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/youtube/clients/{client_id}/start-auth")
async def youtube_client_start_auth(client_id: str) -> JSONResponse:
    """Start loopback listener with timeout and return auth_url for browser."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        return JSONResponse(start_auth_session(client_id))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/youtube/clients/{client_id}/authorize")
async def youtube_client_authorize(client_id: str) -> JSONResponse:
    """Start interactive OAuth session and return auth_url."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        session_info = start_auth_session(client_id)
        return JSONResponse(
            {
                "ok": True,
                "needs_browser": True,
                "auth_url": session_info.get("auth_url", ""),
                "status": session_info.get("status", "pending"),
                "detail": session_info.get("detail", "Sign in with Google in browser"),
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/youtube/clients/{client_id}/authorize")
async def youtube_client_authorize_redirect(client_id: str) -> RedirectResponse:
    """Redirect user's browser directly to Google OAuth sign-in."""
    if not _youtube_enabled():
        raise HTTPException(status_code=400, detail="YouTube upload is disabled")
    try:
        session_info = start_auth_session(client_id)
        return RedirectResponse(session_info["auth_url"], status_code=302)
    except Exception as exc:  # noqa: BLE001
        logger.warning("YouTube start-auth failed for %s: %s", client_id, exc)
        return RedirectResponse(
            "/?youtube_flash=" + quote(f"{client_id}: auth failed ({exc})"),
            status_code=302,
        )


@app.get("/videos/{run_id}/file")
async def video_file(run_id: int) -> FileResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    path = _safe_video_path(run)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/videos/{run_id}/download")
async def video_download(run_id: int) -> FileResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    path = _safe_video_path(run)
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=path.name,
        content_disposition_type="attachment",
    )


def _run_is_uploaded(run: dict[str, Any]) -> bool:
    upload_status = _normalize_upload_status(run)
    yt_id = (run.get("youtube_video_id") or "").strip()
    return upload_status == "uploaded" and bool(yt_id) and yt_id != "skipped"


def _delete_run_if_idle(run: dict[str, Any]) -> dict[str, Any]:
    run_id = int(run["id"])
    if run.get("status") in ("running", "queued"):
        return {"run_id": run_id, "ok": False, "reason": run.get("status")}
    if (run.get("upload_status") or "") == "uploading" or run_id in _uploading_runs:
        return {"run_id": run_id, "ok": False, "reason": "uploading"}

    deleted_paths = _delete_run_artifacts(run)
    store.delete_run(run_id)
    logger.info("Deleted run %s and artifacts: %s", run_id, deleted_paths)
    return {"run_id": run_id, "ok": True, "deleted_paths": deleted_paths}


@app.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: int) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    result = _delete_run_if_idle(run)
    if not result["ok"]:
        reason = result.get("reason")
        if reason == "running":
            raise HTTPException(status_code=409, detail="Cannot delete a running job")
        raise HTTPException(status_code=409, detail="Cannot delete while uploading")
    return JSONResponse(
        {
            "ok": True,
            "run_id": run_id,
            "deleted_paths": result.get("deleted_paths", []),
        }
    )


@app.post("/api/runs/delete-bulk")
async def api_delete_runs_bulk(scope: str = "all") -> JSONResponse:
    """Delete many local runs. scope: all | uploaded | failed (local files + DB rows only)."""
    scope_key = (scope or "all").strip().lower()
    if scope_key not in ("all", "uploaded", "failed"):
        raise HTTPException(
            status_code=400, detail="scope must be 'all', 'uploaded', or 'failed'"
        )

    runs = store.list_runs(limit=5000)
    if scope_key == "uploaded":
        runs = [r for r in runs if _run_is_uploaded(r)]
    elif scope_key == "failed":
        runs = [r for r in runs if r.get("status") in ("failed", "stopped")]

    deleted: list[int] = []
    skipped: list[dict[str, Any]] = []
    for run in runs:
        result = _delete_run_if_idle(run)
        if result["ok"]:
            deleted.append(int(result["run_id"]))
        else:
            skipped.append(
                {"run_id": result["run_id"], "reason": result.get("reason")}
            )

    logger.info(
        "Bulk delete scope=%s deleted=%s skipped=%s",
        scope_key,
        len(deleted),
        len(skipped),
    )
    return JSONResponse(
        {
            "ok": True,
            "scope": scope_key,
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "skipped": skipped,
        }
    )


@app.post("/api/runs/delete-failed")
async def api_delete_failed_runs() -> JSONResponse:
    """Delete all failed and stopped runs from disk and database."""
    return await api_delete_runs_bulk(scope="failed")


@app.post("/api/runs/{run_id}/upload")
async def api_upload_run(run_id: int, background_tasks: BackgroundTasks) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") == "running":
        raise HTTPException(status_code=409, detail="Wait for generation to finish")
    if not _video_exists(run):
        raise HTTPException(status_code=400, detail="No local video to upload")
    if (run.get("upload_status") or "") == "uploading":
        raise HTTPException(status_code=409, detail="Upload already in progress")

    with _upload_lock:
        if run_id in _uploading_runs:
            raise HTTPException(status_code=409, detail="Upload already in progress")
        _uploading_runs.add(run_id)

    store.set_upload_status(run_id, "uploading", upload_error=None)

    def _bg() -> None:
        try:
            _upload_run_video(run_id)
        finally:
            with _upload_lock:
                _uploading_runs.discard(run_id)

    background_tasks.add_task(_bg)
    return JSONResponse({"run_id": run_id, "status": "uploading"})


@app.get("/api/sections")
async def api_sections() -> JSONResponse:
    sections = [
        {
            "code": s.code,
            "name": s.name,
            "news_count": s.news_count,
            "timezone": s.timezone,
            "schedule_enabled": s.schedule_enabled,
            "schedule_hour": s.schedule_hour,
            "schedule_minute": s.schedule_minute,
            "schedule_time_ist": s.schedule_time_ist,
            "google_topic": s.google_topic,
            "search_query": s.search_query,
            "region": s.region,
        }
        for s in load_sections()
    ]
    return JSONResponse({"sections": sections, "next_runs": get_next_run_times()})


@app.patch("/api/sections/{section_code}/schedule")
async def api_section_schedule(section_code: str, body: SectionScheduleIn) -> JSONResponse:
    if body.enabled is None and body.hour is None and body.minute is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    try:
        section = update_section_schedule(
            section_code,
            enabled=body.enabled,
            hour=body.hour,
            minute=body.minute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    reload_section_jobs()
    return JSONResponse(
        {
            "ok": True,
            "section": section.code,
            "schedule_enabled": section.schedule_enabled,
            "schedule_time_ist": section.schedule_time_ist,
            "next_runs": get_next_run_times(),
        }
    )


@app.post("/api/sections")
async def api_add_section(body: NewSectionIn) -> JSONResponse:
    try:
        section = add_section(
            name=body.name,
            code=body.code,
            google_topic=body.google_topic,
            search_query=body.search_query,
            rss_url=body.rss_url,
            region=body.region,
            news_count=body.news_count,
            schedule_enabled=body.schedule_enabled,
            schedule_hour=body.schedule_hour,
            schedule_minute=body.schedule_minute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    reload_section_jobs()
    return JSONResponse(
        {
            "ok": True,
            "section": {
                "code": section.code,
                "name": section.name,
                "news_count": section.news_count,
                "schedule_enabled": section.schedule_enabled,
                "schedule_time_ist": section.schedule_time_ist,
            },
        }
    )


@app.delete("/api/sections/{section_code}")
async def api_remove_section(section_code: str) -> JSONResponse:
    code = section_code.lower()
    with _running_lock:
        if code in _running_sections:
            raise HTTPException(
                status_code=409,
                detail=f"{code} is generating — wait until it finishes",
            )
    try:
        removed = remove_section(code)
    except ValueError as exc:
        status = 404 if "Unknown" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    reload_section_jobs()
    return JSONResponse({"ok": True, "removed": removed})


@app.get("/api/concurrency")
async def api_get_concurrency() -> JSONResponse:
    state = load_pipeline_concurrency()
    return JSONResponse(
        {
            "ok": True,
            "status": "ok",
            "enabled": state["concurrency_enabled"],
            "max_parallel_jobs": state["max_parallel_jobs"],
            "effective_limit": _generate_semaphore.limit,
            "active_jobs": _generate_semaphore.active_count,
        }
    )


@app.post("/api/concurrency")
async def api_set_concurrency(request: Request) -> JSONResponse:
    payload: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    else:
        try:
            form = await request.form()
            for k, v in form.items():
                payload[k] = v
        except Exception:
            payload = {}

    enabled: bool | None = None
    if "enabled" in payload:
        val = payload["enabled"]
        if isinstance(val, bool):
            enabled = val
        elif isinstance(val, str):
            enabled = val.strip().lower() in ("true", "1", "on", "yes")

    max_parallel_jobs: int | None = None
    if "max_parallel_jobs" in payload:
        try:
            max_parallel_jobs = int(payload["max_parallel_jobs"])
        except (ValueError, TypeError):
            pass

    updated = update_pipeline_concurrency(
        enabled=enabled,
        max_parallel=max_parallel_jobs,
    )
    _generate_semaphore.set_limit(updated["effective_limit"])

    return JSONResponse(
        {
            "ok": True,
            "status": "ok",
            "message": "Concurrency settings updated",
            "enabled": updated["concurrency_enabled"],
            "max_parallel_jobs": updated["max_parallel_jobs"],
            "effective_limit": updated["effective_limit"],
            "active_jobs": _generate_semaphore.active_count,
        }
    )


@app.get("/api/runs")
async def api_runs(
    section: str | None = None,
    date: str | None = None,
) -> JSONResponse:
    runs = [
        _enrich_run(r)
        for r in store.list_runs(section_code=section, run_date=date)
    ]
    return JSONResponse({"runs": runs, "groups": _group_by_date(runs)})


@app.post("/api/trigger/{section_code}")
async def api_trigger(
    section_code: str,
    mock: bool = False,
    async_run: bool = True,
) -> JSONResponse:
    code = section_code.lower()
    try:
        section = next(s for s in load_sections() if s.code == code)
    except StopIteration:
        raise HTTPException(status_code=404, detail=f"Unknown section: {code}") from None

    with _running_lock:
        if code in _running_sections:
            raise HTTPException(status_code=409, detail=f"{code} is already running or queued")
        _running_sections.add(code)

    r_date = local_run_date(section)
    b_id = store.next_batch_id()
    rid = store.create_run(
        section.code,
        section.name,
        r_date,
        batch_id=b_id,
        news_title=f"{section.name} Short",
        status="queued",
    )
    store.append_step_log(rid, "queued", f"Job queued for section {section.name}")

    def _bg() -> None:
        try:
            check_stop(rid, code)
            with _generate_semaphore:
                check_stop(rid, code)
                store.update_run(rid, status="running")
                run_section_batch(
                    code,
                    run_date=r_date,
                    news_provider="mock" if mock else "google_news_rss",
                    skip_upload=not _youtube_enabled(),
                    existing_run_id=rid,
                    batch_id=b_id,
                )
        except JobStoppedError:
            logger.info("Job %s for %s stopped by user", rid, code)
            store.stop_run(rid, reason="Stopped by user")
        except Exception as exc:
            logger.exception("Background batch failed for %s", code)
            store.finish_run(rid, "failed", error_message=str(exc))
        finally:
            with _running_lock:
                _running_sections.discard(code)

    if async_run:
        threading.Thread(target=_bg, name=f"run-{code}", daemon=True).start()
        return JSONResponse(
            {
                "status": "queued",
                "run_id": rid,
                "section": code,
                "news_count": section.news_count,
            }
        )

    _bg()
    return JSONResponse(
        {"run_ids": [rid], "status": "completed", "section": code}
    )


@app.post("/api/trigger-all")
async def api_trigger_all(
    mock: bool = False,
) -> JSONResponse:
    sections = load_sections()
    started_sections: list[str] = []
    skipped_sections: list[str] = []
    queued_run_ids: list[int] = []

    def _worker(sec_code: str, rid: int, b_id: int, r_date: str) -> None:
        try:
            check_stop(rid, sec_code)
            with _generate_semaphore:
                check_stop(rid, sec_code)
                store.update_run(rid, status="running")
                run_section_batch(
                    sec_code,
                    run_date=r_date,
                    news_provider="mock" if mock else "google_news_rss",
                    skip_upload=not _youtube_enabled(),
                    existing_run_id=rid,
                    batch_id=b_id,
                )
        except JobStoppedError:
            logger.info("Job %s for %s stopped by user", rid, sec_code)
            store.stop_run(rid, reason="Stopped by user")
        except Exception as exc:
            logger.exception("Background batch failed for %s", sec_code)
            store.finish_run(rid, "failed", error_message=str(exc))
        finally:
            with _running_lock:
                _running_sections.discard(sec_code)

    with _running_lock:
        for section in sections:
            code = section.code.lower()
            if code in _running_sections:
                logger.warning("Skip %s — already running or queued", code)
                skipped_sections.append(code)
                continue
            _running_sections.add(code)
            started_sections.append(code)

            r_date = local_run_date(section)
            b_id = store.next_batch_id()
            rid = store.create_run(
                section.code,
                section.name,
                r_date,
                batch_id=b_id,
                news_title=f"{section.name} Short",
                status="queued",
            )
            store.append_step_log(rid, "queued", f"Job queued for section {section.name}")
            queued_run_ids.append(rid)

            threading.Thread(
                target=_worker,
                args=(code, rid, b_id, r_date),
                name=f"batch-{code}",
                daemon=True,
            ).start()

    return JSONResponse(
        {
            "status": "started",
            "sections": started_sections,
            "skipped": skipped_sections,
            "queued_run_ids": queued_run_ids,
        }
    )


@app.post("/api/runs/{run_id}/stop")
def api_stop_run(run_id: int) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") not in ("running", "queued"):
        return JSONResponse({
            "status": "ignored",
            "message": f"Run {run_id} is not running or queued (status: {run.get('status')})",
            "run_id": run_id,
        })

    job_control.request_stop_run(run_id)
    store.stop_run(run_id, reason="Stopped by user from dashboard")

    sec_code = str(run.get("section_code", "")).lower()
    if sec_code:
        with _running_lock:
            _running_sections.discard(sec_code)

    return JSONResponse({"status": "stopped", "run_id": run_id, "section": sec_code})


@app.post("/api/sections/{section_code}/stop")
def api_stop_section(section_code: str) -> JSONResponse:
    code = section_code.lower()
    stopped_ids = job_control.request_stop_section(code)
    for rid in stopped_ids:
        store.stop_run(rid, reason=f"Stopped section '{code}' by user")
    with store.db() as conn:
        rows = conn.execute(
            "SELECT id FROM runs WHERE section_code = ? AND status IN ('running', 'queued')",
            (code,),
        ).fetchall()
        for row in rows:
            store.stop_run(int(row["id"]), reason=f"Stopped section '{code}' by user")
    with _running_lock:
        _running_sections.discard(code)
    return JSONResponse({"status": "stopped", "section": code, "stopped_run_ids": stopped_ids})


@app.post("/api/stop-all")
def api_stop_all() -> JSONResponse:
    stopped_ids = job_control.request_stop_all()
    for rid in stopped_ids:
        store.stop_run(rid, reason="Stopped all runs by user")
    with store.db() as conn:
        rows = conn.execute("SELECT id FROM runs WHERE status IN ('running', 'queued')").fetchall()
        for row in rows:
            store.stop_run(int(row["id"]), reason="Stopped all runs by user")
    with _running_lock:
        _running_sections.clear()
    job_control.reset_stop_all()
    return JSONResponse({"status": "stopped_all", "stopped_run_ids": stopped_ids})


@app.post("/api/runs/{run_id}/retry")
async def api_retry_run(
    run_id: int,
    mock: bool = False,
    force_upload: bool = False,
) -> JSONResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("status") in ("running", "queued"):
        raise HTTPException(status_code=409, detail=f"Run {run_id} is already running or queued")

    sec_code = str(run.get("section_code", "")).lower()
    job_control.clear_stop(run_id, sec_code)
    with _running_lock:
        if sec_code in _running_sections:
            raise HTTPException(status_code=409, detail=f"Section '{sec_code}' is already running or queued")
        _running_sections.add(sec_code)

    store.queue_run_for_retry(run_id)

    def _bg_retry() -> None:
        try:
            check_stop(run_id, sec_code)
            with _generate_semaphore:
                check_stop(run_id, sec_code)
                retry_single_short(
                    run_id,
                    mock=mock,
                    skip_upload=not _youtube_enabled(),
                    force_upload=force_upload,
                )
        except JobStoppedError:
            logger.info("Queued retry %s for %s stopped by user", run_id, sec_code)
            store.stop_run(run_id, reason="Stopped by user")
        except Exception as exc:
            logger.exception("Retry failed for run %s", run_id)
            store.finish_run(run_id, "failed", error_message=str(exc))
        finally:
            with _running_lock:
                _running_sections.discard(sec_code)

    threading.Thread(
        target=_bg_retry,
        name=f"retry-{run_id}",
        daemon=True,
    ).start()
    return JSONResponse({"status": "retry_queued", "run_id": run_id, "section": sec_code})


@app.post("/api/retry-failed")
def api_retry_failed(
    mock: bool = False,
) -> JSONResponse:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with store.db() as conn:
        rows = conn.execute(
            "SELECT id, section_code FROM runs WHERE run_date = ? AND status IN ('failed', 'stopped') ORDER BY id ASC",
            (today,),
        ).fetchall()
        runs_to_retry = [(int(r["id"]), str(r["section_code"]).lower()) for r in rows]

    if not runs_to_retry:
        return JSONResponse({"status": "none", "message": "No failed or stopped runs found for today"})

    started_run_ids: list[int] = []
    skipped_run_ids: list[int] = []

    def _worker_retry(rid: int, code: str) -> None:
        try:
            check_stop(rid, code)
            with _generate_semaphore:
                check_stop(rid, code)
                retry_single_short(
                    rid,
                    mock=mock,
                    skip_upload=not _youtube_enabled(),
                )
        except JobStoppedError:
            logger.info("Queued retry %s for %s stopped by user", rid, code)
            store.stop_run(rid, reason="Stopped by user")
        except Exception as exc:
            logger.exception("Batch retry failed for run %s", rid)
            store.finish_run(rid, "failed", error_message=str(exc))
        finally:
            with _running_lock:
                _running_sections.discard(code)

    with _running_lock:
        for rid, code in runs_to_retry:
            job_control.clear_stop(rid, code)
            if code in _running_sections:
                skipped_run_ids.append(rid)
                continue
            _running_sections.add(code)
            store.queue_run_for_retry(rid)
            started_run_ids.append(rid)
            threading.Thread(
                target=_worker_retry,
                args=(rid, code),
                name=f"retry-{rid}",
                daemon=True,
            ).start()

    return JSONResponse({
        "status": "retry_queued",
        "retrying_run_ids": started_run_ids,
        "skipped_run_ids": skipped_run_ids,
    })


def create_app() -> FastAPI:
    store.init_db()
    store.fail_orphaned_runs()
    return app
