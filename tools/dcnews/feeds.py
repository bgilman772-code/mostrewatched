"""Fetch and parse RSS/Atom feeds from local news sources."""

from __future__ import annotations

import gzip
import html
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "dcnews-bot/1.0 (Navy Yard / DC local news aggregator)"
RETRY_STATUS = {429, 500, 502, 503, 504}

ATOM = "{http://www.w3.org/2005/Atom}"
TAG_RE = re.compile(r"<[^>]+>")


class FeedError(Exception):
    """A feed could not be fetched or parsed."""


class NotModified(Exception):
    """Server answered 304 — nothing new since the last fetch."""


@dataclass
class Entry:
    source: str
    title: str
    url: str
    summary: str
    published: str | None


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()


def parse_date(raw: str) -> str | None:
    """Parse RFC-822 (RSS) or ISO-8601 (Atom) into a UTC ISO timestamp."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass
    try:
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def fetch(url: str, timeout: float = 20.0, extra_headers: dict | None = None, attempts: int = 3) -> tuple[bytes, dict]:
    """GET a feed. Raises NotModified on 304, FeedError on anything unusable."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip",
    }
    headers.update(extra_headers or {})
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                raise NotModified() from exc
            last = exc
            if exc.code not in RETRY_STATUS:
                raise FeedError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < attempts - 1:
            time.sleep(2 ** attempt)
    raise FeedError(str(last))


def parse(body: bytes, source: str) -> list[Entry]:
    """Parse an RSS 2.0 or Atom document into entries."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML ({exc})") from exc

    entries: list[Entry] = []
    channel = root.find("channel")
    if channel is not None:  # RSS
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                continue
            entries.append(
                Entry(
                    source=source,
                    title=strip_html(item.findtext("title") or ""),
                    url=link,
                    summary=strip_html(item.findtext("description") or ""),
                    published=parse_date(item.findtext("pubDate") or ""),
                )
            )
        return entries

    if root.tag == f"{ATOM}feed":  # Atom
        for item in root.findall(f"{ATOM}entry"):
            link = ""
            for link_el in item.findall(f"{ATOM}link"):
                rel = link_el.get("rel", "alternate")
                if rel == "alternate" and link_el.get("href"):
                    link = link_el.get("href", "").strip()
                    break
            if not link:
                link = (item.findtext(f"{ATOM}id") or "").strip()
            if not link.startswith("http"):
                continue
            summary = item.findtext(f"{ATOM}summary") or item.findtext(f"{ATOM}content") or ""
            entries.append(
                Entry(
                    source=source,
                    title=strip_html(item.findtext(f"{ATOM}title") or ""),
                    url=link,
                    summary=strip_html(summary),
                    published=parse_date(
                        item.findtext(f"{ATOM}published") or item.findtext(f"{ATOM}updated") or ""
                    ),
                )
            )
        return entries

    raise FeedError("neither RSS <channel> nor Atom <feed> found")
