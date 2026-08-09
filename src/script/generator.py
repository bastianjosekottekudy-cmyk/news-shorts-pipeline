"""Shorts narration — roundup of N news stories → LLM chain / template segments."""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Any

from src.config import Section, load_pipeline_config
from src.llm.chain import TEMPLATE_SENTINEL, get_llm_chain
from src.naming import sanitize_news_title

logger = logging.getLogger(__name__)

WORDS_PER_SEC = 2.3
_NARRATION_SYSTEM = (
    "You write natural spoken English for YouTube Shorts news narration. "
    "Write like a human host talking to camera — complete sentences, natural pacing. "
    "Viewers already see each headline on screen — explain the story; "
    "do not re-read captions. Never invent events. Never name news outlets. "
    "Never include URLs, website names, or domains (nothing with .com, .net, .org, etc.). "
    "Keep one spoken beat per story, in order. "
    "Reply with JSON only — no markdown fences."
)

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# Bare domains TTS would read as "dot com" (CNBC.com, example.co.uk, …).
_DOMAIN_RE = re.compile(
    r"\b(?:[\w-]+\.)+(?:com|net|org|io|co|info|tv|news|ai|app|dev|me|uk|us|in|cy)"
    r"(?:\.[a-z]{2,3})?\b",
    re.IGNORECASE,
)
_DOMAIN_TAIL_RE = re.compile(
    r"(?:\s+[\|\-–—]\s+|\s{2,})"
    r"[A-Za-z0-9][A-Za-z0-9 .,&/'!]{0,40}\.(?:com|net|org|io)\b\.?$",
    re.IGNORECASE,
)
# Publisher tails like " - BBC" / " | Reuters" — MUST have whitespace around
# the delimiter so we never chop hyphenated words (anti-government → anti).
_SOURCE_TAIL_RE = re.compile(
    r"(?:\s+[\|\-–—]\s+|\s{2,})"
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
    "Meanwhile,",
    "Finally,",
)

# Common outlet / wire names that should never be spoken.
_OUTLET_NAMES = (
    "BBC",
    "Reuters",
    "Associated Press",
    "AP News",
    "AP",
    "CNN",
    "CNBC",
    "NPR",
    "Fox News",
    "The Guardian",
    "Guardian",
    "The New York Times",
    "New York Times",
    "NYTimes",
    "Washington Post",
    "WSJ",
    "Wall Street Journal",
    "Bloomberg",
    "Forbes",
    "TechCrunch",
    "The Verge",
    "Engadget",
    "Wired",
    "PC Gamer",
    "IGN",
    "Polygon",
    "Kotaku",
    "Eurogamer",
    "Sky News",
    "ITV",
    "Channel 4",
    "Al Jazeera",
    "Times of India",
    "Hindustan Times",
    "NDTV",
    "India Today",
    "The Hindu",
    "Economic Times",
    "Cyprus Mail",
    "Financial Times",
    "Yahoo News",
    "MSN",
    "Google News",
    "Aftermath",
    "Ynetnews",
    "Nintendo World Report",
)
_OUTLET_ALT = "|".join(re.escape(n) for n in sorted(_OUTLET_NAMES, key=len, reverse=True))
_OUTLET_TAIL_RE = re.compile(
    rf"(?:\s+[\|\-–—]\s+|\s{{2,}})(?:{_OUTLET_ALT})\.?$",
    re.IGNORECASE,
)
_OUTLET_PHRASE_RE = re.compile(
    rf"\b(?:according to|as reported by|reports?(?:\s+from)?|via|sourced from|"
    rf"per|says?)\s+(?:the\s+)?(?:{_OUTLET_ALT})\b\.?",
    re.IGNORECASE,
)
_BARE_OUTLET_RE = re.compile(
    rf"(?:^|[\s,;:(])(?:the\s+)?(?:{_OUTLET_ALT})(?=$|[\s,;:.)])",
    re.IGNORECASE,
)
_CONTINUE_RE = re.compile(
    r"\b(?:continue reading|read more|full story|click here)\b.*$",
    re.IGNORECASE,
)


