"""APScheduler: fire section batches daily at 22:00 local time."""

from __future__ import annotations

import logging
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import load_sections

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler(run_callback: Callable[[str], None]) -> BackgroundScheduler:
    """run_callback(section_code) — daily 10pm in each section timezone."""
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler()
    for section in load_sections():
        job_id = f"daily-{section.code}-2200"
        scheduler.add_job(
            run_callback,
            trigger=CronTrigger(hour=22, minute=0, timezone=section.timezone),
            args=[section.code],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "Scheduled 10pm job for %s (%s) at 22:00 %s",
            section.code,
            section.name,
            section.timezone,
        )

    scheduler.start()
    _scheduler = scheduler
    return scheduler


def get_next_run_times() -> list[dict[str, str]]:
    if not _scheduler:
        return []
    result = []
    for job in _scheduler.get_jobs():
        next_run = job.next_run_time
        # job_id: daily-tech-2200
        parts = job.id.split("-")
        section_code = parts[1] if len(parts) >= 2 else job.id
        result.append(
            {
                "job_id": job.id,
                "section_code": section_code,
                "period": "10:00 PM",
                "next_run": next_run.isoformat() if next_run else "",
            }
        )
    result.sort(key=lambda item: (item.get("next_run") or "", item.get("job_id") or ""))
    return result


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
