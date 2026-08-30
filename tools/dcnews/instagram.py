"""Publish a rendered card to Instagram via the Content Publishing API.

Publishing is two calls plus a wait: create a media container pointing at a
publicly reachable image URL, wait for Instagram to finish downloading it, then
publish the container. Instagram fetches the image itself, so the URL has to be
reachable from the public internet — a localhost path will fail.

Both auth paths are supported:
  graph.facebook.com  — Instagram Business account linked to a Facebook Page
  graph.instagram.com — Instagram Login (business login, no Page required)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
MAX_CAPTION = 2200
MAX_HASHTAGS = 30


class InstagramError(Exception):
    """The Graph API rejected a request."""


@dataclass
class PublishResult:
    media_id: str
    permalink: str = ""
    container_id: str = ""


def _request(url: str, data: dict | None = None, timeout: float = 60.0) -> dict:
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method="POST" if data else "GET")
    req.add_header("User-Agent", "dcnews-bot/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(detail).get("error", {})
            msg = err.get("error_user_msg") or err.get("message") or detail
            code = err.get("code", exc.code)
            raise InstagramError(f"[{code}] {msg}") from exc
        except (ValueError, AttributeError):
            raise InstagramError(f"HTTP {exc.code}: {detail[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InstagramError(f"network error: {exc}") from exc


def build_caption(
    headline: str,
    source: str,
    url: str = "",
    tier: str = "dc",
    hashtags: list[str] | None = None,
    extra: str = "",
) -> str:
    """Assemble a caption: headline, attribution, then hashtags.

    Instagram captions aren't clickable, so the article URL is included as
    plain text for readers who want to search it out.
    """
    tags = list(hashtags or [])
    if tier == "navy_yard":
        tags = ["#NavyYard", "#CapitolRiverfront", "#NavyYardDC"] + tags
    tags = ["#WashingtonDC", "#DCNews"] + tags
    seen, deduped = set(), []
    for tag in tags:
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(tag)
    deduped = deduped[:MAX_HASHTAGS]

    parts = [headline.strip()]
    if extra.strip():
        parts.append(extra.strip())
    if source.strip():
        parts.append(f"Source: {source.strip()}")
    if url.strip():
        parts.append(url.strip())
    parts.append(" ".join(deduped))

    caption = "\n\n".join(p for p in parts if p)
    if len(caption) > MAX_CAPTION:
        # Trim the headline block rather than losing attribution or tags.
        overflow = len(caption) - MAX_CAPTION + 1
        parts[0] = parts[0][: max(0, len(parts[0]) - overflow)].rstrip() + "…"
        caption = "\n\n".join(p for p in parts if p)
    return caption


class InstagramPublisher:
    def __init__(
        self,
        ig_user_id: str,
        access_token: str,
        api_base: str = DEFAULT_API_BASE,
        api_version: str = DEFAULT_API_VERSION,
        dry_run: bool = True,
    ):
        if not dry_run and (not ig_user_id or not access_token):
            raise InstagramError("ig_user_id and access_token are required to publish")
        self.ig_user_id = ig_user_id
        self.token = access_token
        self.base = f"{api_base.rstrip('/')}/{api_version}"
        self.dry_run = dry_run

    def _url(self, path: str, **params) -> str:
        params["access_token"] = self.token
        return f"{self.base}/{path}?{urllib.parse.urlencode(params)}"

    def publishing_limit(self) -> dict:
        """How many of the rolling 24-hour posts have been used (limit is 25)."""
        if self.dry_run:
            return {"dry_run": True}
        data = _request(self._url(f"{self.ig_user_id}/content_publishing_limit", fields="config,quota_usage"))
        entries = data.get("data") or [{}]
        return entries[0]

    def create_container(self, image_url: str, caption: str) -> str:
        if self.dry_run:
            return "dry-run-container"
        data = _request(
            f"{self.base}/{self.ig_user_id}/media",
            {"image_url": image_url, "caption": caption, "access_token": self.token},
        )
        container = data.get("id")
        if not container:
            raise InstagramError(f"no container id in response: {data}")
        return container

    def wait_for_container(self, container_id: str, timeout: float = 120.0, interval: float = 5.0) -> None:
        """Poll until Instagram has finished downloading the image."""
        if self.dry_run:
            return
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            data = _request(self._url(container_id, fields="status_code,status"))
            last = data.get("status_code", "")
            if last == "FINISHED":
                return
            if last in ("ERROR", "EXPIRED"):
                raise InstagramError(f"container {last}: {data.get('status', 'no detail')}")
            time.sleep(interval)
        raise InstagramError(f"container not ready after {timeout:.0f}s (last status: {last or 'unknown'})")

    def publish(self, image_url: str, caption: str) -> PublishResult:
        """Full publish flow: container, wait, publish, fetch permalink."""
        if self.dry_run:
            return PublishResult(media_id="dry-run", permalink="", container_id="dry-run-container")
        container = self.create_container(image_url, caption)
        self.wait_for_container(container)
        data = _request(
            f"{self.base}/{self.ig_user_id}/media_publish",
            {"creation_id": container, "access_token": self.token},
        )
        media_id = data.get("id")
        if not media_id:
            raise InstagramError(f"no media id in publish response: {data}")
        permalink = ""
        try:
            permalink = _request(self._url(media_id, fields="permalink")).get("permalink", "")
        except InstagramError:
            pass  # The post is live; a missing permalink isn't worth failing over.
        return PublishResult(media_id=media_id, permalink=permalink, container_id=container)