def _strip_html(text: str) -> str:
    cleaned = html.unescape(text or "")
    cleaned = cleaned.replace("\xa0", " ").replace("\u200b", " ")
    cleaned = re.sub(r"<[^>]*>", " ", cleaned)
    cleaned = re.sub(r"</?[a-zA-Z][^>]*>?", " ", cleaned)
    cleaned = _ATTR_CRUMB_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[<>]", " ", cleaned)
    return cleaned.strip()


def _strip_sources(text: str) -> str:
    cleaned = text or ""
    cleaned = _CONTINUE_RE.sub("", cleaned)
    for _ in range(3):
        prev = cleaned
        cleaned = _OUTLET_TAIL_RE.sub("", cleaned).strip()
        cleaned = _SOURCE_TAIL_RE.sub("", cleaned).strip()
        cleaned = _OUTLET_PHRASE_RE.sub("", cleaned).strip()
        if cleaned == prev:
            break
    # Drop leftover bare outlet tokens (e.g. mid-sentence "BBC said" remnants)
    cleaned = _BARE_OUTLET_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:\"'")
    return cleaned.strip()


def _domain_to_speech(match: re.Match[str]) -> str:
    """Turn Apple.com → Apple; drop longer hostnames so TTS never says 'dot com'."""
    labels = match.group(0).split(".")
    if len(labels) == 2:
        return labels[0]
    if len(labels) == 3 and labels[0].lower() == "www":
        return labels[1]
    return " "


def _strip_domains(text: str) -> str:
    cleaned = text or ""
    for _ in range(2):
        cleaned = _DOMAIN_TAIL_RE.sub("", cleaned).strip()
    cleaned = _DOMAIN_RE.sub(_domain_to_speech, cleaned)
    # Leftover "dot com" / ". com" spoken artifacts
    cleaned = re.sub(r"\bdot\s+com\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\.\s*com\b", " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def _ensure_sentence(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return cleaned
    # Prefer a normal spoken sentence ending.
    cleaned = cleaned.rstrip(" ,;:")
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _clean_for_speech(text: str) -> str:
    cleaned = _strip_html(text)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _strip_domains(cleaned)
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
        cleaned = _DOMAIN_TAIL_RE.sub("", cleaned).strip()
    cleaned = _strip_sources(cleaned)
    cleaned = _strip_domains(cleaned)
    cleaned = cleaned.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:\"'")
    return cleaned.strip()


