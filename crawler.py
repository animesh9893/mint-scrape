"""Crawl news sources into Postgres.

Each run reads every source's RSS feeds, queues new article URLs, then scrapes
pending URLs and queues the article links found in each body. A URL is scraped
once: after that its status is 'done', 'failed' or 'skipped'.

Usage:
    DATABASE_URL=postgresql://... python crawler.py [--max-pages 50] [--max-depth 2]
"""

import argparse
import hashlib
import os
import time
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import psycopg
from bs4 import BeautifulSoup

import livemint

PARSERS = {"livemint": livemint}
SCHEMA = Path(__file__).with_name("schema.sql")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
MAX_ATTEMPTS = 3
CHUNK_TOKENS = 400


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def normalize(href: str, base: str) -> str:
    parts = urlsplit(urljoin(base, href.strip()))
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, "", ""))


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)  # ~4 characters per token for English


def chunk(sections: list[dict]) -> list[tuple[str | None, str]]:
    """Split sections into (heading, text) chunks of at most ~CHUNK_TOKENS."""
    chunks = []
    for section in sections:
        buffer = []
        for paragraph in section["paragraphs"]:
            if buffer and approx_tokens(" ".join(buffer + [paragraph])) > CHUNK_TOKENS:
                chunks.append((section["heading"], "\n\n".join(buffer)))
                buffer = []
            buffer.append(paragraph)
        if buffer:
            chunks.append((section["heading"], "\n\n".join(buffer)))
    return chunks


def upsert_url(conn, source_id: int, url: str, depth: int, status: str = "pending") -> int:
    return conn.execute(
        """INSERT INTO urls (source_id, url, depth, status) VALUES (%s, %s, %s, %s)
           ON CONFLICT (url) DO UPDATE SET depth = LEAST(urls.depth, EXCLUDED.depth)
           RETURNING id""",
        (source_id, url, depth, status),
    ).fetchone()[0]


def queue_feeds(conn, source: dict, parser) -> int:
    queued = 0
    for feed_url in source["feed_urls"]:
        try:
            items = BeautifulSoup(fetch(feed_url), "lxml-xml").find_all("item")
        except Exception as error:
            print(f"  feed failed {feed_url}: {error}")
            continue
        for item in items:
            link = item.find("link")
            url = normalize(link.get_text(strip=True), source["base_url"]) if link else ""
            if parser.is_article_url(url):
                upsert_url(conn, source["id"], url, 0)
                queued += 1
    return queued


def claim(conn, max_depth: int, tried: list[int]):
    return conn.execute(
        """SELECT u.id, u.url, u.depth, u.source_id, s.slug, s.base_url
           FROM urls u JOIN sources s ON s.id = u.source_id
           WHERE u.status = 'pending' AND u.depth <= %s AND NOT (u.id = ANY(%s))
           ORDER BY u.depth, u.discovered_at
           LIMIT 1
           FOR UPDATE OF u SKIP LOCKED""",
        (max_depth, tried),
    ).fetchone()


