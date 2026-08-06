"""FastAPI local Shorts library dashboard."""

from __future__ import annotations

import json
import logging
import shutil
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import (
    OUTPUT_DIR,
    get_section,
    load_pipeline_config,
    load_sections,
    local_run_date,
)
from src.db import store
from src.naming import title_from_video_path
from src.pipeline import run_section_batch
from src.scheduler import get_next_run_times

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(title="News Shorts Library")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

_running_lock = threading.Lock()
_running_sections: set[str] = set()
_upload_lock = threading.Lock()
_uploading_runs: set[int] = set()


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
    run["can_upload"] = bool(run["has_video"] and run.get("status") != "running")
    run["upload_label"] = (
        "Re-upload" if upload_status in ("uploaded", "failed") else "Upload"
    )

    if upload_status == "uploading":
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
    with _running_lock:
        if code in _running_sections:
            logger.warning(
                "Skipping scheduled run for %s — already running",
                code,
            )
            return
        _running_sections.add(code)
    try:
        run_section_batch(
            code,
            skip_upload=not _youtube_enabled(),
        )
    except Exception:
        logger.exception("Scheduled run failed for %s", code)
    finally:
        with _running_lock:
            _running_sections.discard(code)


def _retry_failed_uploads() -> None:
    """Hourly: re-attempt YouTube uploads that previously failed."""
    if not _youtube_enabled():
        return

    failed = store.list_failed_uploads()
    if not failed:
        return

    logger.info("Retrying %s failed YouTube upload(s)", len(failed))
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
        store.append_step_log(run_id, "upload", "Hourly retry of failed upload")
        try:
            _upload_run_video(run_id)
        except Exception:
            logger.exception("Hourly upload retry failed for run %s", run_id)
            current = store.get_run(run_id)
            if current and (current.get("upload_status") or "") == "uploading":
                store.set_upload_status(
                    run_id,
                    "failed",
                    upload_error="Hourly retry crashed unexpectedly",
                )
        finally:
            with _upload_lock:
                _uploading_runs.discard(run_id)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    section: str | None = None,
    date: str | None = None,
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
    has_running = any(r["status"] == "running" for r in runs) or stats.get("running", 0) > 0
    has_uploading = (
        any(r.get("upload_status") == "uploading" for r in runs)
        or stats.get("uploading", 0) > 0
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "groups": groups,
            "stats": stats,
            "sections": sections,
            "available_dates": available_dates,
            "next_runs": next_runs,
            "filter_section": (section or "").lower(),
            "filter_date": date or "",
            "has_running": has_running,
            "has_uploading": has_uploading,
            "youtube_enabled": _youtube_enabled(),
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
    if run.get("status") == "running":
        return {"run_id": run_id, "ok": False, "reason": "running"}
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
    """Delete many local runs. scope: all | uploaded (local files + DB rows only)."""
    scope_key = (scope or "all").strip().lower()
    if scope_key not in ("all", "uploaded"):
        raise HTTPException(
            status_code=400, detail="scope must be 'all' or 'uploaded'"
        )

    runs = store.list_runs(limit=5000)
    if scope_key == "uploaded":
        runs = [r for r in runs if _run_is_uploaded(r)]

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
        }
        for s in load_sections()
    ]
    return JSONResponse({"sections": sections, "next_runs": get_next_run_times()})


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
    background_tasks: BackgroundTasks,
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
            raise HTTPException(status_code=409, detail=f"{code} is already running")

    if async_run:

        def _bg() -> None:
            with _running_lock:
                _running_sections.add(code)
            try:
                run_section_batch(
                    code,
                    news_provider="mock" if mock else "google_news_rss",
                    skip_upload=not _youtube_enabled(),
                )
            except Exception:
                logger.exception("Background batch failed for %s", code)
            finally:
                with _running_lock:
                    _running_sections.discard(code)

        background_tasks.add_task(_bg)
        return JSONResponse(
            {
                "status": "started",
                "section": code,
                "news_count": section.news_count,
            }
        )

    with _running_lock:
        _running_sections.add(code)
    try:
        run_ids = run_section_batch(
            code,
            news_provider="mock" if mock else "google_news_rss",
            skip_upload=not _youtube_enabled(),
        )
    finally:
        with _running_lock:
            _running_sections.discard(code)
    return JSONResponse(
        {"run_ids": run_ids, "status": "completed", "section": code}
    )


@app.post("/api/trigger-all")
async def api_trigger_all(
    background_tasks: BackgroundTasks,
    mock: bool = False,
) -> JSONResponse:
    sections = load_sections()

    def _bg() -> None:
        for section in sections:
            code = section.code
            with _running_lock:
                if code in _running_sections:
                    logger.warning("Skip %s — already running", code)
                    continue
                _running_sections.add(code)
            try:
                run_section_batch(
                    code,
                    news_provider="mock" if mock else "google_news_rss",
                    skip_upload=not _youtube_enabled(),
                )
            except Exception:
                logger.exception("Background batch failed for %s", code)
            finally:
                with _running_lock:
                    _running_sections.discard(code)

    background_tasks.add_task(_bg)
    return JSONResponse(
        {
            "status": "started",
            "sections": [s.code for s in sections],
        }
    )


def create_app() -> FastAPI:
    store.init_db()
    store.fail_orphaned_runs()
    return app
