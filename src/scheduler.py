"""APScheduler: section batches (IST) + conditional failed-upload retries."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import load_sections

logger = logging.getLogger(__name__)

# All sections share India Standard Time schedule (evening only).
SCHEDULE_TIMEZONE = "Asia/Kolkata"
SCHEDULE_HOURS = (22,)  # 10:00 PM IST
UPLOAD_RETRY_JOB_ID = "retry-failed-uploads"
UPLOAD_RETRY_INTERVAL_HOURS = 1

_HOUR_LABELS = {22: "10:00 PM"}

_scheduler: BackgroundScheduler | None = None
_retry_uploads_callback: Callable[[], None] | None = None


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


def start_scheduler(
    run_callback: Callable[[str], None],
    *,
    retry_uploads_callback: Callable[[], None] | None = None,
) -> BackgroundScheduler:
    """Section jobs at 10:00 PM IST; upload retries only while failures exist."""
    global _scheduler, _retry_uploads_callback
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler()
    for section in load_sections():
        for hour in SCHEDULE_HOURS:
            period = f"{hour:02d}00"
            job_id = f"daily-{section.code}-{period}"
            label = _HOUR_LABELS.get(hour, f"{hour:02d}:00")
            scheduler.add_job(
                run_callback,
                trigger=CronTrigger(
                    hour=hour, minute=0, timezone=SCHEDULE_TIMEZONE
                ),
                args=[section.code],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=3600,
            )
            logger.info(
                "Scheduled %s IST job for %s (%s)",
                label,
                section.code,
                section.name,
            )

    _retry_uploads_callback = retry_uploads_callback
    scheduler.start()
    _scheduler = scheduler

    # Only arm retry if there are already failed uploads (e.g. after restart).
    if retry_uploads_callback is not None:
        sync_failed_upload_retry_job()

    return scheduler


def get_next_run_times() -> list[dict[str, str]]:
    """One row per section: daily local clocks + next run in system time."""
    if not _scheduler:
        return []

    section_names = {s.code: s.name for s in load_sections()}
    by_section: dict[str, datetime | None] = {}

    for job in _scheduler.get_jobs():
        if not str(job.id).startswith("daily-"):
            continue
        parts = job.id.split("-")
        section_code = parts[1] if len(parts) >= 2 else job.id
        next_run = job.next_run_time
        if next_run is None:
            continue
        current = by_section.get(section_code)
        if current is None or next_run < current:
            by_section[section_code] = next_run

    clocks = " & ".join(_format_local_clock(h) for h in SCHEDULE_HOURS)
    schedule_label = f"daily {clocks} (system)"

    result: list[dict[str, str]] = []
    for section_code, next_run in by_section.items():
        result.append(
            {
                "section_code": section_code,
                "section_name": section_names.get(section_code, section_code),
                "schedule": schedule_label,
                "next_run": _format_local(next_run) if next_run else "",
                "next_run_iso": next_run.isoformat() if next_run else "",
            }
        )

    result.sort(
        key=lambda item: (
            item.get("next_run_iso") or "",
            item.get("section_code") or "",
        )
    )
    return result


def shutdown_scheduler() -> None:
    global _scheduler, _retry_uploads_callback
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _retry_uploads_callback = None
