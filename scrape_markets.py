"""Scrape the Livemint markets RSS feed with BeautifulSoup.

Usage:
    ./.venv/bin/python scrape_markets.py [--out-dir output]
"""

import argparse
import csv
import json
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from bs4 import BeautifulSoup

FEED_URL = "https://www.livemint.com/rss/markets"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def text_of(item, tag: str) -> str:
    node = item.find(tag)
    return node.get_text(strip=True) if node else ""


def parse_items(xml: bytes) -> list[dict]:
    soup = BeautifulSoup(xml, "lxml-xml")
    articles = []
    for item in soup.find_all("item"):
        pub_date = text_of(item, "pubDate")
        media = item.find("media:content")
        articles.append(
            {
                "title": text_of(item, "title"),
                "link": text_of(item, "link"),
                "description": text_of(item, "description"),
                "published_at": parsedate_to_datetime(pub_date).isoformat() if pub_date else "",
                "image_url": media.get("url", "") if media else "",
            }
        )
    return articles


def save(articles: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    json_path = out_dir / f"livemint_markets_{stamp}.json"
    json_path.write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = out_dir / f"livemint_markets_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(articles[0].keys()))
        writer.writeheader()
        writer.writerows(articles)

    print(f"Saved {len(articles)} articles to:\n  {json_path}\n  {csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="output", type=Path)
    args = parser.parse_args()

    articles = parse_items(fetch(FEED_URL))
    if not articles:
        raise SystemExit("No articles found in feed")

    for article in articles:
        print(f"{article['published_at']} | {article['title']}")
    save(articles, args.out_dir)


if __name__ == "__main__":
    main()
