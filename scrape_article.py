"""Scrape one Livemint article page with BeautifulSoup.

Usage:
    ./.venv/bin/python scrape_article.py <article-url> [--out-dir output]
"""

import argparse
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
    return ""


def body_links(soup: BeautifulSoup, url: str) -> list[dict]:
    body = soup.select_one("div.storyContent")
    if body is None:
        return []
    links, seen = [], set()
    for a in body.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        links.append({"text": a.get_text(" ", strip=True), "url": href})
    return links


def parse_article(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    headline = soup.find("h1")
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in soup.select("div.paragraph p, article p, .storyParagraph p, .contentstyle p")
        if len(p.get_text(strip=True)) > 40
    ]
    # Fallback: any sufficiently long paragraph on the page
    if not paragraphs:
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 80]

    return {
        "url": url,
        "headline": headline.get_text(" ", strip=True) if headline else meta(soup, "og:title"),
        "description": meta(soup, "og:description", "description"),
        "author": meta(soup, "author") or meta(soup, "article:author"),
        "published_time": meta(soup, "article:published_time", "og:published_time"),
        "modified_time": meta(soup, "article:modified_time"),
        "section": meta(soup, "article:section"),
        "keywords": meta(soup, "keywords"),
        "image_url": meta(soup, "og:image"),
        "paragraphs": paragraphs,
        "links": body_links(soup, url),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--out-dir", default="output", type=Path)
    args = parser.parse_args()

    article = parse_article(fetch(args.url), args.url)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = args.out_dir / f"article_{stamp}.json"
    out_path.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(article, ensure_ascii=False, indent=2))
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
