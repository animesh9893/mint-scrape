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


def block_text(el) -> str:
    """Text of a body block; lists become '- item' lines and tables 'a | b' rows."""
    if el.name in ("ul", "ol"):
        items = [clean(li.get_text()) for li in el.find_all("li", recursive=False)]
        marker = (lambda i: f"{i}.") if el.name == "ol" else (lambda i: "-")
        return "\n".join(f"{marker(i)} {item}" for i, item in enumerate((t for t in items if t), 1))
    if el.name == "table":
        rows = [" | ".join(clean(cell.get_text()) for cell in tr.find_all(["th", "td"])) for tr in el.find_all("tr")]
        return "\n".join(row for row in rows if row.strip(" |"))
    return clean(el.get_text())


def sections(body) -> list[dict]:
    """Group body blocks under their sub-headings."""
    result = [{"heading": None, "paragraphs": []}]
    previous_tag = None
    for el in body.find_all(["h2", "h3", "p", "ul", "ol", "table"]):
        # Body text lives in storyParagraph blocks; widgets, captions and "Also Read" boxes sit outside.
        # Lists and tables are read whole, so skip the blocks nested inside them.
        if not el.find_parent(class_="storyParagraph") or el.find_parent(["ul", "ol", "table"]):
            continue
        text = block_text(el)
        if not text:
            continue
        if el.name in ("h2", "h3", "p") and is_heading(el):
            result.append({"heading": text, "paragraphs": []})
            previous_tag = None
            continue
        paragraphs = result[-1]["paragraphs"]
        # An inline "Also Read" box can split one sentence into two paragraphs
        if (el.name == previous_tag == "p" and not paragraphs[-1].endswith(SENTENCE_END)
                and text[0].islower()):
            paragraphs[-1] = f"{paragraphs[-1]} {text}"
        else:
            paragraphs.append(text)
        previous_tag = el.name
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