def save_article(conn, url_id: int, url: str, source_id: int, depth: int, base_url: str, parser, article: dict) -> int:
    body = "\n\n".join(p for s in article["sections"] for p in s["paragraphs"])
    article_id = conn.execute(
        """INSERT INTO articles (url_id, source_id, headline, summary, section, image_url,
                                 published_at, modified_at, content_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (url_id) DO UPDATE SET
               headline = EXCLUDED.headline, summary = EXCLUDED.summary,
               section = EXCLUDED.section, image_url = EXCLUDED.image_url,
               published_at = EXCLUDED.published_at, modified_at = EXCLUDED.modified_at,
               content_hash = EXCLUDED.content_hash, scraped_at = now()
           RETURNING id""",
        (url_id, source_id, article["headline"], article["summary"], article["section"],
         article["image_url"], article["published_at"], article["modified_at"],
         hashlib.sha256(body.encode()).hexdigest()),
    ).fetchone()[0]

    for author in article["authors"]:
        author_id = conn.execute(
            """INSERT INTO authors (source_id, name, profile_url) VALUES (%s, %s, %s)
               ON CONFLICT (source_id, name)
               DO UPDATE SET profile_url = COALESCE(EXCLUDED.profile_url, authors.profile_url)
               RETURNING id""",
            (source_id, author["name"], author["profile_url"]),
        ).fetchone()[0]
        conn.execute("INSERT INTO article_authors VALUES (%s, %s) ON CONFLICT DO NOTHING", (article_id, author_id))

    for tag in article["tags"]:
        tag_id = conn.execute(
            "INSERT INTO tags (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (tag,),
        ).fetchone()[0]
        conn.execute("INSERT INTO article_tags VALUES (%s, %s) ON CONFLICT DO NOTHING", (article_id, tag_id))

    conn.execute("DELETE FROM article_chunks WHERE article_id = %s", (article_id,))
    for index, (heading, text) in enumerate(chunk(article["sections"])):
        conn.execute(
            """INSERT INTO article_chunks (article_id, chunk_index, heading, body, token_count)
               VALUES (%s, %s, %s, %s, %s)""",
            (article_id, index, heading, text, approx_tokens(text)),
        )

    # Follow same-site links only; non-article pages are recorded but never fetched
    host = urlsplit(base_url).netloc
    target_ids = []
    for href, anchor in article["links"]:
        target = normalize(href, url)  # relative hrefs resolve against the article itself
        if urlsplit(target).netloc != host:
            continue
        status = "pending" if parser.is_article_url(target) else "skipped"
        target_id = upsert_url(conn, source_id, target, depth + 1, status)
        if target_id != url_id:
            target_ids.append(target_id)
            conn.execute(
                "INSERT INTO links (from_url_id, to_url_id, anchor_text) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (url_id, target_id, anchor or None),
            )
    # On a re-scrape, drop links the article no longer has
    conn.execute(
        "DELETE FROM links WHERE from_url_id = %s AND NOT (to_url_id = ANY(%s))", (url_id, target_ids)
    )
    return article_id


def crawl(conn, max_pages: int, max_depth: int, delay: float) -> None:
    counts = {"done": 0, "skipped": 0, "failed": 0}
    tried: list[int] = []
    while len(tried) < max_pages:
        try:
            with conn.transaction():
                row = claim(conn, max_depth, tried)
                if row is None:
                    break
                url_id, url, depth, source_id, slug, base_url = row
                tried.append(url_id)
                parser = PARSERS[slug]
                article = parser.parse(fetch(url).decode("utf-8", errors="replace"))
                if article is None or not article["sections"]:
                    status = "skipped"
                else:
                    save_article(conn, url_id, url, source_id, depth, base_url, parser, article)
                    status = "done"
                conn.execute(
                    """UPDATE urls SET status = %s, attempts = attempts + 1, last_error = NULL, fetched_at = now()
                       WHERE id = %s""",
                    (status, url_id),
                )
            counts[status] += 1
            print(f"  {status:<7} d{depth} {url}")
        except Exception as error:
            if not tried:
                raise
            conn.execute(
                """UPDATE urls SET attempts = attempts + 1, last_error = %s, fetched_at = now(),
                       status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END
                   WHERE id = %s""",
                (str(error)[:500], MAX_ATTEMPTS, tried[-1]),
            )
            counts["failed"] += 1
            print(f"  failed  {error}")
        time.sleep(delay)

    pending = conn.execute(
        "SELECT count(*) FROM urls WHERE status = 'pending' AND depth <= %s", (max_depth,)
    ).fetchone()[0]
    print(f"Scraped {counts['done']}, skipped {counts['skipped']}, failed {counts['failed']}; {pending} still pending")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-pages", type=int, default=50, help="pages to scrape this run")
    parser.add_argument("--max-depth", type=int, default=2, help="link hops to follow from the feeds")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = parser.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
        sources = conn.execute("SELECT id, slug, base_url, feed_urls FROM sources ORDER BY id").fetchall()
        for source_id, slug, base_url, feed_urls in sources:
            if slug not in PARSERS:
                print(f"{slug}: no parser, skipping")
                continue
            source = {"id": source_id, "base_url": base_url, "feed_urls": feed_urls}
            print(f"{slug}: {queue_feeds(conn, source, PARSERS[slug])} feed items")
        crawl(conn, args.max_pages, args.max_depth, args.delay)


if __name__ == "__main__":
    main()
