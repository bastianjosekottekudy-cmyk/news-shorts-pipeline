"""On-screen title/subtitle for a news Short roundup."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.naming import build_video_title, sanitize_news_title

logger = logging.getLogger(__name__)


def generate_display_title(
    section_name: str,
    run_date: str,
    output_dir: Path,
    *,
    story_count: int = 1,
) -> dict[str, str]:
    """Return {title, subtitle} for overlays; write display_titles.json."""
    title = build_video_title(section_name, run_date)
    if story_count > 1:
        subtitle = f"{story_count} top stories"
    else:
        subtitle = f"{section_name} · News Short"
    card = {"title": title, "subtitle": subtitle}
    (output_dir / "display_titles.json").write_text(
        json.dumps(card, indent=2), encoding="utf-8"
    )
    return card


def story_card(news_title: str, section_name: str, rank: int) -> dict[str, str]:
    from src.script.generator import _clean_for_speech

    cleaned = sanitize_news_title(_clean_for_speech(news_title), max_len=90)
    return {
        "title": cleaned,
        "subtitle": f"{section_name} · #{rank}",
    }
