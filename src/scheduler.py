"""APScheduler: section batches (IST) + hourly failed-upload retries."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import load_sections

logger = logging.getLogger(__name__)

# All sections share India Standard Time schedule (morning + evening).
SCHEDULE_TIMEZONE = "Asia/Kolkata"
SCHEDULE_HOURS = (10, 22)  # 10:00 AM and 10:00 PM IST
UPLOAD_RETRY_JOB_ID = "retry-failed-uploads"

_scheduler: BackgroundScheduler | None = None


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


def start_scheduler(
    run_callback: Callable[[str], None],
    *,
    retry_uploads_callback: Callable[[], None] | None = None,
) -> BackgroundScheduler:
    """Section jobs at 10 AM/PM IST; optional hourly failed-upload retries."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler()
    for section in load_sections():
        for hour in SCHEDULE_HOURS:
            period = "1000" if hour == 10 else "2200"
            job_id = f"daily-{section.code}-{period}"
            label = "10:00 AM" if hour == 10 else "10:00 PM"
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

    if retry_uploads_callback is not None:
        scheduler.add_job(
            retry_uploads_callback,
            trigger=IntervalTrigger(hours=1),
            id=UPLOAD_RETRY_JOB_ID,
            replace_existing=True,
            misfire_grace_time=1800,
            # First retry soon after start so overnight failures aren't waited out.
            next_run_time=datetime.now().astimezone(),
        )
        logger.info("Scheduled hourly retry for failed YouTube uploads")

    scheduler.start()
    _scheduler = scheduler
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

    morning_local = _format_local_clock(10)
    evening_local = _format_local_clock(22)
    schedule_label = f"daily {morning_local} & {evening_local} (system)"

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
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