def _normalize_compare(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", (text or "").lower())


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _max_words() -> int:
    config = load_pipeline_config()
    max_sec = int(config.get("max_video_duration_sec", 90))
    target = int(config.get("target_duration_sec", 70))
    # Prefer the larger budget — Shorts length is flexible for clearer narration.
    budget = max(max_sec, target)
    return max(110, int(budget * WORDS_PER_SEC))


def _join_segments(segments: dict[str, Any]) -> str:
    parts = [segments.get("intro", "")]
    parts.extend(segments.get("trends") or [])
    parts.append(segments.get("outro", ""))
    return " ".join(p.strip() for p in parts if p and str(p).strip())


def _headline(item: dict[str, Any]) -> str:
    return sanitize_news_title(_clean_for_speech(item.get("title", "")), max_len=120)


def _fact(item: dict[str, Any]) -> str:
    """Extra context for the model — never prefer a near-duplicate of the headline."""
    headline = _headline(item)
    summary = _clean_for_speech(item.get("summary", ""))[:360]
    if not summary or len(summary) < 25:
        return ""
    hn = _normalize_compare(headline)
    sn = _normalize_compare(summary)
    if not hn:
        return summary
    if sn == hn or sn.startswith(hn) or hn in sn[: max(len(hn) + 20, 80)]:
        # Summary mostly restates the headline — drop it so we don't echo twice.
        remainder = summary
        for sep in (". ", " — ", " - ", ": "):
            if sep in summary:
                parts = summary.split(sep, 1)
                if _normalize_compare(parts[0]) == hn or hn.startswith(
                    _normalize_compare(parts[0])
                ):
                    remainder = parts[1].strip()
                    break
        if _normalize_compare(remainder) == hn or len(remainder) < 25:
            return ""
        return remainder
    return summary


def _remove_headline_echo(beat: str, headline: str) -> str:
    """
    If narration restates the on-screen headline, keep only the added meaning.
    Viewer already sees the caption — speech should explain, not re-read.
    """
    cleaned = _clean_for_speech(beat)
    if not cleaned:
        return cleaned
    hn = _normalize_compare(headline)
    if not hn or len(hn) < 12:
        return cleaned

    # Strip opener then check for headline prefix
    body = re.sub(
        r"^(?:first up|next|also|meanwhile|finally|and this)[,:]?\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    bn = _normalize_compare(body)

    if bn == hn:
        return ""

    # Exact headline prefix before punctuation
    pattern = re.compile(
        re.escape(headline.strip().rstrip(".")),
        re.IGNORECASE,
    )
    body2 = pattern.sub(" ", body, count=1)
    body2 = re.sub(r"\s+", " ", body2).strip(" .,;:-")
    if body2 and _normalize_compare(body2) != hn and len(body2) >= 20:
        return body2

    # Fuzzy: if first ~80% of words match headline words in order, drop that span
    h_words = hn.split()
    b_words = bn.split()
    if len(h_words) >= 4 and len(b_words) >= len(h_words):
        match_n = 0
        for hw, bw in zip(h_words, b_words):
            if hw == bw:
                match_n += 1
            else:
                break
        if match_n >= max(4, int(len(h_words) * 0.7)):
            rest_words = body.split()[match_n:]
            rest = " ".join(rest_words).strip(" .,;:-")
            if len(rest) >= 20:
                return rest
    return cleaned


def _finalize_beat(raw_beat: str, headline: str, opener: str) -> str:
    beat = _clean_for_speech(raw_beat)
    beat = _remove_headline_echo(beat, headline)
    if not beat or len(beat) < 18:
        # Meaningful fallback without outlet noise: speak a clean one-liner once.
        beat = _clean_for_speech(headline)
    # Ensure a single natural opener (don't double "Next, Next,")
    low = beat.lower()
    if not any(
        low.startswith(p)
        for p in ("first up", "next", "also", "meanwhile", "finally", "and this")
    ):
        if re.match(r"^(A|An|The)\b", beat):
            beat = beat[0].lower() + beat[1:]
        beat = f"{opener} {beat}"
    beat = _clean_for_speech(beat)
    return _ensure_sentence(beat)


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
        headline = _headline(item)
        fact = _fact(item)
        if fact:
            raw = fact
        else:
            raw = headline
        beats.append(_finalize_beat(raw, headline, opener))
        keywords.append(headline)
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
        "natural English news host; explain each story clearly; never invent facts",
    )
    lines = [
        f"Write spoken narration in English only for ONE vertical YouTube Short covering {len(news_items)} {section.name} stories.",
        f"Style: {style}",
        f"Soft limit: about {max_words} words total (a little longer is OK if needed for clarity).",
        "Only use the facts below. Do not invent details.",
        "",
        "CRITICAL sync rules:",
        f"- Return exactly {len(news_items)} body beats, one per story, in the same order as listed.",
        "- Each on-screen caption already shows that story's headline. Do NOT read the headline word-for-word.",
        "- Each beat must be 1–2 natural spoken sentences (subject + verb), explaining what happened and why it matters.",
        "- Sound like a person talking, not a headline paste or RSS blurb.",
        "- If the fact is empty, lightly rephrase the headline into natural speech — still do not paste it verbatim if you can avoid it.",
        "- Never mention publishers, outlets, or sources (no BBC, Reuters, CNN, 'according to…', etc.).",
        "- Never include URLs, domains, or site names (no .com / .net / www).",
        "- No hashtags or markdown.",
        "- Short intro naming the section and story count; short CTA outro.",
        "",
        "Return ONLY valid JSON:",
        '{ "intro": "...", "trends": ["beat1", "beat2", ...], "outro": "..." }',
        f"The trends array MUST have exactly {len(news_items)} strings.",
        "",
        "Stories (on_screen_caption is already visible to the viewer):",
    ]
    for idx, item in enumerate(news_items, start=1):
        lines.append(
            f"{idx}. on_screen_caption={_headline(item)!r} "
            f"extra_fact={_fact(item) or '(none — rephrase caption into natural speech)'!r}"
        )
    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:\w+)?\n?", "", cleaned)
    cleaned = re.sub(r"\n?```$", "", cleaned)
    return cleaned.strip()


