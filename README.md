# mint-scrape

A news crawler that stores articles in PostgreSQL in a clean, normalized form ready for RAG (retrieval for AI models). It starts from each news site's RSS feeds, scrapes every article, then follows the article links found inside each story, so older history comes in over time. A URL is scraped once and never again.

Livemint (`livemint.com`) is the first source. The code is source-agnostic: adding a site means one parser module and one database row (see [Adding a new news source](#adding-a-new-news-source)).

## How it works

```
RSS feeds ──► urls (pending, depth 0)
                 │
                 ▼  claim one pending URL (FOR UPDATE SKIP LOCKED)
            fetch page ──► source parser ──► articles, authors, tags,
                 │                           article_chunks, links
                 ▼
     same-site links in the article body
       ├─ looks like an article ─► urls (pending, depth + 1)
       └─ anything else ─────────► urls (skipped, never fetched)
```

Each run of `crawler.py`:

1. Applies `schema.sql` (idempotent, so the first run creates the tables).
2. For every row in `sources` that has a parser, reads its `feed_urls` and queues article URLs at depth 0.
3. Repeatedly claims the shallowest, oldest `pending` URL with `depth <= --max-depth`, scrapes it, saves it, and queues its body links at `depth + 1`, until `--max-pages` pages are processed or nothing is pending.
4. Prints a summary and, in GitHub Actions, writes `pending=<n>` to `$GITHUB_OUTPUT` so the workflow can chain another run.

### Crawl rules

- **Scraped once:** `urls.url` is unique, and only `status = 'pending'` URLs are fetched. Once a URL is `done`, `skipped` or `failed`, it is never fetched again, and a feed listing the same article again changes nothing.
- **URL normalization:** links resolve against the page they appear on, query strings and `#fragments` are dropped, and the host is lowercased, so variants of one URL are stored once.
- **Same site only:** only links on the same host as `sources.base_url` are recorded. Same-site pages that are not articles (topic pages, stock pages, `/market/null`) are stored as `skipped`, so the link graph stays complete without fetching them.
- **Depth:** depth 0 comes from RSS, and each link hop adds 1. A URL keeps the smallest depth it was ever found at. URLs deeper than `--max-depth` stay `pending` until a run with a higher depth picks them up.
- **Failures:** a URL that errors goes back to `pending` and is retried on later runs. After 3 attempts it becomes `failed`, with the error in `urls.last_error`.
- **Concurrency:** claims use `FOR UPDATE SKIP LOCKED`, so parallel crawlers (for example the upstream repo and a fork using the same database) never scrape the same URL.
- **Politeness:** one request at a time, `--delay` seconds apart (default 1s).
- **Edits are not tracked:** an article edited after it was scraped is not re-scraped. To re-scrape one, set its `urls.status` back to `pending`. Its chunks and links are then replaced.

## Database

PostgreSQL, hosted on [Layerbase](https://layerbase.com/docs). All tables are in `schema.sql`.

| Table | One row per | Key columns |
|---|---|---|
| `sources` | news site | `slug` (matches the parser name), `base_url`, `feed_urls` |
| `urls` | URL ever seen; this is the crawl queue | `url` (unique), `status`, `depth`, `attempts`, `last_error`, `discovered_at`, `fetched_at` |
| `articles` | scraped article | `url_id`, `headline`, `summary`, `section`, `image_url`, `published_at`, `modified_at`, `scraped_at`, `content_hash` |
| `authors`, `article_authors` | writer per source, and article↔author | `name` unique per source, `profile_url` |
| `tags`, `article_tags` | lowercased keyword, and article↔tag | `name` (unique) |
| `article_chunks` | RAG text chunk | `article_id`, `chunk_index`, `heading`, `body`, `token_count`, `embedded_at` |
| `links` | link from one URL to another | `from_url_id`, `to_url_id`, `anchor_text`, `first_seen_at` |

The database also holds Better Auth tables (`user`, `session`, `account`, `verification`) from Layerbase's "Auth Server" setup. The crawler does not touch them.

### Timestamps

| Column | Meaning |
|---|---|
| `articles.published_at` | When the site published the article (from the page's meta tags) |
| `articles.modified_at` | When the site last updated it |
| `articles.scraped_at` | When the crawler captured it |
| `urls.discovered_at` | When the URL was first found, in a feed or another article |
| `urls.fetched_at` | When the crawler last tried to fetch it |
| `links.first_seen_at` | When the link was first seen |

All are `timestamptz`: Postgres stores the exact instant but not the page's original offset. Use `SET TIME ZONE 'Asia/Kolkata'` (or your zone) when reading.

### RAG chunks

The body is not dumped as one blob. The parser returns the body as sections (a sub-heading and its paragraphs). The crawler packs each section's paragraphs into chunks of at most about 400 tokens, without splitting a paragraph or crossing a heading. Each chunk stores its `heading`, so a retrieved chunk reads on its own; prepend the article headline when embedding. Lists are stored as `- item` or `1. item` lines and tables as `a | b | c` rows. `token_count` is an estimate (characters ÷ 4).

Layerbase Postgres does not support pgvector yet, so embeddings belong in a separate vector store (Layerbase offers Qdrant), keyed by `article_chunks.id`. Set `embedded_at` once a chunk is pushed. The partial index `article_chunks_unembedded_idx` finds chunks still to embed.

### Rebuilding an article from the database

```sql
SET TIME ZONE 'Asia/Kolkata';

SELECT a.headline, a.summary, a.published_at, a.scraped_at, u.url
FROM articles a JOIN urls u ON u.id = a.url_id
WHERE a.id = 49;

SELECT heading, body FROM article_chunks WHERE article_id = 49 ORDER BY chunk_index;

SELECT l.anchor_text, t.url, t.status
FROM links l JOIN urls t ON t.id = l.to_url_id
WHERE l.from_url_id = (SELECT url_id FROM articles WHERE id = 49);
```

Not stored: where each link sits inside the text, inline images and captions, "Also Read" boxes, and bold or italic formatting.

### Useful queries

```sql
-- Progress
SELECT status, depth, count(*) FROM urls GROUP BY 1, 2 ORDER BY 1, 2;

-- Failures
SELECT url, attempts, last_error FROM urls WHERE status = 'failed';

-- Articles per source
SELECT s.slug, count(*) FROM articles a JOIN sources s ON s.id = a.source_id GROUP BY 1;
```

## Running it

### Locally

```sh
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# .env (git-ignored) holds: DATABASE_URL=postgresql://...?sslmode=require
set -a; source .env; set +a
./.venv/bin/python crawler.py --max-pages 50 --max-depth 2
```

| Flag | Default | Meaning |
|---|---|---|
| `--max-pages` | 50 | Pages to process this run (scraped, skipped and failed all count) |
| `--max-depth` | 2 | Deepest link hop to fetch |
| `--delay` | 1.0 | Seconds between requests |

### GitHub Actions

`.github/workflows/scrape.yml` runs only when triggered manually (**Actions → Scrape news → Run workflow**); there is no cron. Inputs:

| Input | Default | Meaning |
|---|---|---|
| `max_pages` | 200 | Pages per run |
| `max_depth` | 2 | Deepest link hop to fetch |
| `runs_left` | 10 | Further runs to chain while URLs are pending; 0 means this run only |

When a run finishes with URLs still pending, it dispatches the next run itself with `runs_left - 1`. The chain stops when nothing at or above `max_depth` is pending, when `runs_left` reaches 0, or when a run fails. Only one run per repository runs at a time; a chained run waits for the current one.

Setup, in whichever repository runs the workflow (a fork needs its own):

1. Add the repository secret `DATABASE_URL`: **Settings → Secrets and variables → Actions**, or `gh secret set -f .env --repo <owner>/<repo>`.
2. For a fork, enable Actions on the fork and use **Sync fork** to pick up new commits.

If `DATABASE_URL` is missing, the crawl step exits with `DATABASE_URL is not set`.

### Layerbase notes

- Use the **pooled** connection string with `sslmode=require` (the free plan allows 20 pooled connections).
- Free plan limits: 2 databases and 5 GB, no scheduled backups. Idle databases sleep after 15 minutes and wake on the next connection. A database is archived after 14 idle days.
- Leave IP allowlisting off: GitHub runner IPs change on every run.
- Sizing: an article takes roughly 8 to 12 KB including chunks, links and indexes. At 50k articles a day that is about 0.5 GB a day, so a large crawl needs a paid plan.

## Adding a new news source

These steps are written so another developer or agent can follow them in order.

### 1. Study the site

- Find its RSS feeds (often linked as `/rss` or in the page `<head>` as `application/rss+xml`), and check that `<item><link>` holds article URLs.
- Work out a regex that matches article URLs only. Livemint article URLs end with an 11+ digit ID and `.html`: `^https://www\.livemint\.com/[\w/-]+-\d{11,}\.html$`.
- Fetch a few articles with plain HTTP (no browser) and check that the body text is in the HTML. If it only appears after JavaScript runs, this crawler cannot read it as-is.
- In the HTML, find:
  - the body container (Livemint: `div.storyContent`)
  - the blocks that hold real body text (Livemint: `div.storyParagraph`)
  - sub-headings (Livemint: `h2`/`h3`, or a paragraph that is entirely bold)
  - the meta tags for publish time, modified time, author, section and keywords
- Note anything inside the body that is not story text: related-story boxes, newsletter widgets, price widgets, image captions, ads, ID strings, repeated disclaimers.

### 2. Write the parser module

Create `<slug>.py` next to `livemint.py`, using it as the template. It must provide two functions.

```python
def is_article_url(url: str) -> bool:
    """True for article pages only. The URL is already normalized (no query or fragment)."""

def parse(html: str) -> dict | None:
    """Return None if the page is not an article; otherwise a dict with these keys."""
    return {
        "headline": str,                    # required, non-empty
        "summary": str | None,
        "section": str | None,
        "image_url": str | None,
        "published_at": datetime | None,    # timezone-aware
        "modified_at": datetime | None,     # timezone-aware
        "authors": [{"name": str, "profile_url": str | None}],
        "tags": [str],                      # lowercased, stripped
        "sections": [{"heading": str | None, "paragraphs": [str]}],  # in page order
        "links": [(href, anchor_text)],     # every <a href> in the body, raw href
    }
```

Rules:

- `sections` is the RAG input, so clean it:
  - collapse whitespace
  - drop widgets and captions
  - turn lists into `- item` or `1. item` lines and tables into `a | b` rows
  - put text before the first sub-heading in a section with `heading: None`
  - leave out empty sections
- A page whose `sections` are empty is stored as `skipped`, not `done`.
- Return links with their raw `href`. The crawler resolves them against the page URL, filters by host, and applies `is_article_url`.

### 3. Register the source

In `crawler.py`, import the module and add it to `PARSERS` under the source's slug:

```python
import livemint, newsite
PARSERS = {"livemint": livemint, "newsite": newsite}
```

Add its row to the seed at the bottom of `schema.sql`. The crawler applies this file on every run, so the row is created automatically:

```sql
INSERT INTO sources (slug, name, base_url, feed_urls)
VALUES ('newsite', 'New Site', 'https://www.newsite.com', ARRAY['https://www.newsite.com/rss/markets'])
ON CONFLICT (slug) DO NOTHING;
```

`base_url` must have the same host as the article URLs (including `www.`), because only same-host links are followed. `ON CONFLICT DO NOTHING` means editing this seed later does not change an existing row; to add feeds afterwards, run an `UPDATE sources SET feed_urls = ... WHERE slug = ...`.

### 4. Test before crawling

- Run `parse()` on 10 to 30 real articles, and print the sections and links for a few of them. Check that:
  - every sub-heading is captured
  - no widget text, IDs or captions leaked in
  - sentences split by inline boxes are not cut in half
  - `published_at` carries the right timezone
- Check that `is_article_url` rejects topic, tag, author, video and stock pages.
- Run the crawler against a throwaway local Postgres first:
  ```sh
  initdb -D /tmp/pgtest -U test
  pg_ctl -D /tmp/pgtest -o "-p 54329 -k '' -c listen_addresses=127.0.0.1" start
  DATABASE_URL=postgresql://test@127.0.0.1:54329/postgres ./.venv/bin/python crawler.py --max-pages 10 --max-depth 1
  ```
  Then run it a second time and confirm it re-scrapes nothing.
- When changing an existing parser, compare its output with the old version on the same pages. Any difference should be intended.

## Files

| File | Purpose |
|---|---|
| `crawler.py` | Source-agnostic crawler: feeds, queue, fetching, saving, chunking |
| `livemint.py` | Livemint parser; the template for new sources |
| `schema.sql` | Tables, indexes and source seed rows; applied on every run |
| `.github/workflows/scrape.yml` | Manual, self-chaining GitHub Actions workflow |
| `requirements.txt` | `beautifulsoup4`, `lxml`, `psycopg[binary]` |
| `scrape_markets.py`, `scrape_article.py` | Early one-off scripts that write JSON/CSV to `output/`; superseded by the crawler |

## Known gaps

- Edits to an already-scraped article are not picked up (see Crawl rules).
- Livemint's repeated "Disclaimer: This story is for educational purposes only…" line is stored as body text and adds noise to retrieval.
- Pages that need JavaScript to show their text are not supported.
- One request at a time limits a run to roughly 25 to 30 pages a minute. Higher volume needs parallel workers (the `SKIP LOCKED` claim already allows them).
