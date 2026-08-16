"""On-screen titles and LLM-clarified news headlines."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.config import Section
from src.llm.chain import get_llm_chain
from src.naming import build_video_title, sanitize_news_title

logger = logging.getLogger(__name__)

_TITLE_SYSTEM = (
    "You rewrite news headlines into clear, short English titles for YouTube Shorts. "
    "Stay faithful to the given facts. Do not invent events, names, or numbers. "
    "Return JSON only."
)


def generate_display_title(
    section_name: str,
    run_date: str,
    output_dir: Path,
    *,
    story_count: int = 1,
) -> dict[str, str]:
    """Return {title, subtitle} for overlays; write display_titles.json."""
    title = build_video_title(section_name, run_date)
    if story_count > 1:
        subtitle = f"{story_count} top stories"
    else:
        subtitle = f"{section_name} · News Short"
    card = {"title": title, "subtitle": subtitle}
    (output_dir / "display_titles.json").write_text(
        json.dumps(card, indent=2), encoding="utf-8"
    )
    return card


def story_card(news_title: str, section_name: str, rank: int) -> dict[str, str]:
    cleaned = fallback_news_title(news_title)
    return {
        "title": cleaned,
        "subtitle": f"{section_name} · #{rank}",
    }


def fallback_news_title(raw: str) -> str:
    """Local cleanup used when LLM titles are unavailable (same as story_card)."""
    from src.script.generator import _clean_for_speech

    return sanitize_news_title(_clean_for_speech(raw or ""), max_len=90)


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _usable_llm_title(text: str) -> str | None:
    from src.script.generator import _clean_for_speech

    cleaned = sanitize_news_title(_clean_for_speech(str(text or "")), max_len=90)
    if len(cleaned) < 8 or cleaned.lower() in {"news", "untitled", "title"}:
        return None
    return cleaned


def _parse_title_payload(raw: str, n: int) -> list[str | None] | None:
    """Parse model JSON into n slots (None = missing)."""
    text = _strip_fences(raw)
    try:
        match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
        payload = json.loads(match.group(0) if match else text)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

    slots: list[str | None] = [None] * n

    if isinstance(payload, dict):
        titles = payload.get("titles")
        if isinstance(titles, list):
            for i, entry in enumerate(titles):
                if i >= n:
                    break
                if isinstance(entry, dict):
                    slots[i] = _usable_llm_title(
                        str(entry.get("title") or entry.get("headline") or "")
                    )
                else:
                    slots[i] = _usable_llm_title(str(entry))
            if any(slots):
                return slots
        # { "1": "...", "2": "..." } or index keys
        for key, val in payload.items():
            if key in ("titles", "stories"):
                continue
            try:
                idx = int(str(key)) - 1
            except ValueError:
                continue
            if 0 <= idx < n:
                if isinstance(val, dict):
                    slots[idx] = _usable_llm_title(
                        str(val.get("title") or val.get("headline") or "")
                    )
                else:
                    slots[idx] = _usable_llm_title(str(val))
        if any(slots):
            return slots
        return None

    if isinstance(payload, list):
        for i, entry in enumerate(payload):
            if isinstance(entry, dict):
                idx_raw = entry.get("index", entry.get("i", i + 1))
                try:
                    idx = int(idx_raw) - 1
                except (TypeError, ValueError):
                    idx = i
                title = entry.get("title") or entry.get("headline") or ""
                if 0 <= idx < n:
                    slots[idx] = _usable_llm_title(str(title))
            elif i < n:
                slots[i] = _usable_llm_title(str(entry))
        if any(slots):
            return slots
    return None


def _full_titles_prompt(section: Section, news_items: list[dict[str, Any]]) -> str:
    n = len(news_items)
    lines = [
        f"Rewrite headlines for a {section.name} YouTube Shorts news roundup.",
        f'Return ONLY JSON: {{ "titles": ["title1", ...] }} with exactly {n} strings.',
        "Each title: clear English, concise (about 90 characters max), no hashtags,",
        "no emojis, no publisher/outlet suffix, no URLs or .com domains.",
        "Stay faithful to each story — do not invent facts.",
        "",
        "Stories:",
    ]
    for i, item in enumerate(news_items, start=1):
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if len(summary) > 240:
            summary = summary[:237].rsplit(" ", 1)[0] + "…"
        lines.append(f"{i}. title={title!r}")
        if summary:
            lines.append(f"   summary={summary!r}")
    return "\n".join(lines)


def _gap_titles_prompt(
    section: Section,
    news_items: list[dict[str, Any]],
    missing_indices: list[int],
    existing: list[str | None],
) -> str:
    lines = [
        f"Fill missing headlines for a {section.name} YouTube Shorts news roundup.",
        f'Return ONLY JSON: {{ "titles": ["title1", ...] }} with exactly '
        f"{len(missing_indices)} strings, in the same order as the MISSING list.",
        "Each title: clear English, concise (~90 chars), no hashtags/emojis/outlets/URLs.",
        "Stay faithful — do not invent facts. Do not rewrite DONE titles.",
        "",
        "Status:",
    ]
    for idx, item in enumerate(news_items):
        raw = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if len(summary) > 160:
            summary = summary[:157].rsplit(" ", 1)[0] + "…"
        if existing[idx]:
            lines.append(f"{idx + 1}. DONE title={existing[idx]!r}")
        else:
            lines.append(f"{idx + 1}. MISSING raw={raw!r}")
            if summary:
                lines.append(f"   summary={summary!r}")
    lines.append("")
    lines.append(
        "Missing indices (1-based, write titles only for these, in order): "
        + ", ".join(str(i + 1) for i in missing_indices)
    )
    return "\n".join(lines)


def clarify_news_titles(
    section: Section,
    news_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Overwrite each item's ``title`` with an LLM-clarified headline.
    Missing / failed titles fall back to sanitize_news_title cleaning.
    """
    if not news_items:
        return news_items

    n = len(news_items)
    raw_titles = [str(item.get("title") or "") for item in news_items]
    filled: list[str | None] = [None] * n
    chain = get_llm_chain()
    endpoints = chain.cloud_endpoints()

    for i, endpoint in enumerate(endpoints):
        missing = [idx for idx, t in enumerate(filled) if not t]
        if not missing:
            break
        if i == 0 or len(missing) == n:
            system, user = _TITLE_SYSTEM, _full_titles_prompt(section, news_items)
            logger.info(
                "News titles full via %s (missing %s/%s)",
                endpoint.label,
                len(missing),
                n,
            )
        else:
            system, user = (
                _TITLE_SYSTEM,
                _gap_titles_prompt(section, news_items, missing, filled),
            )
            logger.info(
                "News titles gap-fill via %s (missing %s/%s)",
                endpoint.label,
                len(missing),
                n,
            )

        text = chain.try_complete(
            endpoint, system, user, temperature=0.35, max_tokens=1200
        )
        if not text:
            continue

        if i == 0 or len(missing) == n:
            parsed = _parse_title_payload(text, n)
            if not parsed:
                logger.warning(
                    "Unusable news-title JSON from %s — keep going", endpoint.label
                )
                continue
            added = 0
            for idx, title in enumerate(parsed):
                if title and not filled[idx]:
                    filled[idx] = title
                    added += 1
            if added:
                logger.info(
                    "Kept %s title(s) from %s (%s/%s filled)",
                    added,
                    endpoint.label,
                    sum(1 for t in filled if t),
                    n,
                )
        else:
            parsed = _parse_title_payload(text, len(missing))
            if not parsed:
                # Also try mapping onto full-length array by index
                parsed_full = _parse_title_payload(text, n)
                if parsed_full:
                    added = 0
                    for idx in missing:
                        if parsed_full[idx] and not filled[idx]:
                            filled[idx] = parsed_full[idx]
                            added += 1
                    if added:
                        logger.info(
                            "Gap-filled %s title(s) from %s (%s/%s filled)",
                            added,
                            endpoint.label,
                            sum(1 for t in filled if t),
                            n,
                        )
                    continue
                logger.warning(
                    "Unusable news-title JSON from %s — keep going", endpoint.label
                )
                continue
            added = 0
            for slot, idx in enumerate(missing):
                title = parsed[slot] if slot < len(parsed) else None
                if title and not filled[idx]:
                    filled[idx] = title
                    added += 1
            if added:
                logger.info(
                    "Gap-filled %s title(s) from %s (%s/%s filled)",
                    added,
                    endpoint.label,
                    sum(1 for t in filled if t),
                    n,
                )

    remaining = sum(1 for t in filled if not t)
    if remaining == n:
        logger.info("All LLM news titles failed — sanitizing every story")
    elif remaining:
        logger.info("Sanitize-padding %s remaining news title(s) after LLM chain", remaining)

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(news_items):
        updated = dict(item)
        updated["title"] = filled[idx] or fallback_news_title(raw_titles[idx])
        out.append(updated)
    return out
