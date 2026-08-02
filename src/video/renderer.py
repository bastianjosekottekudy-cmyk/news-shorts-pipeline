"""Vertical Shorts renderer: related image slides + narration."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from src.config import load_pipeline_config
from src.naming import build_video_title, video_filename
from src.titles.clarity import story_card

logger = logging.getLogger(__name__)


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
        ]
    )
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cover_resize(img: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def _draw_gradient(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    for y in range(height // 3):
        alpha = int(180 * (1 - y / (height / 3)))
        draw.rectangle([(0, y), (width, y + 1)], fill=(0, 0, 0, alpha))
    for i, y in enumerate(range(height - int(height * 0.5), height)):
        alpha = min(230, int(240 * (i / (height * 0.5))))
        draw.rectangle([(0, y), (width, y + 1)], fill=(0, 0, 0, alpha))


def _wrap_text(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    shadow: tuple[int, int, int] = (0, 0, 0),
) -> None:
    x, y = xy
    for dx, dy in ((3, 3), (2, 2), (-1, 2)):
        draw.text((x + dx, y + dy), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_title_block(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    title: str,
    subtitle: str,
    *,
    margin: int = 56,
) -> None:
    title_font = _get_font(54, bold=True)
    sub_font = _get_font(30, bold=False)
    label_font = _get_font(26, bold=True)
    max_text_width = width - margin * 2

    label_y = int(height * 0.55)
    _draw_text_with_shadow(
        draw, (margin, label_y), "NEWS SHORT", label_font, fill=(120, 200, 255)
    )
    title_y = label_y + 44

    title_lines = _wrap_text(title, title_font, max_text_width, draw)[:5]
    line_h = 64
    for i, line in enumerate(title_lines):
        _draw_text_with_shadow(
            draw,
            (margin, title_y + i * line_h),
            line,
            title_font,
            fill=(255, 255, 255),
        )

    if subtitle:
        sub_y = title_y + len(title_lines) * line_h + 20
        sub_lines = _wrap_text(subtitle, sub_font, max_text_width, draw)[:2]
        for i, line in enumerate(sub_lines):
            _draw_text_with_shadow(
                draw,
                (margin, sub_y + i * 38),
                line,
                sub_font,
                fill=(230, 235, 240),
            )

    draw.rectangle([(0, height - 12), (width, height)], fill=(100, 180, 255))


def _make_solid_slide(
    width: int,
    height: int,
    title: str,
    subtitle: str,
) -> Image.Image:
    img = Image.new("RGB", (width, height), color=(14, 20, 34))
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(220):
        alpha = int(50 * (1 - y / 220))
        draw.rectangle([(0, y), (width, y + 1)], fill=(40, 80, 140, alpha))
    draw_rgb = ImageDraw.Draw(img)
    _draw_title_block(draw_rgb, width, height, title, subtitle)
    return img.convert("RGB")


def _make_image_slide(
    width: int,
    height: int,
    image_path: str,
    title: str,
    subtitle: str,
) -> Image.Image:
    try:
        base = Image.open(image_path).convert("RGB")
        base = _cover_resize(base, width, height)
        base = ImageEnhance.Brightness(base).enhance(0.65)
        base = ImageEnhance.Contrast(base).enhance(1.08)
    except Exception as exc:
        logger.warning("Could not open image %s: %s", image_path, exc)
        return _make_solid_slide(width, height, title, subtitle)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _draw_gradient(draw, width, height)
    composed = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw2 = ImageDraw.Draw(composed)
    _draw_title_block(draw2, width, height, title, subtitle)
    return composed


def _nvenc_available() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        enc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if "h264_nvenc" not in (enc.stdout or ""):
            return False
        gpu = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return gpu.returncode == 0 and bool(gpu.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_encoder(video_cfg: dict[str, Any]) -> tuple[str, list[str]]:
    preferred = str(video_cfg.get("codec", "auto")).lower()
    if preferred in ("h264_nvenc", "nvenc", "auto") and _nvenc_available():
        logger.info("Using NVIDIA NVENC (GPU) for video encode")
        return "h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    if preferred == "h264_nvenc":
        logger.warning("h264_nvenc requested but unavailable; falling back to libx264")
    logger.info("Using CPU libx264 for video encode")
    return "libx264", ["-preset", "veryfast", "-crf", "23"]


def _load_segment_durations(output_dir: Path) -> list[dict[str, Any]] | None:
    meta_path = output_dir / "narration_segments.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    return segments


def render_short(
    section_name: str,
    run_date: str,
    audio_path: str,
    output_dir: Path,
    *,
    news_items: list[dict[str, Any]] | None = None,
    images_by_story: list[list[str]] | None = None,
    display_title: dict[str, str] | None = None,
    index: int | None = None,
    total: int | None = None,
) -> str:
    """
    Render one 9:16 Short roundup.
    Slide groups: intro + one group per news story + outro (matches TTS segments).
    """
    config = load_pipeline_config()
    video_cfg = config.get("video", {})
    width = int(video_cfg.get("width", 1080))
    height = int(video_cfg.get("height", 1920))
    fps = int(video_cfg.get("fps", 24))
    max_duration = float(config.get("max_video_duration_sec", 58))
    title = (display_title or {}).get("title") or build_video_title(
        section_name, run_date, index=index, total=total
    )
    subtitle = (display_title or {}).get("subtitle") or f"{section_name} · News Short"
    codec, ffmpeg_params = _resolve_encoder(video_cfg)
    news_items = news_items or []
    images_by_story = images_by_story or [[] for _ in news_items]

    from moviepy import AudioFileClip, ImageClip, concatenate_videoclips

    slides_dir = output_dir / "slides"
    slides_dir.mkdir(exist_ok=True)

    groups: list[list[Path]] = []

    intro = _make_solid_slide(width, height, title, subtitle)
    intro_path = slides_dir / "00_intro.png"
    intro.save(intro_path, optimize=True)
    groups.append([intro_path])

    for idx, item in enumerate(news_items, start=1):
        card = story_card(str(item.get("title") or f"Story {idx}"), section_name, idx)
        story_title = card["title"]
        story_sub = card["subtitle"]
        story_images = images_by_story[idx - 1] if idx - 1 < len(images_by_story) else []
        group: list[Path] = []
        if story_images:
            # Prefer 1–2 images per story so pacing stays tight
            for img_i, img_path in enumerate(story_images[:2], start=1):
                slide = _make_image_slide(
                    width, height, img_path, story_title, story_sub
                )
                slide_path = slides_dir / f"{idx:02d}_story_{img_i}.png"
                slide.save(slide_path, optimize=True)
                group.append(slide_path)
        else:
            slide = _make_solid_slide(width, height, story_title, story_sub)
            slide_path = slides_dir / f"{idx:02d}_story.png"
            slide.save(slide_path, optimize=True)
            group.append(slide_path)
        groups.append(group)

    if not news_items:
        slide = _make_solid_slide(width, height, title, subtitle)
        slide_path = slides_dir / "01_story.png"
        slide.save(slide_path, optimize=True)
        groups.append([slide_path])

    outro = _make_solid_slide(
        width,
        height,
        "Thanks for watching",
        "Follow for more news Shorts",
    )
    outro_path = slides_dir / "99_outro.png"
    outro.save(outro_path, optimize=True)
    groups.append([outro_path])

    audio = AudioFileClip(audio_path)
    audio_duration = float(audio.duration)
    if audio_duration > max_duration:
        logger.warning(
            "Audio %.1fs exceeds Shorts cap %.1fs — trimming",
            audio_duration,
            max_duration,
        )
        audio = audio.subclipped(0, max_duration)
        audio_duration = max_duration

    timed_segments = _load_segment_durations(output_dir)
    clips: list[Any] = []

    if timed_segments and len(timed_segments) == len(groups):
        for group, segment in zip(groups, timed_segments):
            group_dur = float(segment.get("duration_sec") or 0.0)
            if group_dur <= 0:
                group_dur = max(audio_duration / len(groups), 1.2)
            per_slide = max(group_dur / len(group), 0.35)
            for path in group:
                clips.append(
                    ImageClip(str(path)).with_duration(per_slide).with_fps(fps)
                )
    else:
        if timed_segments:
            logger.warning(
                "Segment count (%s) != slide groups (%s); equal timing",
                len(timed_segments),
                len(groups),
            )
        all_paths = [p for g in groups for p in g]
        per_slide = max(audio_duration / len(all_paths), 1.2)
        clips = [
            ImageClip(str(path)).with_duration(per_slide).with_fps(fps)
            for path in all_paths
        ]

    video = concatenate_videoclips(clips, method="compose")
    video = video.with_audio(audio)

    if video.duration > audio_duration:
        video = video.subclipped(0, audio_duration)

    output_path = output_dir / video_filename(
        section_name, run_date, index=index, total=total
    )
    write_kwargs: dict[str, Any] = {
        "fps": fps,
        "codec": codec,
        "audio_codec": "aac",
        "logger": None,
        "ffmpeg_params": ffmpeg_params,
    }
    if codec == "libx264":
        write_kwargs["threads"] = 4

    video.write_videofile(str(output_path), **write_kwargs)
    logger.info("Wrote Short (%s, %.1fs): %s", codec, audio_duration, output_path)

    video.close()
    audio.close()
    return str(output_path)
