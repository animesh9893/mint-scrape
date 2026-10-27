-- News scraper schema (PostgreSQL on Layerbase).
-- Multi-source, crawl-aware, and chunked for RAG. Embeddings live outside
-- Postgres (Layerbase Postgres has no pgvector yet), keyed by article_chunks.id.

CREATE TABLE IF NOT EXISTS sources (
    id          smallserial PRIMARY KEY,
    slug        text NOT NULL UNIQUE,          -- 'livemint'
    name        text NOT NULL,                 -- 'Mint'
    base_url    text NOT NULL,                 -- 'https://www.livemint.com'
    feed_urls   text[] NOT NULL DEFAULT '{}',  -- RSS feeds polled every run
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Crawl frontier. Every URL seen once, ever. A URL is fetched only while
-- status = 'pending', so already-parsed links are never parsed again.
CREATE TABLE IF NOT EXISTS urls (
    id            bigserial PRIMARY KEY,
    source_id     smallint NOT NULL REFERENCES sources(id),
    url           text NOT NULL UNIQUE,        -- normalized: no query/fragment
    status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'done', 'failed', 'skipped')),
    depth         smallint NOT NULL DEFAULT 0, -- 0 = found in RSS
    attempts      smallint NOT NULL DEFAULT 0,
    last_error    text,
    discovered_at timestamptz NOT NULL DEFAULT now(),
    fetched_at    timestamptz
);
CREATE INDEX IF NOT EXISTS urls_pending_idx ON urls (discovered_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS authors (
    id           bigserial PRIMARY KEY,
    source_id    smallint NOT NULL REFERENCES sources(id),
    name         text NOT NULL,
    profile_url  text,
    UNIQUE (source_id, name)
);

CREATE TABLE IF NOT EXISTS articles (
    id             bigserial PRIMARY KEY,
    url_id         bigint NOT NULL UNIQUE REFERENCES urls(id),
    source_id      smallint NOT NULL REFERENCES sources(id),
    headline       text NOT NULL,
    summary        text,                       -- og:description / standfirst
    section        text,
    image_url      text,
    published_at   timestamptz,
    modified_at    timestamptz,
    scraped_at     timestamptz NOT NULL DEFAULT now(),
    content_hash   text NOT NULL               -- sha256 of cleaned body; detects edits
);
CREATE INDEX IF NOT EXISTS articles_published_idx ON articles (source_id, published_at DESC);

CREATE TABLE IF NOT EXISTS article_authors (
    article_id  bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    author_id   bigint NOT NULL REFERENCES authors(id),
    PRIMARY KEY (article_id, author_id)
);

CREATE TABLE IF NOT EXISTS tags (
    id    bigserial PRIMARY KEY,
    name  text NOT NULL UNIQUE                 -- lowercased keyword
);

CREATE TABLE IF NOT EXISTS article_tags (
    article_id  bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    tag_id      bigint NOT NULL REFERENCES tags(id),
    PRIMARY KEY (article_id, tag_id)
);

-- RAG unit: cleaned body text split by sub-heading into ~300-500 token chunks.
-- Each chunk carries its heading so it reads on its own when retrieved.
CREATE TABLE IF NOT EXISTS article_chunks (
    id           bigserial PRIMARY KEY,
    article_id   bigint NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    chunk_index  smallint NOT NULL,
    heading      text,
    body         text NOT NULL,
    token_count  integer NOT NULL,
    embedded_at  timestamptz,                  -- set once pushed to the vector store
    UNIQUE (article_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS article_chunks_unembedded_idx ON article_chunks (id) WHERE embedded_at IS NULL;

-- Link graph between URLs, with when each edge was first seen.
CREATE TABLE IF NOT EXISTS links (
    from_url_id    bigint NOT NULL REFERENCES urls(id),
    to_url_id      bigint NOT NULL REFERENCES urls(id),
    anchor_text    text,
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_url_id, to_url_id)
);

INSERT INTO sources (slug, name, base_url, feed_urls)
VALUES ('livemint', 'Mint', 'https://www.livemint.com', ARRAY['https://www.livemint.com/rss/markets'])
ON CONFLICT (slug) DO NOTHING;
