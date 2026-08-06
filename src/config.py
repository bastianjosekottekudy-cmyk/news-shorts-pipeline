"""Project paths and configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"
SECRETS_DIR = PROJECT_ROOT / "secrets"

# override=True so .env wins over stale shell vars (e.g. SKIP_YOUTUBE_UPLOAD)
load_dotenv(PROJECT_ROOT / ".env", override=True)


@dataclass
class Section:
    code: str
    name: str
    timezone: str
    language: str = "en"
    region: str = "US"
    news_count: int = 5
    google_topic: str = ""
    search_query: str = ""
    rss_url: str = ""
    youtube_tags: list[str] = field(default_factory=list)

    @property
    def shorts_count(self) -> int:
        """Always one Short per section (roundup of news_count headlines)."""
        return 1

    @property
    def count(self) -> int:
        return self.news_count


def load_sections() -> list[Section]:
    path = CONFIG_DIR / "sections.yaml"
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sections: list[Section] = []
    for entry in data.get("sections", []):
        raw = dict(entry)
        raw["code"] = str(raw.get("code", "")).lower()

        # Map legacy count / shorts_count → news_count only
        if "news_count" not in raw and "count" in raw:
            raw["news_count"] = raw["count"]
        raw.pop("count", None)
        raw.pop("shorts_count", None)

        raw.setdefault("news_count", 5)
        raw["news_count"] = max(1, int(raw["news_count"]))

        raw.setdefault("language", "en")
        raw["language"] = "en"
        raw.setdefault("region", "US")
        raw.setdefault("timezone", "Asia/Kolkata")
        raw.setdefault("google_topic", "")
        raw.setdefault("search_query", "")
        raw.setdefault("rss_url", "")
        raw.setdefault("youtube_tags", [])
        sections.append(Section(**raw))
    return sections


def get_section(code: str) -> Section:
    needle = code.lower()
    for section in load_sections():
        if section.code == needle:
            return section
    raise ValueError(f"Unknown section code: {code}")


def local_run_date(section: Section) -> str:
    """Calendar date in the section's timezone (YYYY-MM-DD)."""
    return datetime.now(ZoneInfo(section.timezone)).strftime("%Y-%m-%d")


def local_time_label(section: Section) -> str:
    """Local clock for manual generates, e.g. '9:47 PM'."""
    now = datetime.now(ZoneInfo(section.timezone))
    return now.strftime("%I:%M %p").lstrip("0")


def load_pipeline_config() -> dict[str, Any]:
    path = CONFIG_DIR / "pipeline.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def section_output_dir(
    section_code: str,
    run_date: str,
    run_id: int | None = None,
) -> Path:
    """
    Per-short output folder.
    layout: output/{date}/{section}/run_{id}/
    """
    path = OUTPUT_DIR / run_date / section_code.lower()
    if run_id is not None:
        path = path / f"run_{run_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path
