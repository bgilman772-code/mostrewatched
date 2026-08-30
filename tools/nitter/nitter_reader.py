#!/usr/bin/env python3
"""Read-only Twitter/X reader backed by Nitter RSS feeds.

Nitter exposes every profile and search as an RSS feed, so reading posts needs
no API key and no X account. Public instances are unreliable and go offline
without warning, so every request is tried against each configured instance in
turn until one answers with usable RSS.

Usage:
    ./nitter_reader.py --config feeds.json            # emit posts not seen before
    ./nitter_reader.py --config feeds.json --all      # ignore state, emit everything
    ./nitter_reader.py --check                        # health-check the instances
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "mostrewatched-nitter-reader/1.0 (+https://github.com/bgilman772-code/mostrewatched)"
NS = {"dc": "http://purl.org/dc/elements/1.1/"}

DEFAULT_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://xcancel.com",
]

# Retried; anything else is treated as a dead instance immediately.
RETRY_STATUS = {429, 500, 502, 503, 504}


class FeedError(Exception):
    """An instance could not serve a usable feed."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    name: str
    type: str  # "user" or "search"
    handle: str = ""
    query: str = ""
    include_replies: bool = False
    media_only: bool = False

    def path(self) -> str:
        """Feed path on a Nitter instance, without the instance prefix."""
        if self.type == "user":
            if not self.handle:
                raise ValueError(f"source {self.name!r}: 'handle' is required for type 'user'")
            handle = self.handle.lstrip("@")
            if self.media_only:
                return f"/{handle}/media/rss"
            if self.include_replies:
                return f"/{handle}/with_replies/rss"
            return f"/{handle}/rss"
        if self.type == "search":
            if not self.query:
                raise ValueError(f"source {self.name!r}: 'query' is required for type 'search'")
            qs = urllib.parse.urlencode({"f": "tweets", "q": self.query})
            return f"/search/rss?{qs}"
        raise ValueError(f"source {self.name!r}: unknown type {self.type!r}")


