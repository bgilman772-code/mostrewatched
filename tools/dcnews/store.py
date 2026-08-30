"""SQLite-backed queue of news items moving from ingest to published."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT,
    url          TEXT NOT NULL,
    published    TEXT,
    fetched      TEXT NOT NULL,
    score        INTEGER NOT NULL DEFAULT 0,
    tier         TEXT,
    matched      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    card_path    TEXT,
    ig_media_id  TEXT,
    permalink    TEXT,
    error        TEXT,
    updated      TEXT
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, score DESC);
CREATE TABLE IF NOT EXISTS feed_state (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    last_fetch    TEXT,
    last_status   TEXT
);
"""

# Tracking params that change the URL without changing the article.
TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref_", "__twitter")

STATUSES = ("pending", "approved", "rejected", "posted", "failed")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_url(url: str) -> str:
    """Strip tracking params so the same article isn't queued twice."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith(TRACKING_PREFIXES)
    ]
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, urllib.parse.urlencode(kept), ""))


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


@dataclass
class Item:
    source: str
    title: str
    url: str
    summary: str = ""
    published: str | None = None
    score: int = 0
    tier: str = ""
    matched: list[str] = field(default_factory=list)
    id: str = ""
    status: str = "pending"
    card_path: str | None = None
    ig_media_id: str | None = None
    permalink: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = item_id(self.url)


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- items ------------------------------------------------------------- #

    def add(self, item: Item) -> bool:
        """Insert an item. Returns False if it was already queued."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO items
               (id, source, title, summary, url, published, fetched, score, tier, matched, status, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.id, item.source, item.title, item.summary, canonical_url(item.url),
                item.published, now_iso(), item.score, item.tier,
                json.dumps(item.matched), item.status, now_iso(),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get(self, item_id_: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id_,)).fetchone()

    def list(self, status: str | None = None, limit: int = 50, min_score: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM items WHERE score >= ?"
        args: list = [min_score]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY score DESC, COALESCE(published, fetched) DESC LIMIT ?"
        args.append(limit)
        return list(self.conn.execute(sql, args))

    def set_status(self, item_id_: str, status: str, **fields) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; expected one of {', '.join(STATUSES)}")
        cols = ["status = ?", "updated = ?"]
        args: list = [status, now_iso()]
        for key, value in fields.items():
            if key not in {"card_path", "ig_media_id", "permalink", "error"}:
                raise ValueError(f"cannot set unknown column {key!r}")
            cols.append(f"{key} = ?")
            args.append(value)
        args.append(item_id_)
        self.conn.execute(f"UPDATE items SET {', '.join(cols)} WHERE id = ?", args)
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def posted_since(self, iso_ts: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM items WHERE status = 'posted' AND updated >= ?", (iso_ts,)
        ).fetchone()
        return row["n"]

    # -- feed conditional-GET state ---------------------------------------- #

    def feed_headers(self, url: str) -> dict[str, str]:
        row = self.conn.execute(
            "SELECT etag, last_modified FROM feed_state WHERE url = ?", (url,)
        ).fetchone()
        if not row:
            return {}
        headers = {}
        if row["etag"]:
            headers["If-None-Match"] = row["etag"]
        if row["last_modified"]:
            headers["If-Modified-Since"] = row["last_modified"]
        return headers

    def save_feed_state(self, url: str, etag: str | None, last_modified: str | None, status: str) -> None:
        self.conn.execute(
            """INSERT INTO feed_state (url, etag, last_modified, last_fetch, last_status)
               VALUES (?,?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET
                 etag=COALESCE(excluded.etag, feed_state.etag),
                 last_modified=COALESCE(excluded.last_modified, feed_state.last_modified),
                 last_fetch=excluded.last_fetch,
                 last_status=excluded.last_status""",
            (url, etag, last_modified, now_iso(), status),
        )
        self.conn.commit()
