"""APScheduler: per-section IST jobs + conditional failed-upload retries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import (
    DEFAULT_SCHEDULE_HOUR,
    DEFAULT_SCHEDULE_MINUTE,
    SCHEDULE_TIMEZONE,
    Section,
    load_sections,
)

logger = logging.getLogger(__name__)

UPLOAD_RETRY_JOB_ID = "retry-failed-uploads"
UPLOAD_RETRY_INTERVAL_HOURS = 1

_scheduler: BackgroundScheduler | None = None
_retry_uploads_callback: Callable[[], None] | None = None
_run_callback: Callable[[str], None] | None = None


def _format_local(dt: datetime) -> str:
    """Format datetime in the machine's local timezone."""
    local = dt.astimezone()
    return local.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def _format_local_clock(hour: int, minute: int = 0) -> str:
    """Convert a fixed IST clock time to today's equivalent local clock label."""
    ist = ZoneInfo(SCHEDULE_TIMEZONE)
    now_ist = datetime.now(ist)
    ist_dt = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    local = ist_dt.astimezone()
    return local.strftime("%I:%M %p").lstrip("0")


def _job_id(section_code: str) -> str:
    return f"daily-{section_code}"


def _ist_label(hour: int, minute: int = 0) -> str:
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:{minute:02d} {suffix} IST"


def sync_failed_upload_retry_job(*, run_in_hours: float | None = None) -> None:
    """
    Keep the hourly retry job only while failed uploads exist.
    Does nothing if the scheduler or retry callback is not ready.
    """
    if not _scheduler or not _scheduler.running or _retry_uploads_callback is None:
        return

    from src.db import store

    has_failed = store.count_failed_uploads() > 0
    existing = _scheduler.get_job(UPLOAD_RETRY_JOB_ID)

    if not has_failed:
        if existing is not None:
            _scheduler.remove_job(UPLOAD_RETRY_JOB_ID)
            logger.info("Cleared upload-retry job — no failed uploads")
        return

    delay_h = (
        UPLOAD_RETRY_INTERVAL_HOURS if run_in_hours is None else max(0.0, float(run_in_hours))
    )
    next_run = datetime.now().astimezone() + timedelta(hours=delay_h)

    if existing is None:
        _scheduler.add_job(
            _retry_uploads_callback,
            trigger=IntervalTrigger(hours=UPLOAD_RETRY_INTERVAL_HOURS),
            id=UPLOAD_RETRY_JOB_ID,
            replace_existing=True,
            misfire_grace_time=1800,
            next_run_time=next_run,
        )
        logger.info(
            "Scheduled upload retry every %sh (next %s) — failed uploads pending",
            UPLOAD_RETRY_INTERVAL_HOURS,
            _format_local(next_run),
        )


def _clear_section_jobs(scheduler: BackgroundScheduler) -> None:
    for job in list(scheduler.get_jobs()):
        if str(job.id).startswith("daily-"):
            scheduler.remove_job(job.id)


def _schedule_section(scheduler: BackgroundScheduler, section: Section, run_callback: Callable[[str], None]) -> None:
    hour = section.schedule_hour if section.schedule_hour is not None else DEFAULT_SCHEDULE_HOUR
    minute = (
        section.schedule_minute
        if section.schedule_minute is not None
        else DEFAULT_SCHEDULE_MINUTE
    )
    scheduler.add_job(
        run_callback,
        trigger=CronTrigger(
            hour=hour,
            minute=minute,
            timezone=SCHEDULE_TIMEZONE,
        ),
        args=[section.code],
        id=_job_id(section.code),
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info(
        "Scheduled %s job for %s (%s)",
        _ist_label(hour, minute),
        section.code,
        section.name,
    )


def reload_section_jobs() -> None:
    """Rebuild daily section jobs from current sections.yaml."""
    if not _scheduler or not _scheduler.running or _run_callback is None:
        return
    _clear_section_jobs(_scheduler)
    for section in load_sections():
        if not section.schedule_enabled:
            logger.info("Schedule off for %s (%s)", section.code, section.name)
            continue
        _schedule_section(_scheduler, section, _run_callback)


def start_scheduler(
    run_callback: Callable[[str], None],
    *,
    retry_uploads_callback: Callable[[], None] | None = None,
) -> BackgroundScheduler:
    """Per-section IST jobs; upload retries only while failures exist."""
    global _scheduler, _retry_uploads_callback, _run_callback
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler()
    _run_callback = run_callback
    for section in load_sections():
        if not section.schedule_enabled:
            logger.info("Schedule off for %s (%s)", section.code, section.name)
            continue
        _schedule_section(scheduler, section, run_callback)

    _retry_uploads_callback = retry_uploads_callback
    scheduler.start()
    _scheduler = scheduler

    if retry_uploads_callback is not None:
        sync_failed_upload_retry_job()

    return scheduler


def get_next_run_times() -> list[dict[str, str | bool]]:
    """One row per section, including disabled schedules."""
    sections = load_sections()
    next_by_code: dict[str, datetime | None] = {}
    if _scheduler:
        for job in _scheduler.get_jobs():
            if not str(job.id).startswith("daily-"):
                continue
            code = str(job.id)[len("daily-") :]
            next_by_code[code] = job.next_run_time

    result: list[dict[str, str | bool]] = []
    for section in sections:
        hour = section.schedule_hour
        minute = section.schedule_minute
        local_clock = _format_local_clock(hour, minute)
        next_run = next_by_code.get(section.code)
        if section.schedule_enabled:
            schedule = f"daily {_ist_label(hour, minute)} · {local_clock} (system)"
        else:
            schedule = f"off · {_ist_label(hour, minute)}"
        result.append(
            {
                "section_code": section.code,
                "section_name": section.name,
                "schedule": schedule,
                "enabled": section.schedule_enabled,
                "hour": hour,
                "minute": minute,
                "time_ist": f"{hour:02d}:{minute:02d}",
                "next_run": _format_local(next_run) if next_run else "",
                "next_run_iso": next_run.isoformat() if next_run else "",
            }
        )

    result.sort(
        key=lambda item: (
            not bool(item.get("enabled")),
            item.get("next_run_iso") or "z",
            item.get("section_code") or "",
        )
    )
    return result


def shutdown_scheduler() -> None:
    global _scheduler, _retry_uploads_callback, _run_callback
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _retry_uploads_callback = None
    _run_callback = None
