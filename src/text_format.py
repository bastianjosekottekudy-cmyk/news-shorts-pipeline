"""Clean, display-ready titles for trends and news overlays."""

from __future__ import annotations

import html
import re

# Common publisher suffixes in Google News RSS titles
_PUBLISHER_SPLIT = re.compile(
    r"\s+[-–—|]\s+(?:BBC|CNN|Reuters|AP|Associated Press|Fox News|NBC|CBS|ABC|"
    r"The Guardian|NYTimes|New York Times|Washington Post|Bloomberg|Forbes|"
    r"TechCrunch|The Verge|Sky News|Al Jazeera|NDTV|Times of India|Hindustan Times|"
    r"India Today|The Hindu|BBC News|CNBC|USA Today|WSJ|Wall Street Journal|"
    r"Yahoo|MSN|People|ESPN|Variety|Deadline|Politico|Axios).*$",
    re.IGNORECASE,
)

_MULTI_SPACE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return _MULTI_SPACE.sub(" ", text).strip()


def format_trend_title(keyword: str) -> str:
    """
    Turn raw search terms into a clean on-screen headline.
    e.g. 'angel city fc vs kc current' → 'Angel City FC vs KC Current'
    """
    text = strip_html(keyword)
    if not text:
        return "Trending Now"

    # Preserve short acronyms / FC / vs
    small = {"vs", "v", "and", "or", "of", "the", "a", "an", "in", "on", "at", "to", "for"}
    acronyms = {
        "fc", "nfl", "nba", "mlb", "nhl", "ufc", "ai", "uk", "us", "usa", "eu", "un",
        "kc", "espn", "bbc", "cnn", "ceo", "gpt", "ios", "tv", "uae",
    }

    words = re.split(r"(\s+)", text)
    out: list[str] = []
    for i, part in enumerate(words):
        if not part or part.isspace():
            out.append(part)
            continue
        lower = part.lower()
        if lower in acronyms:
            out.append(lower.upper())
        elif i > 0 and lower in small:
            out.append(lower)
        elif part.isupper() and len(part) <= 4:
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower() if len(part) > 1 else part.upper())
    cleaned = "".join(out)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    # Soft cap for on-screen readability
    if len(cleaned) > 72:
        cleaned = cleaned[:69].rsplit(" ", 1)[0] + "…"
    return cleaned


def format_news_headline(title: str, max_len: int = 110) -> str:
    """Strip HTML/publisher tails and trim for subtitle overlay."""
    text = strip_html(title)
    if not text:
        return "Trending in the news"
    text = _PUBLISHER_SPLIT.sub("", text).strip()
    # Also drop trailing " - Source" patterns generically
    text = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,40}$", "", text).strip()
    text = _MULTI_SPACE.sub(" ", text)
    if len(text) > max_len:
        text = text[: max_len - 1].rsplit(" ", 1)[0] + "…"
    return text or "Trending in the news"
