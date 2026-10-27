"""Livemint article parser. Each source gets a module like this one."""

import re
from datetime import datetime

from bs4 import BeautifulSoup

ARTICLE_URL = re.compile(r"^https://www\.livemint\.com/[\w/-]+-\d{11,}\.html$")
# Story IDs leak into text where related-story widgets are embedded
WIDGET_ID = re.compile(r"\s*Id:\d+")
SENTENCE_END = tuple(".!?:;\"'”’)")


def is_article_url(url: str) -> bool:
    return bool(ARTICLE_URL.match(url))


def clean(text: str) -> str:
    return " ".join(WIDGET_ID.sub("", text).split())


def meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return tag["content"].strip() if tag and tag.get("content") else ""


def to_datetime(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_heading(el) -> bool:
    if el.name in ("h2", "h3"):
        return True
    # Livemint also writes sub-headings as a paragraph that is entirely bold
    bold = clean("".join(s.get_text() for s in el.find_all(["strong", "b"])))
    text = clean(el.get_text())
    return bool(text) and bold == text and len(text) < 120


def sections(body) -> list[dict]:
    """Group body paragraphs under their sub-headings."""
    result = [{"heading": None, "paragraphs": []}]
    for el in body.select("div.storyParagraph > h2, div.storyParagraph > h3, div.storyParagraph > p"):
        text = clean(el.get_text())
        if not text:
            continue
        if is_heading(el):
            result.append({"heading": text, "paragraphs": []})
            continue
        paragraphs = result[-1]["paragraphs"]
        # An inline "Also Read" box can split one sentence into two paragraphs
        if paragraphs and not paragraphs[-1].endswith(SENTENCE_END) and text[0].islower():
            paragraphs[-1] = f"{paragraphs[-1]} {text}"
        else:
            paragraphs.append(text)
    return [s for s in result if s["paragraphs"]]


def parse(html: str) -> dict | None:
    """Return the article's fields, or None if the page is not an article."""
    soup = BeautifulSoup(html, "lxml")
    body = soup.select_one("div.storyContent")
    headline = soup.find("h1")
    if body is None or headline is None:
        return None

    author_link = soup.select_one('a[href*="/authors/"]')
    author = meta(soup, "author")
    return {
        "headline": clean(headline.get_text()),
        "summary": meta(soup, "og:description") or None,
        "section": meta(soup, "article:section") or None,
        "image_url": meta(soup, "og:image") or None,
        "published_at": to_datetime(meta(soup, "article:published_time")),
        "modified_at": to_datetime(meta(soup, "article:modified_time")),
        "authors": [{"name": author, "profile_url": author_link["href"] if author_link else None}] if author else [],
        "tags": [t.strip().lower() for t in meta(soup, "keywords").split(",") if t.strip()],
        "sections": sections(body),
        "links": [(a["href"], clean(a.get_text())) for a in body.find_all("a", href=True)],
    }
