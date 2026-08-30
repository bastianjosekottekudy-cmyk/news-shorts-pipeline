"""Orchestrates section batch → Shorts (roundup or per-story)."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from src.audio.tts import generate_narration
from src.config import (
    Section,
    get_section,
    load_sections,
    local_run_date,
    section_output_dir,
)
from src.db import store
from src.images.fetcher import fetch_images_for_news
from src.images.keywords import enrich_news_with_image_queries
from src.naming import build_video_title
from src.news.fetcher import fetch_section_news
from src.script.generator import generate_script
from src.titles.clarity import clarify_news_titles, generate_display_title
from src.video.renderer import render_short

logger = logging.getLogger(__name__)


def _youtube_enabled() -> bool:
    from src.youtube.uploader import youtube_enabled

    return youtube_enabled()


def _attempt_youtube_upload(
    run_id: int,
    video_path: str,
    section: Section,
    news_items: list[dict[str, Any]],
    run_date: str,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str | None:
    from src.youtube.uploader import YouTubeUploadError, upload_short

    store.set_upload_status(run_id, "uploading", upload_error=None)
    store.append_step_log(run_id, "upload", "Uploading Short to YouTube")
    try:
        youtube_id = upload_short(
            video_path,
            section,
            news_items,
            run_date,
            index=index,
            total=total,
        )
        store.set_upload_status(
            run_id,
            "uploaded",
            youtube_video_id=youtube_id,
            upload_error=None,
        )
        store.append_step_log(
            run_id, "upload", f"Uploaded https://www.youtube.com/watch?v={youtube_id}"
        )
        return youtube_id
    except YouTubeUploadError as exc:
        msg = str(exc)
        if "upload skipped" in msg.lower():
            logger.info("YouTube upload skipped for run %s: %s", run_id, exc)
            store.set_upload_status(run_id, "none", upload_error=None)
            store.append_step_log(run_id, "upload", msg)
            return None
        logger.warning("YouTube upload failed for run %s: %s", run_id, exc)
        store.set_upload_status(run_id, "failed", upload_error=msg)
        store.append_step_log(run_id, "upload", f"Upload failed: {exc}")
        from src.scheduler import sync_failed_upload_retry_job

        sync_failed_upload_retry_job()
        return None
    except Exception as exc:
        logger.exception("Unexpected YouTube upload error for run %s", run_id)
        store.set_upload_status(run_id, "failed", upload_error=str(exc))
        store.append_step_log(run_id, "upload", f"Upload failed: {exc}")
        from src.scheduler import sync_failed_upload_retry_job

        sync_failed_upload_retry_job()
        return None


def run_single_short(
    section: Section,
    news_items: list[dict[str, Any]],
    *,
    run_date: str,
    batch_id: int,
    skip_upload: bool = True,
    force_upload: bool = False,
    mock_images: bool = False,
    existing_run_id: int | None = None,
    index: int | None = None,
    total: int | None = None,
) -> int:
    """Build one Short covering one or more news items. Returns run_id."""
    if isinstance(news_items, dict):
        news_items = [news_items]
    if not news_items:
        raise ValueError("news_items required")

    video_title = build_video_title(
        section.name, run_date, index=index, total=total
    )
    primary_title = video_title
    run_id = existing_run_id or store.create_run(
        section.code,
        section.name,
        run_date,
        batch_id=batch_id,
        news_title=primary_title,
    )
    if existing_run_id:
        store.update_run(
            run_id,
            batch_id=batch_id,
            news_title=primary_title,
            section_code=section.code,
            section_name=section.name,
        )

    output_dir = section_output_dir(section.code, run_date, run_id=run_id)
    logger.info(
        "Starting short run %s [%s] batch=%s → %s (%s stories)",
        run_id,
        section.code,
        batch_id,
        video_title,
        len(news_items),
    )

    try:
        store.append_step_log(
            run_id,
            "start",
            f"{video_title} — {len(news_items)} stor{'y' if len(news_items)==1 else 'ies'}",
        )

        store.append_step_log(run_id, "titles", "Clarifying news headlines")
        news_items = clarify_news_titles(section, news_items)
        store.update_run(
            run_id,
            news_json=json.dumps(news_items),
            news_link=str(news_items[0].get("link") or ""),
            news_title=primary_title,
        )

        store.append_step_log(run_id, "image_keywords", "Extracting image search keywords")
        news_items = enrich_news_with_image_queries(section, news_items)
        store.update_run(
            run_id,
            news_json=json.dumps(news_items),
            news_link=str(news_items[0].get("link") or ""),
        )

        store.append_step_log(run_id, "images", "Fetching related images per story")
        images_by_story: list[list[str]] = []
        for i, item in enumerate(news_items, start=1):
            story_dir = output_dir / f"story_{i}"
            story_dir.mkdir(parents=True, exist_ok=True)
            imgs = fetch_images_for_news(item, story_dir, mock=mock_images)
            images_by_story.append(imgs)

        store.append_step_log(run_id, "overlay", "Writing on-screen titles")
        display_title = generate_display_title(
            section.name,
            run_date,
            output_dir,
            story_count=len(news_items),
        )

        store.append_step_log(run_id, "script", "Generating Short narration")
        script_path = generate_script(section, news_items, output_dir)
        store.update_run(run_id, script_path=script_path)

        store.append_step_log(run_id, "tts", "Generating voiceover")
        audio_path = generate_narration(Path(script_path), section, output_dir)

        store.append_step_log(run_id, "render", "Rendering 9:16 Short")
        video_path = render_short(
            section.name,
            run_date,
            audio_path,
            output_dir,
            news_items=news_items,
            images_by_story=images_by_story,
            display_title=display_title,
            index=index,
            total=total,
        )
        store.update_run(run_id, video_path=video_path)

        youtube_id = None
        should_upload = force_upload or (not skip_upload and _youtube_enabled())
        if should_upload:
            youtube_id = _attempt_youtube_upload(
                run_id,
                video_path,
                section,
                news_items,
                run_date,
                index=index,
                total=total,
            )
        else:
            store.append_step_log(
                run_id,
                "local",
                f"Saved as '{video_title}.mp4' — upload from dashboard or enable auto-upload",
            )

        manifest: dict[str, Any] = {
            "run_id": run_id,
            "batch_id": batch_id,
            "section": section.code,
            "run_date": run_date,
            "news": news_items,
            "images_by_story": images_by_story,
            "display_title": display_title,
            "script_path": script_path,
            "video_title": video_title,
            "video_path": video_path,
            "youtube_video_id": youtube_id,
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        store.append_step_log(run_id, "done", "Short completed successfully")
        store.finish_run(run_id, "success")
        return run_id
    except Exception as exc:
        logger.exception("Short run %s failed: %s", run_id, exc)
        store.append_step_log(run_id, "error", str(exc))
        store.finish_run(run_id, "failed", error_message=str(exc))
        raise


def run_section_batch(
    section_code: str,
    *,
    run_date: str | None = None,
    news_provider: str = "google_news_rss",
    skip_upload: bool = True,
    force_upload: bool = False,
    news_count: int | None = None,
    shorts_count: int | None = None,
    count: int | None = None,
) -> list[int]:
    """
    Fetch `news_count` headlines.
    If shorts_count == 1: one roundup Short covering all fetched headlines.
    If shorts_count > 1: that many Shorts from the top headlines (one story each).
    """
    section = get_section(section_code)
    run_date = run_date or local_run_date(section)
    fetch_n = news_count if news_count is not None else (
        count if count is not None else int(section.news_count)
    )
    fetch_n = max(1, int(fetch_n))
    # Always exactly one Short per section covering all fetched headlines
    _ = shorts_count  # ignored; kept for CLI back-compat
    batch_id = store.next_batch_id()
    mock_images = news_provider == "mock"

    batch_dir = section_output_dir(section.code, run_date) / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Batch %s for %s: fetching %s headlines → 1 Short",
        batch_id,
        section.code,
        fetch_n,
    )
    news_items = fetch_section_news(
        section,
        batch_dir,
        provider_name=news_provider,
        max_items=fetch_n,
    )
    if not news_items:
        raise RuntimeError(f"No news items fetched for section {section.code}")

    run_ids: list[int] = []
    try:
        rid = run_single_short(
            section,
            news_items,
            run_date=run_date,
            batch_id=batch_id,
            skip_upload=skip_upload,
            force_upload=force_upload,
            mock_images=mock_images,
        )
        run_ids.append(rid)
    except Exception:
        logger.exception("Failed short in batch %s", batch_id)

    if not run_ids:
        raise RuntimeError(f"Short failed for section {section.code}")
    logger.info(
        "Batch %s complete for %s: 1 Short from %s headlines",
        batch_id,
        section.code,
        len(news_items),
    )
    return run_ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Run news Shorts pipeline for one section")
    parser.add_argument("--section", default=None, help="Section code (e.g. tech)")
    parser.add_argument("--date", default=None, help="Run date YYYY-MM-DD")
    parser.add_argument(
        "--news-count",
        type=int,
        default=None,
        help="Override headlines to fetch (default 3)",
    )
    parser.add_argument(
        "--shorts-count",
        type=int,
        default=None,
        help="Override Shorts to render (default 1 = roundup of all news)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="Deprecated: same as --news-count",
    )
    parser.add_argument("--mock", action="store_true", help="Use mock news + placeholder images")
    parser.add_argument("--upload", action="store_true", help="Force YouTube upload")
    parser.add_argument("--all", action="store_true", help="Run all sections")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    store.init_db()
    news_provider = "mock" if args.mock else "google_news_rss"
    skip_upload = not _youtube_enabled()

    if args.all:
        codes = [s.code for s in load_sections()]
    elif args.section:
        codes = [args.section]
    else:
        parser.error("Provide --section CODE or --all")

    all_ids: list[int] = []
    for code in codes:
        ids = run_section_batch(
            code,
            run_date=args.date,
            news_provider=news_provider,
            skip_upload=skip_upload,
            force_upload=args.upload,
            news_count=args.news_count,
            shorts_count=args.shorts_count,
            count=args.count,
        )
        all_ids.extend(ids)

    print(
        f"Completed {len(all_ids)} short(s). "
        "Dashboard: http://127.0.0.1:8081"
    )


if __name__ == "__main__":
    main()