@dataclass
class Config:
    instances: list[str] = field(default_factory=lambda: list(DEFAULT_INSTANCES))
    sources: list[Source] = field(default_factory=list)
    request_timeout: float = 20.0
    delay_between_requests: float = 2.0
    max_seen_per_source: int = 500

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        instances = [i.rstrip("/") for i in raw.get("instances") or DEFAULT_INSTANCES]
        sources = []
        for entry in raw.get("sources", []):
            known = {f for f in Source.__dataclass_fields__}
            unknown = set(entry) - known
            if unknown:
                raise ValueError(f"unknown key(s) in source: {', '.join(sorted(unknown))}")
            sources.append(Source(**entry))
        if not sources:
            raise ValueError(f"{path}: no sources configured")
        names = [s.name for s in sources]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"{path}: duplicate source name(s): {', '.join(sorted(dupes))}")
        cfg = cls(instances=instances, sources=sources)
        for key in ("request_timeout", "delay_between_requests", "max_seen_per_source"):
            if key in raw:
                setattr(cfg, key, raw[key])
        return cfg


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class State:
    """Remembers which post ids have already been emitted, per source."""

    def __init__(self, path: str, cap: int = 500):
        self.path = path
        self.cap = cap
        self.seen: dict[str, list[str]] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.seen = json.load(fh).get("seen", {})
            except (OSError, ValueError) as exc:
                log(f"warning: could not read state file {path}: {exc}; starting fresh")

    def is_new(self, source: str, post_id: str) -> bool:
        return post_id not in self.seen.get(source, [])

    def mark(self, source: str, post_ids: list[str]) -> None:
        bucket = self.seen.setdefault(source, [])
        bucket.extend(pid for pid in post_ids if pid not in bucket)
        if len(bucket) > self.cap:
            del bucket[: len(bucket) - self.cap]

    def save(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        payload = {"updated": datetime.now(timezone.utc).isoformat(), "seen": self.seen}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def http_get(url: str, timeout: float, attempts: int = 3) -> bytes:
    """GET with backoff on the status codes worth retrying."""
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_STATUS:
                raise FeedError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < attempts - 1:
            time.sleep(2 ** attempt + random.uniform(0, 0.5))
    raise FeedError(str(last))


def unproxy_media(url: str) -> str:
    """Turn a Nitter-proxied media URL back into its twimg.com original.

    Nitter serves media through /pic/<url-encoded path>, which only works for as
    long as that instance does. The underlying twimg.com URL keeps working.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        for prefix in ("/pic/", "/video/"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        else:
            return url
        decoded = urllib.parse.unquote(path)
        if decoded.startswith("orig/"):
            decoded = decoded[len("orig/") :]
        # /video/<hash>/<encoded url> — keep only the embedded URL.
        idx = decoded.find("twimg.com")
        if idx != -1:
            start = decoded.rfind("/", 0, idx)
            host_and_path = decoded[start + 1 :] if start != -1 else decoded
            return f"https://{host_and_path}"
        return f"https://pbs.twimg.com/{decoded.lstrip('/')}"
    except ValueError:
        return url


MEDIA_RE = re.compile(r'<(?:img|source|video)[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def extract_media(description: str, instance: str) -> list[str]:
    out: list[str] = []
    for raw in MEDIA_RE.findall(description or ""):
        url = html.unescape(raw)
        if url.startswith("/"):
            url = instance + url
        url = unproxy_media(url)
        if url not in out:
            out.append(url)
    return out


def strip_html(description: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", description or ""))
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(link: str) -> str:
    """Rewrite an instance link to its x.com equivalent."""
    try:
        parsed = urllib.parse.urlparse(link)
        path = parsed.path.replace("#m", "")
        return urllib.parse.urlunparse(("https", "x.com", path, "", "", ""))
    except ValueError:
        return link


def parse_feed(body: bytes, instance: str, source: Source) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML ({exc})") from exc
    channel = root.find("channel")
    if channel is None:
        raise FeedError("no <channel> element (not an RSS feed)")

    posts = []
    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not guid:
            continue
        description = item.findtext("description") or ""
        pub_raw = (item.findtext("pubDate") or "").strip()
        published = None
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published = pub_raw
        posts.append(
            {
                "source": source.name,
                "id": guid,
                "author": (item.findtext("dc:creator", namespaces=NS) or "").strip(),
                "title": strip_html(item.findtext("title") or ""),
                "text": strip_html(description),
                "published": published,
                "url": canonical_url(link),
                "nitter_url": link,
                "media": extract_media(description, instance),
                "instance": instance,
            }
        )
    return posts


def fetch_source(source: Source, cfg: Config, instances: list[str]) -> list[dict]:
    """Try each instance in turn; the first usable feed wins."""
    path = source.path()
    errors = []
    for instance in instances:
        url = instance + path
        try:
            body = http_get(url, cfg.request_timeout)
            posts = parse_feed(body, instance, source)
            log(f"  {source.name}: {len(posts)} post(s) via {instance}")
            return posts
        except FeedError as exc:
            errors.append(f"{instance}: {exc}")
        finally:
            time.sleep(cfg.delay_between_requests)
    detail = "\n      ".join(errors)
    raise FeedError(f"no instance served {source.name!r}:\n      {detail}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def check_instances(cfg: Config) -> int:
    """Health-check every instance against a known-good feed path."""
    probe = cfg.sources[0].path() if cfg.sources else "/jack/rss"
    alive = 0
    for instance in cfg.instances:
        start = time.monotonic()
        try:
            body = http_get(instance + probe, cfg.request_timeout, attempts=1)
            count = len(parse_feed(body, instance, cfg.sources[0] if cfg.sources else Source("probe", "user", "jack")))
            elapsed = time.monotonic() - start
            print(f"OK    {instance}  ({count} items, {elapsed:.1f}s)")
            alive += 1
        except FeedError as exc:
            print(f"DEAD  {instance}  ({exc})")
        time.sleep(cfg.delay_between_requests)
    print(f"\n{alive}/{len(cfg.instances)} instance(s) usable")
    return 0 if alive else 1


def emit(posts: list[dict], fmt: str) -> None:
    if fmt == "json":
        for post in posts:
            print(json.dumps(post, ensure_ascii=False))
        return
    for post in posts:
        print(f"[{post['published'] or '?'}] {post['author'] or post['source']}")
        print(f"  {post['text'][:280]}")
        print(f"  {post['url']}")
        for media in post["media"]:
            print(f"  media: {media}")
        print()


def run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    if args.check:
        return check_instances(cfg)

    instances = list(cfg.instances)
    if args.shuffle:
        random.shuffle(instances)

    state = State("" if args.all else args.state, cap=cfg.max_seen_per_source)
    collected: list[dict] = []
    failures = 0

    for source in cfg.sources:
        try:
            posts = fetch_source(source, cfg, instances)
        except (FeedError, ValueError) as exc:
            log(f"  {source.name}: FAILED — {exc}")
            failures += 1
            continue
        fresh = [p for p in posts if args.all or state.is_new(source.name, p["id"])]
        state.mark(source.name, [p["id"] for p in posts])
        collected.extend(fresh)

    collected.sort(key=lambda p: p["published"] or "", reverse=True)
    if args.limit:
        collected = collected[: args.limit]
    emit(collected, args.format)

    if not args.all:
        state.save()

    log(f"{len(collected)} new post(s); {failures} source(s) failed")
    if failures == len(cfg.sources):
        return 1
    return 0


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(here, "feeds.json"), help="path to the feed config (default: feeds.json next to this script)")
    parser.add_argument("--state", default=os.path.join(here, "state.json"), help="path to the seen-post state file")
    parser.add_argument("--all", action="store_true", help="emit every post in the feed, ignoring and not updating state")
    parser.add_argument("--check", action="store_true", help="health-check the configured instances and exit")
    parser.add_argument("--shuffle", action="store_true", help="randomise instance order to spread load")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="output format (default: json lines)")
    parser.add_argument("--limit", type=int, default=0, help="emit at most N posts")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, ValueError) as exc:
        log(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
