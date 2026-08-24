"""Project paths and configuration loading."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field, fields
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
SECTIONS_PATH = CONFIG_DIR / "sections.yaml"

# override=True so .env wins over stale shell vars (e.g. SKIP_YOUTUBE_UPLOAD)
load_dotenv(PROJECT_ROOT / ".env", override=True)

SCHEDULE_TIMEZONE = "Asia/Kolkata"
DEFAULT_SCHEDULE_HOUR = 22
DEFAULT_SCHEDULE_MINUTE = 0
GOOGLE_NEWS_TOPICS = (
    "TECHNOLOGY",
    "ENTERTAINMENT",
    "WORLD",
    "BUSINESS",
    "NATION",
    "SCIENCE",
    "HEALTH",
    "SPORTS",
)

_SECTIONS_LOCK = threading.RLock()
_CODE_RE = re.compile(r"[^a-z0-9]+")
_SECTION_FIELD_NAMES = None

_SECTIONS_HEADER = """# Add or remove sections here, or use the dashboard Schedule panel.
#
# Fields:
#   code              - short id used in output paths and API (e.g. tech)
#   name              - Display name
#   google_topic      - Google News topic (TECHNOLOGY, ENTERTAINMENT, WORLD, BUSINESS, NATION, …)
#   search_query      - optional; if set, uses RSS search instead of topic
#   rss_url           - optional; full custom RSS URL overrides topic/search
#   news_count        - how many headlines to fetch into the section Short (default 5)
#   language          - always English (en) for narration + Google News hl
#   region            - Google News gl / ceid region (e.g. US, GB, IN, CY)
#   timezone          - IANA timezone for run dates
#   youtube_tags      - tags appended on upload
#   schedule_enabled  - if false, skip the daily APScheduler job (manual Generate still works)
#   schedule_hour     - daily hour in Asia/Kolkata (0–23, default 22 = 10:00 PM IST)
#   schedule_minute   - daily minute (0–59, default 0)
#
# Always produces exactly 1 Short per section covering all fetched headlines.
"""


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
    schedule_enabled: bool = True
    schedule_hour: int = DEFAULT_SCHEDULE_HOUR
    schedule_minute: int = DEFAULT_SCHEDULE_MINUTE

    @property
    def shorts_count(self) -> int:
        """Always one Short per section (roundup of news_count headlines)."""
        return 1

    @property
    def count(self) -> int:
        return self.news_count

    @property
    def schedule_time_ist(self) -> str:
        return f"{self.schedule_hour:02d}:{self.schedule_minute:02d}"


def _section_field_names() -> set[str]:
    global _SECTION_FIELD_NAMES
    if _SECTION_FIELD_NAMES is None:
        _SECTION_FIELD_NAMES = {f.name for f in fields(Section)}
    return _SECTION_FIELD_NAMES


def slugify_section_code(raw: str) -> str:
    text = _CODE_RE.sub("-", (raw or "").strip().lower()).strip("-")
    return text[:32]


def _clamp_hour(value: Any) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULE_HOUR
    return max(0, min(23, hour))


def _clamp_minute(value: Any) -> int:
    try:
        minute = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULE_MINUTE
    return max(0, min(59, minute))


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    raw = dict(entry)
    raw["code"] = slugify_section_code(str(raw.get("code") or raw.get("name") or ""))
    if "news_count" not in raw and "count" in raw:
        raw["news_count"] = raw["count"]
    raw.pop("count", None)
    raw.pop("shorts_count", None)
    raw.setdefault("news_count", 5)
    try:
        raw["news_count"] = max(1, min(15, int(raw["news_count"])))
    except (TypeError, ValueError):
        raw["news_count"] = 5
    raw.setdefault("language", "en")
    raw["language"] = "en"
    raw.setdefault("region", "US")
    raw["region"] = str(raw.get("region") or "US").upper()[:2]
    raw.setdefault("timezone", SCHEDULE_TIMEZONE)
    raw.setdefault("google_topic", "")
    raw.setdefault("search_query", "")
    raw.setdefault("rss_url", "")
    tags = raw.get("youtube_tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    raw["youtube_tags"] = [str(t).strip() for t in tags if str(t).strip()]
    raw["schedule_enabled"] = bool(raw.get("schedule_enabled", True))
    raw["schedule_hour"] = _clamp_hour(raw.get("schedule_hour", DEFAULT_SCHEDULE_HOUR))
    raw["schedule_minute"] = _clamp_minute(
        raw.get("schedule_minute", DEFAULT_SCHEDULE_MINUTE)
    )
    topic = str(raw.get("google_topic") or "").strip().upper()
    raw["google_topic"] = topic if topic in GOOGLE_NEWS_TOPICS else (topic or "")
    return raw


def _entry_to_section(entry: dict[str, Any]) -> Section:
    raw = _normalize_entry(entry)
    known = _section_field_names()
    return Section(**{k: raw[k] for k in known if k in raw})


def _dump_entry(entry: dict[str, Any]) -> dict[str, Any]:
    raw = _normalize_entry(entry)
    out: dict[str, Any] = {
        "code": raw["code"],
        "name": str(raw.get("name") or raw["code"]).strip(),
        "news_count": raw["news_count"],
        "language": "en",
        "region": raw["region"],
        "timezone": raw.get("timezone") or SCHEDULE_TIMEZONE,
        "youtube_tags": raw["youtube_tags"],
        "schedule_enabled": raw["schedule_enabled"],
        "schedule_hour": raw["schedule_hour"],
        "schedule_minute": raw["schedule_minute"],
    }
    rss = str(raw.get("rss_url") or "").strip()
    query = str(raw.get("search_query") or "").strip()
    topic = str(raw.get("google_topic") or "").strip().upper()
    if rss:
        out["rss_url"] = rss
    elif query:
        out["search_query"] = query
    elif topic:
        out["google_topic"] = topic
    else:
        out["google_topic"] = "WORLD"
    return out


def load_section_entries() -> list[dict[str, Any]]:
    with _SECTIONS_LOCK:
        with SECTIONS_PATH.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return [dict(e) for e in (data.get("sections") or []) if isinstance(e, dict)]


def save_section_entries(entries: list[dict[str, Any]]) -> list[Section]:
    dumped = [_dump_entry(e) for e in entries]
    codes = [e["code"] for e in dumped]
    if any(not c for c in codes):
        raise ValueError("Every section needs a code")
    if len(codes) != len(set(codes)):
        raise ValueError("Duplicate section codes")
    body = yaml.safe_dump(
        {"sections": dumped},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    with _SECTIONS_LOCK:
        SECTIONS_PATH.write_text(_SECTIONS_HEADER + body, encoding="utf-8")
    return [_entry_to_section(e) for e in dumped]


def load_sections() -> list[Section]:
    return [_entry_to_section(e) for e in load_section_entries()]


def get_section(code: str) -> Section:
    needle = slugify_section_code(code)
    for section in load_sections():
        if section.code == needle:
            return section
    raise ValueError(f"Unknown section code: {code}")


def update_section_schedule(
    code: str,
    *,
    enabled: bool | None = None,
    hour: int | None = None,
    minute: int | None = None,
) -> Section:
    needle = slugify_section_code(code)
    entries = load_section_entries()
    found = False
    for entry in entries:
        if slugify_section_code(str(entry.get("code") or "")) == needle:
            if enabled is not None:
                entry["schedule_enabled"] = bool(enabled)
            if hour is not None:
                entry["schedule_hour"] = _clamp_hour(hour)
            if minute is not None:
                entry["schedule_minute"] = _clamp_minute(minute)
            found = True
            break
    if not found:
        raise ValueError(f"Unknown section code: {code}")
    sections = save_section_entries(entries)
    return next(s for s in sections if s.code == needle)


def add_section(
    *,
    name: str,
    code: str = "",
    google_topic: str = "",
    search_query: str = "",
    rss_url: str = "",
    region: str = "US",
    news_count: int = 5,
    schedule_enabled: bool = True,
    schedule_hour: int = DEFAULT_SCHEDULE_HOUR,
    schedule_minute: int = DEFAULT_SCHEDULE_MINUTE,
) -> Section:
    display = (name or "").strip()
    if not display:
        raise ValueError("Section name is required")
    slug = slugify_section_code(code or display)
    if not slug:
        raise ValueError("Section code is required")
    existing = {s.code for s in load_sections()}
    if slug in existing:
        raise ValueError(f"Section {slug!r} already exists")
    topic = (google_topic or "").strip().upper()
    query = (search_query or "").strip()
    rss = (rss_url or "").strip()
    if not (topic or query or rss):
        raise ValueError("Provide a Google News topic, search query, or RSS URL")
    if topic and topic not in GOOGLE_NEWS_TOPICS:
        raise ValueError(f"Unknown Google News topic: {topic}")
    tags = ["shorts", slug, "news"]
    entries = load_section_entries()
    entries.append(
        {
            "code": slug,
            "name": display,
            "google_topic": topic if not query and not rss else "",
            "search_query": query,
            "rss_url": rss,
            "news_count": news_count,
            "region": region,
            "youtube_tags": tags,
            "schedule_enabled": schedule_enabled,
            "schedule_hour": schedule_hour,
            "schedule_minute": schedule_minute,
        }
    )
    sections = save_section_entries(entries)
    return next(s for s in sections if s.code == slug)


def remove_section(code: str) -> str:
    needle = slugify_section_code(code)
    entries = load_section_entries()
    kept: list[dict[str, Any]] = []
    removed = ""
    for entry in entries:
        slug = slugify_section_code(str(entry.get("code") or ""))
        if slug == needle:
            removed = slug
            continue
        kept.append(entry)
    if not removed:
        raise ValueError(f"Unknown section code: {code}")
    if not kept:
        raise ValueError("Cannot remove the last remaining section")
    save_section_entries(kept)
    return removed


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
