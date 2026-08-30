"""Fetch top headlines for a news section (Google News RSS)."""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

import feedparser

from src.config import Section

logger = logging.getLogger(__name__)

# Prefer Latin-script English headlines (skip mostly non-English scripts).
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_NON_LATIN_LETTER_RE = re.compile(
    r"[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u0B80-\u0BFF"
    r"\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F\u0E00-\u0E7F"
    r"\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]"
)


def _looks_english(text: str) -> bool:
    """Keep titles that are primarily Latin/English letters."""
    if not text or not text.strip():
        return False
    latin = len(_LATIN_LETTER_RE.findall(text))
    non_latin = len(_NON_LATIN_LETTER_RE.findall(text))
    if latin < 8:
        return False
    if non_latin and non_latin >= max(3, latin // 3):
        return False
    return True


@dataclass
class NewsItem:
    title: str
    link: str
    summary: str
    source: str = "google_news_rss"


class NewsProvider:
    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        raise NotImplementedError


class GoogleNewsRssProvider(NewsProvider):
    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        url = self._build_url(section)
        feed = feedparser.parse(url)
        items: list[NewsItem] = []
        seen: set[str] = set()
        for entry in feed.entries:
            title = (getattr(entry, "title", "") or "").strip()
            if not title or not _looks_english(title):
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            summary = (getattr(entry, "summary", "") or "").strip()
            if summary and not _looks_english(summary):
                summary = ""
            items.append(
                NewsItem(
                    title=title,
                    link=getattr(entry, "link", "") or "",
                    summary=summary[:800],
                    source="google_news_rss",
                )
            )
            if len(items) >= max_items:
                break
        logger.info(
            "Fetched %s English headlines for section %s from %s",
            len(items),
            section.code,
            url,
        )
        return items

    def _build_url(self, section: Section) -> str:
        if section.rss_url:
            return section.rss_url
        # Always English Google News edition for narration + TTS.
        lang = "en"
        region = (section.region or "US").upper()
        if section.search_query:
            query = urllib.parse.quote(section.search_query)
            return (
                f"https://news.google.com/rss/search?q={query}"
                f"&hl={lang}&gl={region}&ceid={region}:{lang}"
            )
        topic = (section.google_topic or "WORLD").upper()
        return (
            f"https://news.google.com/rss/headlines/section/topic/{topic}"
            f"?hl={lang}&gl={region}&ceid={region}:{lang}"
        )


class MockNewsProvider(NewsProvider):
    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        items = [
            NewsItem(
                title=f"Mock {section.name} headline {i + 1}: major development today",
                link=f"https://example.com/{section.code}/story-{i + 1}",
                summary=f"Simulated summary for {section.name} story {i + 1}.",
                source="mock",
            )
            for i in range(max_items)
        ]
        return items


def get_news_provider(name: str = "google_news_rss") -> NewsProvider:
    providers: dict[str, NewsProvider] = {
        "google_news_rss": GoogleNewsRssProvider(),
        "mock": MockNewsProvider(),
    }
    if name not in providers:
        raise ValueError(f"Unknown news provider: {name}")
    return providers[name]


def fetch_section_news(
    section: Section,
    output_dir: Path | None = None,
    *,
    provider_name: str = "google_news_rss",
    max_items: int | None = None,
) -> list[dict]:
    """Fetch top N news items for a section. Optionally write news.json."""
    count = (
        max_items
        if max_items is not None
        else max(1, int(getattr(section, "news_count", None) or section.count))
    )
    provider = get_news_provider(provider_name)
    items = provider.fetch_for_section(section, count)
    payload = [asdict(item) for item in items]
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "news.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    return payload
