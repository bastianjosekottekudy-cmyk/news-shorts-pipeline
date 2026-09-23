"""Orchestrates section batch → Shorts (roundup or per-story)."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import shutil
from src.audio.tts import generate_narration
from src.config import (
    OUTPUT_DIR,
    Section,
    get_section,
    load_sections,
    local_run_date,
    section_output_dir,
    should_delete_after_upload,
)
from src.db import store
from src import job_control
from src.job_control import JobStoppedError, check_stop, register_run, unregister_run
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


def _cleanup_run_media(run_id: int, video_path: str) -> None:
    try:
        p = Path(video_path)
        run_dir = p.parent
        if run_dir.is_dir() and run_dir.name.startswith("run_"):
            shutil.rmtree(run_dir, ignore_errors=True)
            logger.info("Deleted local run directory after upload for run %s: %s", run_id, run_dir)
            store.append_step_log(run_id, "cleanup", f"Deleted local run folder: {run_dir.name}")
            for parent in (run_dir.parent, run_dir.parent.parent):
                try:
                    if parent.is_dir() and parent.resolve() != OUTPUT_DIR.resolve():
                        if not any(parent.iterdir()):
                            parent.rmdir()
                except OSError:
                    pass
        elif p.is_file():
            p.unlink(missing_ok=True)
            logger.info("Deleted local video after upload for run %s: %s", run_id, video_path)
            store.append_step_log(run_id, "cleanup", f"Deleted local video: {p.name}")
        store.mark_run_dashboard_deleted(run_id)
        logger.info("Removed uploaded run %s from dashboard", run_id)
        store.append_step_log(run_id, "cleanup", "Deleted uploaded item from dashboard")
    except Exception as del_exc:
        logger.warning(
            "Failed to delete local video/dashboard item for run %s (%s): %s",
            run_id,
            video_path,
            del_exc,
        )


def _attempt_youtube_upload(
    run_id: int,
    video_path: str,
    section: Section,
    news_items: list[dict[str, Any]],
    run_date: str,
    *,
    index: int | None = None,
    total: int | None = None,
    delete_after_upload: bool | None = None,
    force_upload: bool = False,
) -> str | None:
    from src.youtube.uploader import YouTubeUploadError, upload_short

    run = store.get_run(run_id) or {}
    primary_title = str(run.get("news_title") or (news_items[0].get("title") if news_items else "") or "").strip()
    primary_link = str(run.get("news_link") or (news_items[0].get("link") if news_items else "") or "").strip()

    # Prevent re-uploading an already uploaded topic/short
    if not force_upload:
        if (
            run.get("upload_status") == "uploaded"
            and run.get("youtube_video_id")
            and run.get("youtube_video_id") != "skipped"
        ):
            logger.info("Run %s already uploaded as %s; skipping re-upload", run_id, run.get("youtube_video_id"))
            return str(run.get("youtube_video_id"))

        if primary_title and store.is_topic_uploaded(primary_title):
            logger.warning("Topic/headline %r already uploaded to YouTube; skipping re-upload for run %s", primary_title, run_id)
            store.set_upload_status(run_id, "none", upload_error=None)
            store.append_step_log(run_id, "upload", f"Topic '{primary_title}' already uploaded to YouTube; skipped re-upload")
            return None

    store.set_upload_status(run_id, "uploading", upload_error=None)
    store.append_step_log(run_id, "upload", "Uploading Short to YouTube")
    check_stop(run_id, section.code)
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
        if primary_title:
            store.record_uploaded_topic(
                topic=primary_title,
                section_code=section.code,
                news_title=primary_title,
                news_link=primary_link,
                run_id=run_id,
            )
        if should_delete_after_upload(delete_after_upload):
            _cleanup_run_media(run_id, video_path)
        return youtube_id
    except JobStoppedError:
        store.set_upload_status(run_id, "none", upload_error=None)
        raise
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
    delete_after_upload: bool | None = None,
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
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            batch_id=batch_id,
            news_title=primary_title,
            section_code=section.code,
            section_name=section.name,
        )

    register_run(run_id, section.code)
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
        check_stop(run_id, section.code)
        store.append_step_log(
            run_id,
            "start",
            f"{video_title} — {len(news_items)} stor{'y' if len(news_items)==1 else 'ies'}",
        )

        check_stop(run_id, section.code)
        store.append_step_log(run_id, "titles", "Clarifying news headlines")
        news_items = clarify_news_titles(section, news_items)
        store.update_run(
            run_id,
            news_json=json.dumps(news_items),
            news_link=str(news_items[0].get("link") or ""),
            news_title=primary_title,
        )

        check_stop(run_id, section.code)
        store.append_step_log(run_id, "image_keywords", "Extracting image search keywords")
        news_items = enrich_news_with_image_queries(section, news_items)
        store.update_run(
            run_id,
            news_json=json.dumps(news_items),
            news_link=str(news_items[0].get("link") or ""),
        )

        check_stop(run_id, section.code)
        store.append_step_log(run_id, "images", "Fetching related images per story")
        images_by_story: list[list[str]] = []
        for i, item in enumerate(news_items, start=1):
            check_stop(run_id, section.code)
            story_dir = output_dir / f"story_{i}"
            story_dir.mkdir(parents=True, exist_ok=True)
            imgs = fetch_images_for_news(item, story_dir, mock=mock_images)
            images_by_story.append(imgs)

        check_stop(run_id, section.code)
        store.append_step_log(run_id, "overlay", "Writing on-screen titles")
        display_title = generate_display_title(
            section.name,
            run_date,
            output_dir,
            story_count=len(news_items),
        )

        check_stop(run_id, section.code)
        store.append_step_log(run_id, "script", "Generating Short narration")
        script_path = generate_script(section, news_items, output_dir)
        store.update_run(run_id, script_path=script_path)

        check_stop(run_id, section.code)
        store.append_step_log(run_id, "tts", "Generating voiceover")
        audio_path = generate_narration(Path(script_path), section, output_dir)

        check_stop(run_id, section.code)
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
            run_id=run_id,
        )
        store.update_run(run_id, video_path=video_path)

        check_stop(run_id, section.code)

        # Write initial manifest prior to upload while output_dir is guaranteed intact
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
            "youtube_video_id": None,
            "video_deleted": False,
        }
        try:
            (output_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
        except Exception as m_exc:
            logger.warning("Could not write initial manifest.json for run %s: %s", run_id, m_exc)

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
                delete_after_upload=False,  # Defer cleanup until run is finalized as success
                force_upload=force_upload,
            )
        else:
            store.append_step_log(
                run_id,
                "local",
                f"Saved as '{video_title}.mp4' — upload from dashboard or enable auto-upload",
            )

        manifest["youtube_video_id"] = youtube_id
        manifest["video_deleted"] = bool(youtube_id and not Path(video_path).is_file())
        if output_dir.is_dir():
            try:
                (output_dir / "manifest.json").write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
            except Exception as m_exc:
                logger.warning("Could not update manifest.json for run %s: %s", run_id, m_exc)

        store.append_step_log(run_id, "done", "Short completed successfully")
        store.finish_run(run_id, "success")

        # Post-completion media cleanup if delete_after_upload is enabled
        if should_upload and should_delete_after_upload(delete_after_upload):
            curr_run = store.get_run(run_id)
            if curr_run and curr_run.get("upload_status") == "uploaded":
                _cleanup_run_media(run_id, video_path)

        return run_id
    except JobStoppedError as exc:
        logger.info("Short run %s stopped: %s", run_id, exc)
        store.stop_run(run_id, reason=str(exc))
        return run_id
    except Exception as exc:
        curr = store.get_run(run_id)
        if curr and (curr.get("upload_status") == "uploaded" or curr.get("youtube_video_id")):
            logger.warning(
                "Short run %s encountered post-upload error: %s; preserving success status",
                run_id,
                exc,
            )
            store.append_step_log(run_id, "warning", f"Post-upload warning: {exc}")
            store.finish_run(run_id, "success")
            return run_id
        logger.exception("Short run %s failed: %s", run_id, exc)
        store.append_step_log(run_id, "error", str(exc))
        store.finish_run(run_id, "failed", error_message=str(exc))
        raise
    finally:
        unregister_run(run_id)


def run_section_batch(
    section_code: str,
    *,
    run_date: str | None = None,
    news_provider: str = "google_news_rss",
    skip_upload: bool = True,
    force_upload: bool = False,
    delete_after_upload: bool | None = None,
    news_count: int | None = None,
    shorts_count: int | None = None,
    count: int | None = None,
    existing_run_id: int | None = None,
    batch_id: int | None = None,
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
    target_shorts = max(1, int(shorts_count or 1))
    batch_id = batch_id if batch_id is not None else store.next_batch_id()
    mock_images = news_provider == "mock"

    batch_dir = section_output_dir(section.code, run_date) / f"batch_{batch_id}"
    batch_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Batch %s for %s: fetching %s headlines → %s Short(s)",
        batch_id,
        section.code,
        fetch_n,
        target_shorts,
    )
    check_stop(existing_run_id, section.code)
    try:
        news_items = fetch_section_news(
            section,
            batch_dir,
            provider_name=news_provider,
            max_items=fetch_n,
        )
        if not news_items:
            raise RuntimeError(f"No news items fetched for section {section.code}")
    except JobStoppedError:
        if existing_run_id:
            store.stop_run(existing_run_id, reason="Stopped by user")
        raise
    except Exception as exc:
        if existing_run_id:
            store.finish_run(existing_run_id, "failed", error_message=str(exc))
        raise

    check_stop(existing_run_id, section.code)
    run_ids: list[int] = []
    try:
        if target_shorts > 1 and len(news_items) >= target_shorts:
            chunk_size = max(1, len(news_items) // target_shorts)
            for s_idx in range(target_shorts):
                chunk = (
                    news_items[s_idx * chunk_size : (s_idx + 1) * chunk_size]
                    if s_idx < target_shorts - 1
                    else news_items[s_idx * chunk_size :]
                )
                if not chunk:
                    continue
                rid = run_single_short(
                    section,
                    chunk,
                    run_date=run_date,
                    batch_id=batch_id,
                    skip_upload=skip_upload,
                    force_upload=force_upload,
                    delete_after_upload=delete_after_upload,
                    mock_images=mock_images,
                    existing_run_id=existing_run_id if s_idx == 0 else None,
                    index=s_idx + 1,
                    total=target_shorts,
                )
                run_ids.append(rid)
        else:
            rid = run_single_short(
                section,
                news_items,
                run_date=run_date,
                batch_id=batch_id,
                skip_upload=skip_upload,
                force_upload=force_upload,
                delete_after_upload=delete_after_upload,
                mock_images=mock_images,
                existing_run_id=existing_run_id,
            )
            run_ids.append(rid)
    except JobStoppedError:
        logger.info("Batch %s for section %s stopped by user", batch_id, section.code)
        if existing_run_id:
            store.stop_run(existing_run_id, reason="Stopped by user")
        return run_ids
    except Exception as exc:
        logger.exception("Failed short in batch %s", batch_id)
        if existing_run_id:
            curr = store.get_run(existing_run_id)
            if curr and (curr.get("upload_status") == "uploaded" or curr.get("youtube_video_id")):
                store.finish_run(existing_run_id, "success")
                run_ids.append(existing_run_id)
            else:
                store.finish_run(existing_run_id, "failed", error_message=str(exc))

    if not run_ids:
        if job_control.is_stop_requested(existing_run_id, section.code):
            return run_ids
        if existing_run_id:
            curr = store.get_run(existing_run_id)
            if curr and (curr.get("upload_status") == "uploaded" or curr.get("youtube_video_id")):
                return [existing_run_id]
        raise RuntimeError(f"Short failed for section {section.code}")
    logger.info(
        "Batch %s complete for %s: 1 Short from %s headlines",
        batch_id,
        section.code,
        len(news_items),
    )
    return run_ids


def retry_single_short(
    run_id: int,
    *,
    mock: bool = False,
    skip_upload: bool = True,
    force_upload: bool = False,
    delete_after_upload: bool | None = None,
) -> int:
    """
    Retry a failed or stopped run, reusing existing headlines if available or fetching fresh ones.
    Updates the existing run record rather than creating a new one.
    """
    run = store.get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    if run.get("status") == "running":
        raise ValueError(f"Run {run_id} is already running")

    section = get_section(run["section_code"])
    run_date = run.get("run_date") or local_run_date(section)
    batch_id = int(run.get("batch_id") or store.next_batch_id())

    store.reset_run_for_retry(run_id)
    job_control.clear_stop(run_id, section.code)

    news_items: list[dict[str, Any]] | None = None
    if run.get("news_json"):
        try:
            parsed = json.loads(run["news_json"])
            if isinstance(parsed, list) and parsed:
                news_items = parsed
        except Exception:
            news_items = None

    if not news_items:
        batch_dir = section_output_dir(section.code, run_date) / f"batch_{batch_id}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        news_provider = "mock" if mock else "google_news_rss"
        news_items = fetch_section_news(
            section,
            batch_dir,
            provider_name=news_provider,
            max_items=int(section.news_count),
        )

    return run_single_short(
        section,
        news_items,
        run_date=run_date,
        batch_id=batch_id,
        skip_upload=skip_upload,
        force_upload=force_upload,
        delete_after_upload=delete_after_upload,
        mock_images=mock,
        existing_run_id=run_id,
    )


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
    parser.add_argument(
        "--delete-after-upload",
        action="store_true",
        default=None,
        help="Delete local video file after successful YouTube upload (default: true)",
    )
    parser.add_argument(
        "--keep-video",
        "--no-delete-after-upload",
        dest="delete_after_upload",
        action="store_false",
        help="Keep local video file after YouTube upload (do not auto-delete)",
    )
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
            delete_after_upload=args.delete_after_upload,
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
