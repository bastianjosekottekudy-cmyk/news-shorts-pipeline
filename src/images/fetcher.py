"""Fetch related still images for a single news Short."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from src.config import load_pipeline_config
from src.images.keywords import heuristic_image_queries

logger = logging.getLogger(__name__)

# Browser-like UA — Wikimedia/Openverse reject generic bot strings with 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

_LOW_VALUE_URL_RE = re.compile(
    r"(?:^|/)(?:logo|icon|sprite|favicon|badge)(?:[._/-]|$)|\.svg(?:\?|$)",
    re.IGNORECASE,
)


def _safe_stem(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text)[:40].strip("_") or "news"


def _is_low_value_url(url: str) -> bool:
    return bool(_LOW_VALUE_URL_RE.search(url or ""))


def _download_image(client: httpx.Client, url: str, dest: Path) -> bool:
    try:
        resp = client.get(url, follow_redirects=True, timeout=20.0)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type and not url.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        ):
            return False
        data = resp.content
        if len(data) < 2000:
            return False
        dest.write_bytes(data)
        return True
    except Exception as exc:
        logger.debug("Image download failed %s: %s", url, exc)
        return False


def _decode_google_news_url(url: str) -> str | None:
    """Resolve news.google.com/rss/articles/... to the publisher URL."""
    if not url or "news.google.com" not in url:
        return None
    try:
        from googlenewsdecoder import gnewsdecoder

        result = gnewsdecoder(url, interval=1)
        if isinstance(result, dict) and result.get("status") and result.get("decoded_url"):
            decoded = str(result["decoded_url"]).strip()
            if decoded.startswith("http"):
                logger.info("Decoded Google News URL → %s", decoded[:100])
                return decoded
    except Exception as exc:
        logger.warning("Google News URL decode failed: %s", exc)
    return None


def _resolve_article_url(link: str) -> str:
    if not link:
        return ""
    if "news.google.com" in link:
        return _decode_google_news_url(link) or link
    return link


def _wikimedia_urls(client: httpx.Client, query: str, limit: int) -> list[str]:
    try:
        resp = client.get(
            WIKIMEDIA_API,
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": max(limit * 2, 4),
                "gsrnamespace": 6,
                "prop": "imageinfo",
                "iiprop": "url|mime|size",
                "iiurlwidth": 1080,
                "format": "json",
            },
            timeout=20.0,
        )
        if resp.status_code == 403:
            logger.warning("Wikimedia returned 403 for '%s'", query)
            return []
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        urls: list[str] = []
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            mime = str(info.get("mime", ""))
            if not mime.startswith("image/") or "svg" in mime:
                continue
            url = info.get("thumburl") or info.get("url")
            if url:
                urls.append(url)
            if len(urls) >= limit:
                break
        return urls
    except Exception as exc:
        logger.warning("Wikimedia search failed for '%s': %s", query, exc)
        return []


def _openverse_urls(client: httpx.Client, query: str, limit: int) -> list[str]:
    try:
        resp = client.get(
            OPENVERSE_API,
            params={
                "q": query,
                "page_size": max(limit, 3),
            },
            timeout=20.0,
            headers={"Accept": "application/json"},
        )
        if resp.status_code in (401, 403):
            return []
        resp.raise_for_status()
        results = resp.json().get("results") or []
        urls: list[str] = []
        for item in results:
            url = item.get("url") or item.get("thumbnail")
            if url:
                urls.append(url)
            if len(urls) >= limit:
                break
        return urls
    except Exception as exc:
        logger.warning("Openverse search failed for '%s': %s", query, exc)
        return []


def _wikipedia_thumbnail(client: httpx.Client, query: str) -> str | None:
    """Try Wikipedia page summary thumbnail for a short keyword."""
    terms = [w for w in query.split() if len(w) > 2][:3]
    candidates = ["_".join(terms)] if terms else []
    if terms:
        candidates.append(terms[0])
        if len(terms) >= 2:
            candidates.append("_".join(terms[:2]))
    for term in candidates:
        try:
            resp = client.get(
                WIKI_SUMMARY + quote(term, safe=""),
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                continue
            thumb = (resp.json().get("thumbnail") or {}).get("source")
            if thumb and str(thumb).startswith("http"):
                return str(thumb)
        except Exception:
            continue
    return None


def _og_image_url(client: httpx.Client, page_url: str) -> str | None:
    if not page_url or not page_url.startswith("http"):
        return None
    if "news.google.com" in page_url or "consent.google.com" in page_url:
        return None
    try:
        resp = client.get(page_url, follow_redirects=True, timeout=15.0)
        resp.raise_for_status()
        final = str(resp.url)
        if "consent.google.com" in final:
            return None
        html = resp.text[:200_000]
        patterns = [
            r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image(?::secure_url)?["\']',
            r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                url = match.group(1).strip()
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http") and "google.com/images" not in url:
                    return url
    except Exception as exc:
        logger.debug("og:image fetch failed for %s: %s", page_url, exc)
    return None


def _resolve_story_queries(news_item: dict[str, Any]) -> tuple[str, list[str]]:
    raw_queries = news_item.get("image_queries")
    entity = str(news_item.get("primary_entity") or "").strip()
    queries: list[str] = []
    if isinstance(raw_queries, list):
        for q in raw_queries:
            text = str(q or "").strip()
            if text and text.lower() not in {x.lower() for x in queries}:
                queries.append(text)
    if queries:
        if not entity:
            entity = queries[0]
        return entity, queries
    return heuristic_image_queries(str(news_item.get("title") or "news"))


def _make_placeholder(dest: Path, label: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1080, 1920), color=(22, 32, 48))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    text = (label or "News")[:80]
    draw.rectangle([(40, 800), (1040, 1120)], fill=(40, 70, 110))
    draw.text((80, 900), text, fill=(255, 255, 255), font=font)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, format="JPEG", quality=85)


def fetch_images_for_news(
    news_item: dict[str, Any],
    output_dir: Path,
    *,
    mock: bool = False,
) -> list[str]:
    """
    Download related images for one news Short.
    Returns list of local image paths.
    """
    config = load_pipeline_config()
    max_images = int(config.get("max_images_per_short", 4))
    img_cfg = config.get("images") or {}
    providers = img_cfg.get("providers", ["news_og", "wikimedia", "openverse"])
    # Prefer fewer downloads aligned with renderer (uses up to 2).
    max_images = min(max_images, 3) if max_images > 3 else max_images

    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    title = str(news_item.get("title") or "news")
    link = str(news_item.get("link") or "")
    primary_entity, queries = _resolve_story_queries(news_item)
    stem = _safe_stem(title)

    logger.info(
        "Image search for '%s' entity=%r queries=%s",
        title[:50],
        primary_entity,
        queries,
    )

    if mock:
        saved: list[str] = []
        for i in range(min(max_images, 2)):
            dest = images_dir / f"01_{stem}_{i + 1}.jpg"
            _make_placeholder(dest, title)
            saved.append(str(dest))
        (output_dir / "images.json").write_text(
            json.dumps(saved, indent=2), encoding="utf-8"
        )
        return saved

    urls: list[str] = []
    low_value: list[str] = []
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        # 1) Decode Google News → publisher og:image / twitter:image
        if "news_og" in providers and link:
            article_url = _resolve_article_url(link)
            if article_url and article_url != link:
                news_item["resolved_link"] = article_url
            og = _og_image_url(client, article_url)
            if og:
                if _is_low_value_url(og):
                    low_value.append(og)
                else:
                    urls.append(og)
                    logger.info("Got og:image for short")

        wiki_enabled = "wikimedia" in providers or "wikipedia" in providers
        search_order = []
        if primary_entity:
            search_order.append(primary_entity)
        for q in queries:
            if q.lower() not in {x.lower() for x in search_order}:
                search_order.append(q)

        # 2) Wikipedia thumbnail on primary entity first
        if len(urls) < max_images and wiki_enabled:
            for q in search_order:
                thumb = _wikipedia_thumbnail(client, q)
                if not thumb:
                    continue
                if _is_low_value_url(thumb):
                    if thumb not in low_value:
                        low_value.append(thumb)
                    continue
                if thumb not in urls:
                    urls.append(thumb)
                if len(urls) >= max_images:
                    break

        # 3) Wikimedia Commons
        if len(urls) < max_images and "wikimedia" in providers:
            for q in search_order:
                for url in _wikimedia_urls(client, q, max_images):
                    if _is_low_value_url(url):
                        if url not in low_value:
                            low_value.append(url)
                        continue
                    if url not in urls:
                        urls.append(url)
                    if len(urls) >= max_images:
                        break
                if len(urls) >= max_images:
                    break

        # 4) Openverse stock photos
        if len(urls) < max_images and "openverse" in providers:
            for q in search_order:
                for url in _openverse_urls(client, q, max_images):
                    if _is_low_value_url(url):
                        if url not in low_value:
                            low_value.append(url)
                        continue
                    if url not in urls:
                        urls.append(url)
                    if len(urls) >= max_images:
                        break
                if len(urls) >= max_images:
                    break

        # Fill remaining with low-value only if nothing better
        for url in low_value:
            if len(urls) >= max_images:
                break
            if url not in urls:
                urls.append(url)

        saved: list[str] = []
        for i, url in enumerate(urls[:max_images], start=1):
            ext = ".jpg"
            lower = url.lower()
            if ".png" in lower:
                ext = ".png"
            elif ".webp" in lower:
                ext = ".webp"
            dest = images_dir / f"01_{stem}_{i}{ext}"
            if _download_image(client, url, dest):
                saved.append(str(dest))

    if not saved:
        logger.warning("No related images for '%s'; writing fallback slide", title)
        dest = images_dir / f"01_{stem}_fallback.jpg"
        _make_placeholder(dest, title)
        saved.append(str(dest))

    (output_dir / "images.json").write_text(
        json.dumps(
            {
                "paths": saved,
                "primary_entity": primary_entity,
                "image_queries": queries,
            },
            indent=2,
            ),
        encoding="utf-8",
    )
    logger.info("Images for short '%s': %s", title[:60], len(saved))
    return saved
