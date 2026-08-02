"""Shorts narration — roundup of N news stories → Groq / template segments."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from src.config import Section, get_env, load_pipeline_config
from src.naming import sanitize_news_title

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WORDS_PER_SEC = 2.2

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_SOURCE_TAIL_RE = re.compile(
    r"(?:\s*[\|\-–—]\s*|\s{2,})"
    r"[A-Za-z0-9][A-Za-z0-9 .,&/'!]{0,50}$"
)
_ATTR_CRUMB_RE = re.compile(
    r"""\b(?:href|src|target|oc|rel)\s*=\s*["']?[^"'>\s]*["']?""",
    re.IGNORECASE,
)
_OPENERS = (
    "First up,",
    "Next,",
    "Also,",
    "And this,",
    "Finally,",
)


def _strip_html(text: str) -> str:
    cleaned = html.unescape(text or "")
    cleaned = cleaned.replace("\xa0", " ").replace("\u200b", " ")
    cleaned = re.sub(r"<[^>]*>", " ", cleaned)
    cleaned = re.sub(r"</?[a-zA-Z][^>]*>?", " ", cleaned)
    cleaned = _ATTR_CRUMB_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[<>]", " ", cleaned)
    return cleaned.strip()


def _clean_for_speech(text: str) -> str:
    cleaned = _strip_html(text)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\bnbsp\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"&[#a-zA-Z0-9]+;", " ", cleaned)
    cleaned = re.sub(r"[^\S\r\n]+", " ", cleaned)
    cleaned = re.sub(
        r"\s+-\s+[A-Za-z0-9][A-Za-z0-9 .,&/'!]{0,50}$",
        "",
        cleaned,
    ).strip()
    cleaned = re.sub(
        r"\s+\|\s+[A-Za-z0-9][A-Za-z0-9 .,&/'!]{0,50}$",
        "",
        cleaned,
    ).strip()
    for _ in range(2):
        cleaned = _SOURCE_TAIL_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:-\"'")
    return cleaned.strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _max_words() -> int:
    config = load_pipeline_config()
    max_sec = int(config.get("max_video_duration_sec", 58))
    target = int(config.get("target_duration_sec", 45))
    budget = min(max_sec, target)
    return max(50, int(budget * WORDS_PER_SEC))


def _join_segments(segments: dict[str, Any]) -> str:
    parts = [segments.get("intro", "")]
    parts.extend(segments.get("trends") or [])
    parts.append(segments.get("outro", ""))
    return " ".join(p.strip() for p in parts if p and str(p).strip())


def _headline(item: dict[str, Any]) -> str:
    return sanitize_news_title(_clean_for_speech(item.get("title", "")), max_len=120)


def _fact(item: dict[str, Any]) -> str:
    headline = _headline(item)
    summary = _clean_for_speech(item.get("summary", ""))[:180]
    if summary and len(summary) > 20 and summary.lower() != headline.lower():
        return summary
    return headline


def _template_segments(
    section: Section,
    news_items: list[dict[str, Any]],
) -> dict[str, Any]:
    config = load_pipeline_config()
    script_cfg = config.get("script", {})
    n = len(news_items)
    intro = _clean_for_speech(
        script_cfg.get(
            "intro_template",
            "Here are today's top {n} {section} stories.",
        ).format(section=section.name, n=n)
    )
    outro = _clean_for_speech(
        script_cfg.get(
            "outro_template",
            "Thanks for watching. Follow for more news Shorts.",
        )
    )
    beats: list[str] = []
    keywords: list[str] = []
    for idx, item in enumerate(news_items):
        opener = _OPENERS[min(idx, len(_OPENERS) - 1)]
        fact = _fact(item)
        beat = f"{opener} {fact}"
        if beat[-1] not in ".!?":
            beat += "."
        beats.append(_clean_for_speech(beat))
        keywords.append(_headline(item))
    return {
        "intro": intro,
        "trends": beats,
        "outro": outro,
        "trend_keywords": keywords,
    }


def _build_prompt(
    section: Section,
    news_items: list[dict[str, Any]],
    max_words: int,
) -> str:
    style = load_pipeline_config().get("script", {}).get(
        "style",
        "punchy YouTube Shorts news host; no invented facts",
    )
    lines = [
        f"Write spoken narration for ONE vertical YouTube Short covering the top {len(news_items)} {section.name} news stories.",
        f"Style: {style}",
        f"Hard limit: under {max_words} words total.",
        "Only use the facts below. Do not invent details.",
        "Rules:",
        "- Short intro naming the section and how many stories.",
        f"- Exactly {len(news_items)} body beats — one per story, in order.",
        "- Short CTA outro.",
        "- Never include URLs or markdown.",
        "",
        "Return ONLY valid JSON:",
        '{ "intro": "...", "trends": ["beat1", "beat2", ...], "outro": "..." }',
        f"The trends array MUST have exactly {len(news_items)} strings.",
        "",
        "Stories:",
    ]
    for idx, item in enumerate(news_items, start=1):
        lines.append(f"{idx}. headline={_headline(item)!r} fact={_fact(item)!r}")
    return "\n".join(lines)


def _call_groq(prompt: str, model: str, api_key: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0.6,
        "max_tokens": 1200,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write punchy YouTube Shorts news roundup narration. "
                    "Stay factual. Never invent events. "
                    "Reply with JSON only — no markdown fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(GROQ_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:\w+)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse_groq_segments(
    raw: str,
    section: Section,
    news_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        payload = json.loads(match.group(0) if match else raw)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

    intro = _clean_for_speech(str(payload.get("intro") or ""))
    outro = _clean_for_speech(str(payload.get("outro") or ""))
    raw_trends = payload.get("trends")
    if not isinstance(raw_trends, list):
        return None
    beats = [_clean_for_speech(str(b)) for b in raw_trends if str(b).strip()]
    if not beats:
        return None
    # Align to news count
    while len(beats) < len(news_items):
        item = news_items[len(beats)]
        beats.append(_clean_for_speech(f"Also in the news: {_headline(item)}."))
    beats = beats[: len(news_items)]
    if not intro:
        intro = f"Here are today's top {len(news_items)} {section.name} stories."
    if not outro:
        outro = "Thanks for watching. Follow for more news Shorts."
    return {
        "intro": intro,
        "trends": beats,
        "outro": outro,
        "trend_keywords": [_headline(i) for i in news_items],
    }


def generate_script(
    section: Section,
    news_items: list[dict[str, Any]] | dict[str, Any],
    output_dir: Path,
) -> str:
    if isinstance(news_items, dict):
        news_items = [news_items]
    if not news_items:
        raise ValueError("No news items for script")

    config = load_pipeline_config()
    script_cfg = config.get("script", {})
    provider = str(script_cfg.get("provider", "groq")).lower()
    model = str(script_cfg.get("model", "llama-3.1-8b-instant"))
    api_key = get_env("GROQ_API_KEY")
    max_words = _max_words()

    used_provider = "template"
    segments: dict[str, Any] | None = None

    if provider == "groq" and api_key:
        try:
            prompt = _build_prompt(section, news_items, max_words)
            raw = _call_groq(prompt, model, api_key)
            segments = _parse_groq_segments(raw, section, news_items)
            if segments is None:
                raise ValueError("Could not parse Groq JSON segments")
            used_provider = "groq"
        except Exception as exc:
            logger.warning("Groq narration failed, using template: %s", exc)
            segments = None

    if segments is None:
        used_provider = "template"
        segments = _template_segments(section, news_items)

    script = _join_segments(segments)
    if _word_count(script) > max_words:
        # Trim last beats first
        while _word_count(script) > max_words and len(segments["trends"]) > 1:
            segments["trends"] = segments["trends"][:-1]
            segments["trend_keywords"] = segments["trend_keywords"][
                : len(segments["trends"])
            ]
            script = _join_segments(segments)

    script_path = output_dir / "script.txt"
    script_path.write_text(script, encoding="utf-8")
    (output_dir / "script_segments.json").write_text(
        json.dumps(
            {
                "intro": segments["intro"],
                "trends": segments["trends"],
                "outro": segments["outro"],
                "trend_keywords": segments["trend_keywords"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "script_meta.json").write_text(
        json.dumps(
            {
                "provider": used_provider,
                "model": model if used_provider == "groq" else None,
                "word_count": _word_count(script),
                "max_words": max_words,
                "stories": len(segments["trends"]),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Short script via %s (%s words, %s stories)",
        used_provider,
        _word_count(script),
        len(segments["trends"]),
    )
    return str(script_path)
