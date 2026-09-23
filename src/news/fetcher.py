"""Fetch top headlines for a news section with multi-source ingestion & factual extraction."""

from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import re
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import feedparser
import httpx

from src.config import Section

logger = logging.getLogger(__name__)

# Prefer Latin-script English headlines (skip mostly non-English scripts).
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_NON_LATIN_LETTER_RE = re.compile(
    r"[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u0B80-\u0BFF"
    r"\u0C00-\u0C7F\u0C80-\u0CFF\u0D00-\u0D7F\u0E00-\u0E7F"
    r"\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]"
)

# Affiliate, deal, shopping, and guide noise filters
_DEAL_NOISE_RE = re.compile(
    r"\b(?:deals?|deal of the day|save \$|\b\d+%\s+off\b|coupon|promo code|"
    r"best prices?|lowest price|price drop|buying guide|gift guide|where to buy|"
    r"on sale for|cheap|discounted|review:?|hands-on review|unboxing|"
    r"how to (?:start|beat|get|unlock|play|fix|solve|catch)|walkthrough|"
    r"patch notes|quest guide)\b",
    re.IGNORECASE,
)

_HTML_TAGS_RE = re.compile(r"<[^>]+>")


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


def _clean_summary_text(raw_html: str) -> str:
    """Strip HTML tags and unescape text from RSS summaries."""
    if not raw_html:
        return ""
    text = _HTML_TAGS_RE.sub(" ", raw_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # If the summary is just a list of other publications (Google News RSS artifact), ignore it
    if text.startswith("1.") or "View Full Coverage on Google News" in text:
        return ""
    return text


def _is_low_quality_or_deal(title: str, summary: str = "") -> bool:
    """Reject deals, buying guides, walkthroughs, and spam."""
    combined = f"{title} {summary}"
    if _DEAL_NOISE_RE.search(combined):
        return True
    return False


def _normalize_title_key(title: str) -> str:
    """Normalize headline for deduplication."""
    clean = re.sub(r"[^a-zA-Z0-9\s]", "", title.lower())
    words = clean.split()
    return " ".join(words[:8])


def extract_lead_from_url(url: str, timeout: float = 3.0) -> tuple[str, str]:
    """
    Attempt to decode Google News URL and extract og:description or meta description.
    Returns (resolved_url, extracted_summary).
    """
    resolved_url = url
    if "news.google.com" in url:
        try:
            from googlenewsdecoder import gnewsdecoder

            decoded = gnewsdecoder(url)
            if isinstance(decoded, dict):
                resolved_url = decoded.get("decoded_url") or url
            elif isinstance(decoded, str):
                resolved_url = decoded
        except Exception:
            resolved_url = url

    if not resolved_url or "news.google.com" in resolved_url:
        return resolved_url, ""

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        }
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = client.get(resolved_url)
            if resp.status_code == 200:
                text = resp.text
                match = re.search(
                    r'<meta\s+(?:property|name)=[\'"](?:og:description|description)[\'"]\s+content=[\'"]([^\'"]+)[\'"]',
                    text,
                    re.IGNORECASE,
                )
                if not match:
                    match = re.search(
                        r'<meta\s+content=[\'"]([^\'"]+)[\'"]\s+(?:property|name)=[\'"](?:og:description|description)[\'"]',
                        text,
                        re.IGNORECASE,
                    )
                if match:
                    desc = html.unescape(match.group(1)).strip()
                    desc = re.sub(r"\s+", " ", desc)
                    if len(desc) >= 30 and not _is_low_quality_or_deal("", desc):
                        return resolved_url, desc[:400]
    except Exception:
        pass

    return resolved_url, ""


@dataclass
class NewsItem:
    title: str
    link: str
    summary: str
    source: str = "curated_rss"
    resolved_link: str = ""


class NewsProvider:
    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        raise NotImplementedError


