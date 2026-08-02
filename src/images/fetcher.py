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


def _safe_stem(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text)[:40].strip("_") or "news"


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


def _search_queries_from_title(title: str) -> list[str]:
    cleaned = re.sub(r"\s*[\|\-–—]\s*.*$", "", title or "").strip()
    # Drop common filler words for better stock/wiki hits
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
        "this", "that", "with", "from", "why", "how", "what", "when", "after",
        "into", "its", "it", "as", "at", "by", "be", "was", "were", "has", "have",
        "new", "now", "just", "about", "over", "under", "up", "down",
    }
    words = [
        w for w in re.findall(r"[A-Za-z0-9]{3,}", cleaned)
        if w.lower() not in stop
    ]
    queries: list[str] = []
    if len(words) >= 2:
        queries.append(" ".join(words[:3]))
    if words:
        queries.append(words[0])
    if not queries:
        queries.append(cleaned[:40] or "news")
    # Unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out


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
    providers = config.get("images", {}).get(
        "providers", ["news_og", "wikimedia", "openverse"]
    )

    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    title = str(news_item.get("title") or "news")
    link = str(news_item.get("link") or "")
    queries = _search_queries_from_title(title)
    stem = _safe_stem(title)

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
                urls.append(og)
                logger.info("Got og:image for short")

        # 2) Wikipedia thumbnail (often works when Commons API is blocked)
        if len(urls) < max_images:
            for q in queries:
                thumb = _wikipedia_thumbnail(client, q)
                if thumb and thumb not in urls:
                    urls.append(thumb)
                if len(urls) >= max_images:
                    break

        # 3) Wikimedia Commons
        if len(urls) < max_images and "wikimedia" in providers:
            for q in queries:
                for url in _wikimedia_urls(client, q, max_images):
                    if url not in urls:
                        urls.append(url)
                    if len(urls) >= max_images:
                        break
                if len(urls) >= max_images:
                    break

        # 4) Openverse stock photos keyed to headline keywords
        if len(urls) < max_images and "openverse" in providers:
            for q in queries:
                for url in _openverse_urls(client, q, max_images):
                    if url not in urls:
                        urls.append(url)
                    if len(urls) >= max_images:
                        break
                if len(urls) >= max_images:
                    break

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
        json.dumps(saved, indent=2), encoding="utf-8"
    )
    logger.info("Images for short '%s': %s", title[:60], len(saved))
    return saved
