"""YouTube Shorts upload — same OAuth/secrets as trends-video-pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from src.config import Section, get_env, load_pipeline_config
from src.naming import build_video_title, sanitize_news_title
from src.youtube.auth import get_credentials

logger = logging.getLogger(__name__)


class YouTubeUploadError(RuntimeError):
    """Raised when an upload cannot proceed or fails."""


def _build_description(
    section: Section,
    news_items: list[dict[str, Any]],
    run_date: str,
    video_title: str,
) -> str:
    lines = [
        video_title,
        "",
        f"Top {section.name} news · {run_date}",
        "",
        "Stories:",
    ]
    for idx, item in enumerate(news_items, start=1):
        headline = sanitize_news_title(str(item.get("title") or f"Story {idx}"))
        lines.append(f"{idx}. {headline}")
        link = str(item.get("resolved_link") or item.get("link") or "").strip()
        if link and "news.google.com" not in link:
            lines.append(f"   {link}")
    lines.extend(
        [
            "",
            "#Shorts",
            f"#{section.code}",
            "#News",
            "#NewsShorts",
        ]
    )
    return "\n".join(lines)


def youtube_enabled() -> bool:
    config = load_pipeline_config()
    return bool(config.get("youtube", {}).get("enabled", False))


def upload_short(
    video_path: str,
    section: Section,
    news_items: list[dict[str, Any]] | dict[str, Any],
    run_date: str,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str:
    if isinstance(news_items, dict):
        news_items = [news_items]

    skip = get_env("SKIP_YOUTUBE_UPLOAD", "false").strip().lower()
    if skip in ("true", "1", "yes"):
        raise YouTubeUploadError(
            "YouTube upload skipped (SKIP_YOUTUBE_UPLOAD=true). "
            "Set SKIP_YOUTUBE_UPLOAD=false in .env and restart the app."
        )

    if not youtube_enabled():
        raise YouTubeUploadError(
            "YouTube upload is disabled (set youtube.enabled: true in config/pipeline.yaml)"
        )

    path = Path(video_path)
    if not path.is_file():
        raise YouTubeUploadError(f"Video file not found: {video_path}")

    config = load_pipeline_config()
    yt_cfg = config.get("youtube", {})

    try:
        creds = get_credentials()
    except Exception as exc:
        raise YouTubeUploadError(
            f"YouTube auth failed. Run: python -m src.youtube.auth ({exc})"
        ) from exc

    youtube = build("youtube", "v3", credentials=creds)

    title = build_video_title(
        section.name, run_date, index=index, total=total
    )
    description = _build_description(section, news_items, run_date, title)
    tags = list(section.youtube_tags) + ["shorts", "news", "news shorts"]

    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": str(yt_cfg.get("category_id", "25")),
        },
        "status": {
            "privacyStatus": yt_cfg.get("privacy", "public"),
            "selfDeclaredMadeForKids": bool(yt_cfg.get("made_for_kids", False)),
        },
    }

    media = MediaFileUpload(str(path), chunksize=256 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("Upload progress: %.1f%%", status.progress() * 100)
    except Exception as exc:
        raise YouTubeUploadError(f"YouTube API upload failed: {exc}") from exc

    if not response or not response.get("id"):
        raise YouTubeUploadError("YouTube API returned no video id")

    video_id = response["id"]
    logger.info("Uploaded Short: https://youtube.com/watch?v=%s", video_id)
    return video_id