class MultiSourceNewsProvider(NewsProvider):
    """
    Ingests curated editorial publisher feeds, supplements with Google News RSS,
    filters noise/deals, enriches summaries with factual leads, and deduplicates.
    """

    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        candidates: list[NewsItem] = []
        seen_keys: set[str] = set()

        # 1. First ingest from curated editorial RSS feeds if configured
        editorial_feeds = getattr(section, "rss_feeds", []) or []
        for feed_url in editorial_feeds:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:8]:
                    title = (getattr(entry, "title", "") or "").strip()
                    if not title or not _looks_english(title):
                        continue
                    if _is_low_quality_or_deal(title):
                        continue
                    key = _normalize_title_key(title)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
                    clean_summary = _clean_summary_text(raw_summary)

                    link = getattr(entry, "link", "") or ""
                    candidates.append(
                        NewsItem(
                            title=title,
                            link=link,
                            summary=clean_summary[:500],
                            source="curated_rss",
                            resolved_link=link,
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to fetch curated feed %s: %s", feed_url, exc)

        # 2. Ingest from Google News RSS (for breaking or niche coverage)
        google_items = self._fetch_google_news(section, max_items=12)
        for item in google_items:
            key = _normalize_title_key(item.title)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            candidates.append(item)

        # 3. Enrich items lacking substantive summaries using concurrent lead extraction
        items_to_enrich = [item for item in candidates if len(item.summary) < 40 or "news.google.com" in item.link]
        if items_to_enrich:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_item = {
                    executor.submit(extract_lead_from_url, item.link, 2.5): item
                    for item in items_to_enrich[:12]
                }
                for future in concurrent.futures.as_completed(future_to_item):
                    item = future_to_item[future]
                    try:
                        resolved, lead = future.result()
                        if resolved:
                            item.resolved_link = resolved
                        if lead and len(lead) > len(item.summary):
                            item.summary = lead
                    except Exception:
                        pass

        # 4. Final filter & selection: prefer items that have substantive summaries
        filtered: list[NewsItem] = []
        for item in candidates:
            if _is_low_quality_or_deal(item.title, item.summary):
                continue
            filtered.append(item)

        # Sort so items with substantive factual context rank first
        filtered.sort(key=lambda it: (len(it.summary) >= 30, it.source == "curated_rss"), reverse=True)
        selected = filtered[:max_items]

        logger.info(
            "MultiSourceNewsProvider selected %s items for section %s (curated: %s, total candidates: %s)",
            len(selected),
            section.code,
            sum(1 for x in selected if x.source == "curated_rss"),
            len(candidates),
        )
        return selected

    def _fetch_google_news(self, section: Section, max_items: int) -> list[NewsItem]:
        url = self._build_google_url(section)
        try:
            feed = feedparser.parse(url)
            items: list[NewsItem] = []
            for entry in feed.entries:
                title = (getattr(entry, "title", "") or "").strip()
                if not title or not _looks_english(title):
                    continue
                if _is_low_quality_or_deal(title):
                    continue
                items.append(
                    NewsItem(
                        title=title,
                        link=getattr(entry, "link", "") or "",
                        summary="",
                        source="google_news_rss",
                    )
                )
                if len(items) >= max_items:
                    break
            return items
        except Exception as exc:
            logger.warning("Google News fetch failed for %s: %s", section.code, exc)
            return []

    def _build_google_url(self, section: Section) -> str:
        if section.rss_url:
            return section.rss_url
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


class GoogleNewsRssProvider(NewsProvider):
    """Legacy single-source Google News RSS provider."""

    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        multi = MultiSourceNewsProvider()
        return multi.fetch_for_section(section, max_items)


class MockNewsProvider(NewsProvider):
    def fetch_for_section(self, section: Section, max_items: int) -> list[NewsItem]:
        items = [
            NewsItem(
                title=f"Mock {section.name} headline {i + 1}: major development today",
                link=f"https://example.com/{section.code}/story-{i + 1}",
                summary=f"Key factual background and context for {section.name} story {i + 1}.",
                source="mock",
                resolved_link=f"https://example.com/{section.code}/story-{i + 1}",
            )
            for i in range(max_items)
        ]
        return items


def get_news_provider(name: str = "multi_source") -> NewsProvider:
    providers: dict[str, NewsProvider] = {
        "multi_source": MultiSourceNewsProvider(),
        "google_news_rss": MultiSourceNewsProvider(),  # Enhanced transparently
        "mock": MockNewsProvider(),
    }
    if name not in providers:
        return MultiSourceNewsProvider()
    return providers[name]


def fetch_section_news(
    section: Section,
    output_dir: Path | None = None,
    *,
    provider_name: str = "multi_source",
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