def _parse_narration_segments(
    raw: str,
    section: Section,
    news_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        payload = json.loads(match.group(0) if match else raw)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

    intro = _ensure_sentence(_clean_for_speech(str(payload.get("intro") or "")))
    outro = _ensure_sentence(_clean_for_speech(str(payload.get("outro") or "")))
    raw_trends = payload.get("trends")
    if not isinstance(raw_trends, list):
        return None
    raw_beats = [str(b) for b in raw_trends if str(b).strip()]
    if not raw_beats:
        return None

    # Always exactly one beat per on-screen story (keeps slides + audio in sync).
    beats: list[str] = []
    keywords: list[str] = []
    for idx, item in enumerate(news_items):
        opener = _OPENERS[min(idx, len(_OPENERS) - 1)]
        headline = _headline(item)
        keywords.append(headline)
        if idx < len(raw_beats):
            beats.append(_finalize_beat(raw_beats[idx], headline, opener))
        else:
            fallback = _fact(item) or headline
            beats.append(_finalize_beat(fallback, headline, opener))

    if not intro:
        intro = f"Here are today's top {len(news_items)} {section.name} stories."
    if not outro:
        outro = "Thanks for watching. Follow for more news Shorts."
    return {
        "intro": intro,
        "trends": beats,
        "outro": outro,
        "trend_keywords": keywords,
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
    provider = str(script_cfg.get("provider", "chain")).lower()
    yaml_model = str(script_cfg.get("model", "llama-3.1-8b-instant"))
    max_words = _max_words()

    used_provider = "template"
    used_endpoint: str | None = None
    last_error: str | None = None
    segments: dict[str, Any] | None = None

    if provider != "template":
        try:
            chain = get_llm_chain(yaml_model=yaml_model)
            prompt = _build_prompt(section, news_items, max_words)
            raw = chain.complete(_NARRATION_SYSTEM, prompt)
            used_endpoint = chain.last_endpoint
            last_error = chain.last_error
            if raw != TEMPLATE_SENTINEL:
                raw = _strip_fences(raw)
                segments = _parse_narration_segments(raw, section, news_items)
                if segments is None:
                    raise ValueError("Could not parse narration JSON segments")
                used_provider = used_endpoint or "chain"
            else:
                segments = None
        except Exception as exc:
            logger.warning("LLM narration chain failed, using template: %s", exc)
            last_error = str(exc)[:300]
            segments = None

    if segments is None:
        used_provider = "template"
        used_endpoint = "template"
        segments = _template_segments(section, news_items)

    # Never drop story beats — that desyncs narration from on-screen news.
    # Soft-trim only the outro if we somehow blow past a hard ceiling.
    hard_ceiling = max_words + 40
    script = _join_segments(segments)
    if _word_count(script) > hard_ceiling and segments.get("outro"):
        segments["outro"] = "Thanks for watching."
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
                "endpoint": used_endpoint,
                "model": yaml_model if used_provider != "template" else None,
                "last_error": last_error,
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
        used_endpoint or used_provider,
        _word_count(script),
        len(segments["trends"]),
    )
    return str(script_path)
