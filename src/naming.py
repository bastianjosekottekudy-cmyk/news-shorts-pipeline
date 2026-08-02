"""YouTube Short title and filename helpers."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def format_display_date(run_date: str) -> str:
    """Convert YYYY-MM-DD to 'August 2, 2026'."""
    try:
        dt = datetime.strptime(run_date, "%Y-%m-%d")
        return f"{dt.strftime('%B')} {dt.day}, {dt.year}"
    except ValueError:
        return run_date


def sanitize_news_title(title: str, max_len: int = 100) -> str:
    """Clean a headline (for overlays / descriptions)."""
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    cleaned = re.sub(
        r"\s*[\|\-–—]\s*[A-Za-z0-9][A-Za-z0-9 .,&/'!]{0,40}$",
        "",
        cleaned,
    ).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned or "News"


def build_video_title(
    section_name: str,
    run_date: str,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str:
    """
    YouTube / filename title.
    e.g. 'Top Entertainment August 2, 2026'
    or   'Top Entertainment #2 August 2, 2026' when multiple shorts in a run.
    """
    date_part = format_display_date(run_date)
    name = (section_name or "News").strip()
    if index is not None and total is not None and total > 1:
        return f"Top {name} #{index} {date_part}"
    return f"Top {name} {date_part}"


def safe_filename(title: str) -> str:
    """Strip Windows-invalid filename characters; keep spaces and commas."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    return cleaned or "short"


def video_filename(
    section_name: str,
    run_date: str,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str:
    return f"{safe_filename(build_video_title(section_name, run_date, index=index, total=total))}.mp4"


def title_from_video_path(
    video_path: str | None,
    section_name: str = "",
    run_date: str = "",
) -> str:
    """Derive display title from stored path, or rebuild."""
    if video_path:
        stem = Path(video_path).stem
        if stem and stem != "final":
            return stem
    if section_name and run_date:
        return build_video_title(section_name, run_date)
    return section_name or "Short"
