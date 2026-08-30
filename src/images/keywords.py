"""LLM + heuristic image search keywords per news story."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from src.config import Section, load_pipeline_config
from src.llm.chain import get_llm_chain

logger = logging.getLogger(__name__)

_KW_SYSTEM = (
    "You extract visual image-search keywords for YouTube Shorts news slides. "
    "Prefer concrete nouns: people, places, products, games, hardware. "
    "No outlets, URLs, hashtags, or invented facts. Return JSON only."
)

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "this", "that", "with", "from", "why", "how", "what", "when", "after",
    "into", "its", "it", "as", "at", "by", "be", "was", "were", "has", "have",
    "new", "now", "just", "about", "over", "under", "up", "down",
}

_WEAK_SOLO = {
    "column", "feature", "top", "news", "update", "report", "breaking",
    "live", "video", "watch", "story", "latest", "today", "week",
}

# Trailing publisher only — whitespace around delimiter keeps X|S.
# Suffix must be short (outlet-like), not a long clause after |.
_TRAILING_OUTLET_RE = re.compile(
    r"\s+[\|\-–—]\s+([A-Za-z0-9][A-Za-z0-9 .,&/'!]{0,40})$"
)


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _plain_summary(text: str, max_len: int = 240) -> str:
    cleaned = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return cleaned


def _strip_trailing_outlet(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "").strip())
    for _ in range(3):
        match = _TRAILING_OUTLET_RE.search(cleaned)
        if not match:
            break
        suffix = match.group(1).strip()
        if len(suffix.split()) > 3:
            break
        cleaned = cleaned[: match.start()].strip()
    return cleaned or (title or "").strip() or "news"


def heuristic_image_queries(
    title: str,
    *,
    max_queries: int = 3,
) -> tuple[str, list[str]]:
    """
    Local fallback keywords from a headline.
    Returns (primary_entity, queries).
    """
    cleaned = _strip_trailing_outlet(title)
    # Keep letters/digits and internal | (X|S) by normalizing | between alnum.
    cleaned = re.sub(r"(?<=\w)\|(?=\w)", "", cleaned)
    words = [
        w for w in re.findall(r"[A-Za-z0-9]{2,}", cleaned)
        if w.lower() not in _STOP
    ]
    # Drop leading weak tokens (Column, Feature, Top…) when more content follows.
    while len(words) > 1 and words[0].lower() in _WEAK_SOLO:
        words = words[1:]
    queries: list[str] = []
    if len(words) >= 2:
        chunk = " ".join(words[:4])
        queries.append(chunk)
    # Prefer capitalized / longer tokens as entity
    ranked = sorted(words, key=lambda w: (w[:1].isupper(), len(w)), reverse=True)
    primary = ""
    for w in ranked:
        if w.lower() not in _WEAK_SOLO:
            primary = w
            break
    if not primary and words:
        primary = words[0]
    if primary and primary.lower() not in {q.lower() for q in queries}:
        queries.append(primary)
    if len(words) >= 3:
        alt = " ".join(words[1:4])
        if alt.lower() not in {q.lower() for q in queries}:
            queries.append(alt)

    # Drop weak solo-only results
    queries = [
        q for q in queries
        if q.strip() and not (
            len(q.split()) == 1 and q.lower() in _WEAK_SOLO
        )
    ][:max_queries]
    if not queries:
        queries = [cleaned[:48] or "news"]
    if not primary or primary.lower() in _WEAK_SOLO:
        primary = queries[0].split()[0] if queries else "news"
    return primary, queries


def _usable_slot(
    primary: str,
    queries: list[str],
    *,
    max_queries: int,
) -> dict[str, Any] | None:
    cleaned_q: list[str] = []
    seen: set[str] = set()
    for q in queries:
        text = re.sub(r"\s+", " ", str(q or "").strip())
        if len(text) < 2:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_q.append(text[:80])
        if len(cleaned_q) >= max_queries:
            break
    entity = re.sub(r"\s+", " ", str(primary or "").strip())[:80]
    if not cleaned_q:
        return None
    if not entity or entity.lower() in _WEAK_SOLO:
        entity = cleaned_q[0]
    return {"primary_entity": entity, "queries": cleaned_q}


def _parse_keyword_payload(
    raw: str,
    n: int,
    *,
    max_queries: int,
) -> list[dict[str, Any] | None] | None:
    text = _strip_fences(raw)
    try:
        match = re.search(r"[\{\[].*[\}\]]", text, re.DOTALL)
        payload = json.loads(match.group(0) if match else text)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None

    slots: list[dict[str, Any] | None] = [None] * n

    def _from_entry(entry: Any) -> dict[str, Any] | None:
        if isinstance(entry, dict):
            queries = entry.get("queries") or entry.get("keywords") or []
            if isinstance(queries, str):
                queries = [queries]
            if not isinstance(queries, list):
                queries = []
            primary = str(
                entry.get("primary_entity")
                or entry.get("entity")
                or (queries[0] if queries else "")
            )
            return _usable_slot(primary, [str(q) for q in queries], max_queries=max_queries)
        if isinstance(entry, list):
            return _usable_slot(
                str(entry[0]) if entry else "",
                [str(q) for q in entry],
                max_queries=max_queries,
            )
        if isinstance(entry, str):
            return _usable_slot(entry, [entry], max_queries=max_queries)
        return None

    if isinstance(payload, dict):
        stories = payload.get("stories") or payload.get("items")
        if isinstance(stories, list):
            for i, entry in enumerate(stories):
                if isinstance(entry, dict) and (
                    "index" in entry or "i" in entry
                ):
                    try:
                        idx = int(entry.get("index", entry.get("i"))) - 1
                    except (TypeError, ValueError):
                        idx = i
                else:
                    idx = i
                if 0 <= idx < n and not slots[idx]:
                    slots[idx] = _from_entry(entry)
            if any(slots):
                return slots
        # Positional under "queries" as list of lists
        nested = payload.get("queries")
        if isinstance(nested, list) and nested and isinstance(nested[0], (list, dict)):
            for i, entry in enumerate(nested):
                if i < n:
                    slots[i] = _from_entry(entry)
            if any(slots):
                return slots
        return None if not any(slots) else slots

    if isinstance(payload, list):
        for i, entry in enumerate(payload):
            if isinstance(entry, dict) and ("index" in entry or "i" in entry):
                try:
                    idx = int(entry.get("index", entry.get("i"))) - 1
                except (TypeError, ValueError):
                    idx = i
            else:
                idx = i
            if 0 <= idx < n and not slots[idx]:
                slots[idx] = _from_entry(entry)
        if any(slots):
            return slots
    return None


def _full_keywords_prompt(
    section: Section,
    news_items: list[dict[str, Any]],
    *,
    max_queries: int,
) -> str:
    n = len(news_items)
    lines = [
        f"Extract image-search keywords for a {section.name} YouTube Shorts roundup.",
        f'Return ONLY JSON: {{ "stories": [ {{ "index": 1, "primary_entity": "...", '
        f'"queries": ["...", "..."] }}, ... ] }} with exactly {n} stories.',
        f"Each story: primary_entity (best visual subject) + {max_queries} short queries max.",
        "Concrete nouns only. Section-aware. No outlets or URLs.",
        "",
        "Stories:",
    ]
    for i, item in enumerate(news_items, start=1):
        title = str(item.get("title") or "").strip()
        summary = _plain_summary(str(item.get("summary") or ""))
        lines.append(f"{i}. title={title!r}")
        if summary:
            lines.append(f"   summary={summary!r}")
    return "\n".join(lines)


def _gap_keywords_prompt(
    section: Section,
    news_items: list[dict[str, Any]],
    missing_indices: list[int],
    existing: list[dict[str, Any] | None],
    *,
    max_queries: int,
) -> str:
    lines = [
        f"Fill missing image-search keywords for a {section.name} YouTube Shorts roundup.",
        f'Return ONLY JSON: {{ "stories": [ ... ] }} with exactly {len(missing_indices)} '
        f"entries for the MISSING indices only (include index field).",
        f"Each: primary_entity + up to {max_queries} queries. Concrete nouns. No outlets/URLs.",
        "Do not rewrite DONE stories.",
        "",
        "Status:",
    ]
    for idx, item in enumerate(news_items):
        title = str(item.get("title") or "").strip()
        summary = _plain_summary(str(item.get("summary") or ""), max_len=160)
        if existing[idx]:
            lines.append(
                f"{idx + 1}. DONE entity={existing[idx]['primary_entity']!r} "
                f"queries={existing[idx]['queries']!r}"
            )
        else:
            lines.append(f"{idx + 1}. MISSING title={title!r}")
            if summary:
                lines.append(f"   summary={summary!r}")
    lines.append("")
    lines.append(
        "Missing indices (1-based): " + ", ".join(str(i + 1) for i in missing_indices)
    )
    return "\n".join(lines)


def enrich_news_with_image_queries(
    section: Section,
    news_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Attach ``image_queries`` and ``primary_entity`` to each news item.
    Uses LLM chain with title-style gap-fill; heuristic for leftovers.
    """
    if not news_items:
        return news_items

    cfg = load_pipeline_config().get("images") or {}
    max_queries = int(cfg.get("max_queries_per_story", 3))
    use_llm = bool(cfg.get("llm_keywords", True))

    n = len(news_items)
    filled: list[dict[str, Any] | None] = [None] * n

    if use_llm:
        chain = get_llm_chain()
        endpoints = chain.cloud_endpoints()
        for i, endpoint in enumerate(endpoints):
            missing = [idx for idx, slot in enumerate(filled) if not slot]
            if not missing:
                break
            if i == 0 or len(missing) == n:
                system = _KW_SYSTEM
                user = _full_keywords_prompt(
                    section, news_items, max_queries=max_queries
                )
                logger.info(
                    "Image keywords full via %s (missing %s/%s)",
                    endpoint.label,
                    len(missing),
                    n,
                )
            else:
                system = _KW_SYSTEM
                user = _gap_keywords_prompt(
                    section,
                    news_items,
                    missing,
                    filled,
                    max_queries=max_queries,
                )
                logger.info(
                    "Image keywords gap-fill via %s (missing %s/%s)",
                    endpoint.label,
                    len(missing),
                    n,
                )

            text = chain.try_complete(
                endpoint, system, user, temperature=0.3, max_tokens=1200
            )
            if not text:
                continue

            if i == 0 or len(missing) == n:
                parsed = _parse_keyword_payload(text, n, max_queries=max_queries)
                if not parsed:
                    logger.warning(
                        "Unusable image-keyword JSON from %s — keep going",
                        endpoint.label,
                    )
                    continue
                added = 0
                for idx, slot in enumerate(parsed):
                    if slot and not filled[idx]:
                        filled[idx] = slot
                        added += 1
                if added:
                    logger.info(
                        "Kept %s image-keyword set(s) from %s (%s/%s filled)",
                        added,
                        endpoint.label,
                        sum(1 for s in filled if s),
                        n,
                    )
            else:
                parsed = _parse_keyword_payload(
                    text, len(missing), max_queries=max_queries
                )
                if not parsed:
                    parsed_full = _parse_keyword_payload(
                        text, n, max_queries=max_queries
                    )
                    if parsed_full:
                        added = 0
                        for idx in missing:
                            if parsed_full[idx] and not filled[idx]:
                                filled[idx] = parsed_full[idx]
                                added += 1
                        if added:
                            logger.info(
                                "Gap-filled %s image-keyword set(s) from %s (%s/%s filled)",
                                added,
                                endpoint.label,
                                sum(1 for s in filled if s),
                                n,
                            )
                        continue
                    logger.warning(
                        "Unusable image-keyword JSON from %s — keep going",
                        endpoint.label,
                    )
                    continue
                added = 0
                for slot_i, idx in enumerate(missing):
                    slot = parsed[slot_i] if slot_i < len(parsed) else None
                    if slot and not filled[idx]:
                        filled[idx] = slot
                        added += 1
                if added:
                    logger.info(
                        "Gap-filled %s image-keyword set(s) from %s (%s/%s filled)",
                        added,
                        endpoint.label,
                        sum(1 for s in filled if s),
                        n,
                    )

    remaining = sum(1 for s in filled if not s)
    if remaining == n:
        logger.info("All LLM image keywords failed — using heuristic for every story")
    elif remaining:
        logger.info(
            "Heuristic-padding %s remaining image-keyword set(s) after LLM chain",
            remaining,
        )

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(news_items):
        updated = dict(item)
        slot = filled[idx]
        if not slot:
            entity, queries = heuristic_image_queries(
                str(item.get("title") or ""), max_queries=max_queries
            )
            slot = {"primary_entity": entity, "queries": queries}
        updated["primary_entity"] = slot["primary_entity"]
        updated["image_queries"] = list(slot["queries"])
        out.append(updated)
    return out
