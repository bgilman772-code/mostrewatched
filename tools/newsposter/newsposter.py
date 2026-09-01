#!/usr/bin/env python3
"""Fire news posts on a schedule — built to run as a Railway cron service.

One run is one shot: read the configured feeds, drop anything already posted,
render what's left through a per-source template, publish it to the configured
target, record what went out, exit. State makes repeated runs safe, so the
schedule can be as tight as the feeds justify.

Usage:
    ./newsposter.py --check          # validate config + credentials, fetch nothing
    ./newsposter.py --dry-run        # render what would go out, publish nothing
    ./newsposter.py                  # publish
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "mostrewatched-newsposter/1.0 (+https://github.com/bgilman772-code/mostrewatched)"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
RETRY_STATUS = {429, 500, 502, 503, 504}

# X shortens every link to a t.co of this length, whatever the original.
TCO_LENGTH = 23
URL_RE = re.compile(r"https?://\S+")
TAG_RE = re.compile(r"<[^>]+>")


class FeedError(Exception):
    """A source could not be read."""


class PublishError(Exception):
    """A post could not be published."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    name: str
    type: str = "rss"  # "rss" or "nitter"
    url: str = ""
    template: str = ""
    max_per_run: int = 0  # 0 = no per-source cap
    nitter: dict = field(default_factory=dict)  # a tools/nitter source entry

    def validate(self) -> None:
        if self.type == "rss":
            if not self.url:
                raise ValueError(f"source {self.name!r}: 'url' is required for type 'rss'")
        elif self.type == "nitter":
            if not self.nitter:
                raise ValueError(f"source {self.name!r}: 'nitter' is required for type 'nitter'")
        else:
            raise ValueError(f"source {self.name!r}: unknown type {self.type!r}")


@dataclass
class Config:
    sources: list[Source] = field(default_factory=list)
    target: str = "stdout"  # stdout | webhook | x
    template: str = "{title} {url}"
    request_timeout: float = 20.0
    delay_between_requests: float = 2.0
    delay_between_posts: float = 5.0
    max_posts_per_run: int = 5
    max_seen_per_source: int = 500
    nitter_instances: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | None, inline: str | None) -> "Config":
        """Config comes from NEWSPOSTER_CONFIG_JSON if set, else from a file.

        Railway services have env vars and no convenient writable checkout, so
        the inline form is the one that actually gets used in production; the
        file form is for local runs.
        """
        if inline:
            try:
                raw = json.loads(inline)
            except ValueError as exc:
                raise ValueError(f"NEWSPOSTER_CONFIG_JSON is not valid JSON: {exc}") from exc
            origin = "NEWSPOSTER_CONFIG_JSON"
        else:
            if not path or not os.path.exists(path):
                raise ValueError(
                    f"no config: set NEWSPOSTER_CONFIG_JSON, or create {path or 'feeds.json'}"
                )
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            origin = path

        cfg = cls()
        known_source_keys = set(Source.__dataclass_fields__)
        for entry in raw.get("sources", []):
            unknown = set(entry) - known_source_keys
            if unknown:
                raise ValueError(f"{origin}: unknown key(s) in source: {', '.join(sorted(unknown))}")
            source = Source(**entry)
            source.validate()
            cfg.sources.append(source)
        if not cfg.sources:
            raise ValueError(f"{origin}: no sources configured")
        names = [s.name for s in cfg.sources]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"{origin}: duplicate source name(s): {', '.join(sorted(dupes))}")

        for key in (
            "target",
            "template",
            "request_timeout",
            "delay_between_requests",
            "delay_between_posts",
            "max_posts_per_run",
            "max_seen_per_source",
            "nitter_instances",
        ):
            if key in raw:
                setattr(cfg, key, raw[key])

        # Env wins over the config body, so a Railway variable can flip the
        # target to stdout without editing the config.
        env_target = os.environ.get("NEWSPOSTER_TARGET")
        if env_target:
            cfg.target = env_target
        env_max = os.environ.get("NEWSPOSTER_MAX_POSTS_PER_RUN")
        if env_max:
            cfg.max_posts_per_run = int(env_max)

        if cfg.target not in PUBLISHERS:
            raise ValueError(f"unknown target {cfg.target!r}; expected one of {', '.join(PUBLISHERS)}")
        return cfg


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class State:
    """Remembers which item ids have already been published, per source.

    A missing state file means a first run. Publishing every item a feed
    happens to be carrying at that moment is never what anyone wants, so the
    first run seeds state and posts nothing unless told otherwise.
    """

    def __init__(self, path: str, cap: int = 500):
        self.path = path
        self.cap = cap
        self.first_run = True
        self.seen: dict[str, list[str]] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.seen = json.load(fh).get("seen", {})
                self.first_run = False
            except (OSError, ValueError) as exc:
                log(f"warning: could not read state file {path}: {exc}; treating as a first run")

    def is_new(self, source: str, item_id: str) -> bool:
        return item_id not in self.seen.get(source, [])

    def mark(self, source: str, item_ids: list[str]) -> None:
        bucket = self.seen.setdefault(source, [])
        bucket.extend(i for i in item_ids if i not in bucket)
        if len(bucket) > self.cap:
            del bucket[: len(bucket) - self.cap]

    def save(self) -> None:
        if not self.path:
            return
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        tmp = f"{self.path}.tmp"
        payload = {"updated": datetime.now(timezone.utc).isoformat(), "seen": self.seen}
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def http_get(url: str, timeout: float, attempts: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
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
            time.sleep(2 ** attempt)
    raise FeedError(str(last))


def http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise PublishError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", value or ""))).strip()


def to_iso(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return raw


def parse_feed(body: bytes, source_name: str) -> list[dict]:
    """Parse RSS 2.0 or Atom into the common item shape."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML ({exc})") from exc

    items: list[dict] = []
    channel = root.find("channel")
    if channel is not None:  # RSS 2.0
        for item in channel.findall("item"):
            link = (item.findtext("link") or "").strip()
            item_id = (item.findtext("guid") or link).strip()
            if not item_id:
                continue
            items.append(
                {
                    "source": source_name,
                    "id": item_id,
                    "title": strip_html(item.findtext("title") or ""),
                    "text": strip_html(item.findtext("description") or ""),
                    "url": link,
                    "author": strip_html(item.findtext("author") or ""),
                    "published": to_iso(item.findtext("pubDate") or ""),
                }
            )
        return items

    if root.tag == f"{ATOM_NS}feed":
        for entry in root.findall(f"{ATOM_NS}entry"):
            link = ""
            for candidate in entry.findall(f"{ATOM_NS}link"):
                rel = candidate.get("rel", "alternate")
                if rel == "alternate" and candidate.get("href"):
                    link = candidate.get("href", "")
                    break
            item_id = (entry.findtext(f"{ATOM_NS}id") or link or "").strip()
            if not item_id:
                continue
            summary = entry.findtext(f"{ATOM_NS}summary") or entry.findtext(f"{ATOM_NS}content") or ""
            author = entry.find(f"{ATOM_NS}author")
            items.append(
                {
                    "source": source_name,
                    "id": item_id,
                    "title": strip_html(entry.findtext(f"{ATOM_NS}title") or ""),
                    "text": strip_html(summary),
                    "url": link,
                    "author": strip_html(author.findtext(f"{ATOM_NS}name") or "") if author is not None else "",
                    "published": to_iso(
                        entry.findtext(f"{ATOM_NS}published") or entry.findtext(f"{ATOM_NS}updated") or ""
                    ),
                }
            )
        return items

    raise FeedError("neither an RSS <channel> nor an Atom <feed>")


def fetch_nitter(source: Source, cfg: Config) -> list[dict]:
    """Delegate to the sibling Nitter reader rather than re-implementing it."""
    here = os.path.dirname(os.path.abspath(__file__))
    nitter_dir = os.path.join(os.path.dirname(here), "nitter")
    if nitter_dir not in sys.path:
        sys.path.insert(0, nitter_dir)
    try:
        import nitter_reader  # type: ignore
    except ImportError as exc:
        raise FeedError(f"tools/nitter/nitter_reader.py is not importable ({exc})") from exc

    entry = dict(source.nitter)
    entry.setdefault("name", source.name)
    try:
        nitter_source = nitter_reader.Source(**entry)
    except TypeError as exc:
        raise FeedError(f"bad 'nitter' block: {exc}") from exc

    nitter_cfg = nitter_reader.Config(
        instances=[i.rstrip("/") for i in (cfg.nitter_instances or nitter_reader.DEFAULT_INSTANCES)],
        sources=[nitter_source],
        request_timeout=cfg.request_timeout,
        delay_between_requests=cfg.delay_between_requests,
    )
    try:
        posts = nitter_reader.fetch_source(nitter_source, nitter_cfg, nitter_cfg.instances)
    except (nitter_reader.FeedError, ValueError) as exc:
        raise FeedError(str(exc)) from exc
    for post in posts:
        post["source"] = source.name
    return posts


def fetch_source(source: Source, cfg: Config) -> list[dict]:
    if source.type == "nitter":
        return fetch_nitter(source, cfg)
    body = http_get(source.url, cfg.request_timeout)
    items = parse_feed(body, source.name)
    log(f"  {source.name}: {len(items)} item(s)")
    return items


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class _Blanks(dict):
    def __missing__(self, key: str) -> str:
        return ""


def weighted_length(text: str, target: str) -> int:
    """Length as the target counts it — X bills every URL at t.co length."""
    if target != "x":
        return len(text)
    total = len(text)
    for url in URL_RE.findall(text):
        total += TCO_LENGTH - len(url)
    return total


def render(item: dict, template: str, target: str) -> str:
    text = template.format_map(_Blanks(item)).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    limit = 280 if target == "x" else 0
    if not limit or weighted_length(text, target) <= limit:
        return text

    # Too long: shrink the title/text substitution rather than the URL, which
    # is the part that has to survive intact.
    for key in ("title", "text"):
        value = (item.get(key) or "").strip()
        if not value or f"{{{key}}}" not in template:
            continue
        overflow = weighted_length(text, target) - limit
        keep = max(1, len(value) - overflow - 1)
        shortened = value[:keep].rstrip()
        shortened = shortened.rsplit(" ", 1)[0] if " " in shortened else shortened
        trimmed = dict(item)
        trimmed[key] = f"{shortened}…"
        text = template.format_map(_Blanks(trimmed)).strip()
        text = re.sub(r"[ \t]{2,}", " ", text)
        if weighted_length(text, target) <= limit:
            return text
    return text


# --------------------------------------------------------------------------- #
# Publishers
# --------------------------------------------------------------------------- #


def publish_stdout(text: str, item: dict, cfg: Config) -> str:
    print(json.dumps({"text": text, "item": item}, ensure_ascii=False))
    return "stdout"


def publish_webhook(text: str, item: dict, cfg: Config) -> str:
    url = os.environ.get("NEWSPOSTER_WEBHOOK_URL", "").strip()
    if not url:
        raise PublishError("NEWSPOSTER_WEBHOOK_URL is not set")
    host = urllib.parse.urlparse(url).netloc
    if "discord" in host:
        payload = {"content": text}
    elif "slack" in host:
        payload = {"text": text}
    else:
        payload = {"text": text, "item": item}
    status, _ = http_post_json(url, payload, {}, cfg.request_timeout)
    return f"webhook {status}"


def oauth1_header(method: str, url: str, creds: dict[str, str]) -> str:
    """OAuth 1.0a user-context header. The JSON body is not signed."""
    params = {
        "oauth_consumer_key": creds["consumer_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds["access_token"],
        "oauth_version": "1.0",
    }
    parsed = urllib.parse.urlparse(url)
    signing = dict(params)
    signing.update({k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()})
    encoded = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(signing.items())
    )
    base_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    base = "&".join(
        urllib.parse.quote(part, safe="")
        for part in (method.upper(), base_url, encoded)
    )
    key = f"{urllib.parse.quote(creds['consumer_secret'], safe='')}&{urllib.parse.quote(creds['access_token_secret'], safe='')}"
    signature = base64.b64encode(
        hmac.new(key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    params["oauth_signature"] = signature
    return "OAuth " + ", ".join(
        f'{urllib.parse.quote(k, safe="")}="{urllib.parse.quote(v, safe="")}"'
        for k, v in sorted(params.items())
    )


X_CREDENTIAL_ENV = {
    "consumer_key": "X_API_KEY",
    "consumer_secret": "X_API_SECRET",
    "access_token": "X_ACCESS_TOKEN",
    "access_token_secret": "X_ACCESS_TOKEN_SECRET",
}
X_TWEETS_URL = "https://api.x.com/2/tweets"


def x_credentials() -> dict[str, str]:
    creds = {name: os.environ.get(env, "").strip() for name, env in X_CREDENTIAL_ENV.items()}
    missing = [X_CREDENTIAL_ENV[name] for name, value in creds.items() if not value]
    if missing:
        raise PublishError(f"missing credential(s): {', '.join(sorted(missing))}")
    return creds


def publish_x(text: str, item: dict, cfg: Config) -> str:
    creds = x_credentials()
    header = oauth1_header("POST", X_TWEETS_URL, creds)
    status, body = http_post_json(
        X_TWEETS_URL, {"text": text}, {"Authorization": header}, cfg.request_timeout
    )
    try:
        tweet_id = json.loads(body).get("data", {}).get("id", "?")
    except ValueError:
        tweet_id = "?"
    return f"x {status} id={tweet_id}"


PUBLISHERS = {
    "stdout": publish_stdout,
    "webhook": publish_webhook,
    "x": publish_x,
}


def check_target(cfg: Config) -> list[str]:
    """Report what would stop the configured target from publishing."""
    if cfg.target == "webhook":
        if not os.environ.get("NEWSPOSTER_WEBHOOK_URL", "").strip():
            return ["NEWSPOSTER_WEBHOOK_URL is not set"]
    elif cfg.target == "x":
        try:
            x_credentials()
        except PublishError as exc:
            return [str(exc)]
    return []


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def check(cfg: Config, problems: list[str]) -> int:
    """Validate the target and read every source without publishing."""
    print(f"target: {cfg.target}")
    for problem in problems:
        print(f"  BLOCKED  {problem}")
    if not problems:
        print("  ready")
    print(f"sources: {len(cfg.sources)}")
    failures = 0
    for source in cfg.sources:
        try:
            items = fetch_source(source, cfg)
            newest = max((i["published"] or "" for i in items), default="") or "—"
            print(f"  OK    {source.name}  ({len(items)} items, newest {newest})")
        except (FeedError, ValueError) as exc:
            print(f"  DEAD  {source.name}  ({exc})")
            failures += 1
        time.sleep(cfg.delay_between_requests)
    return 1 if (problems or failures == len(cfg.sources)) else 0


def collect(cfg: Config, state: State, ignore_state: bool) -> tuple[list[dict], dict[str, list[str]], int]:
    """Fetch every source, returning the unpublished items oldest-first.

    Nothing is marked seen here: an item is only seen once it has actually
    been published, so a run that hits its post cap leaves the rest for the
    next run instead of silently dropping them.
    """
    collected: list[dict] = []
    fetched: dict[str, list[str]] = {}
    failures = 0
    for source in cfg.sources:
        try:
            items = fetch_source(source, cfg)
        except (FeedError, ValueError) as exc:
            log(f"  {source.name}: FAILED — {exc}")
            failures += 1
            continue
        fetched[source.name] = [i["id"] for i in items]
        fresh = [i for i in items if ignore_state or state.is_new(source.name, i["id"])]
        fresh.sort(key=lambda i: i["published"] or "")
        if source.max_per_run:
            fresh = fresh[-source.max_per_run :]
        for item in fresh:
            item["_template"] = source.template or cfg.template
        collected.extend(fresh)
        time.sleep(cfg.delay_between_requests)
    collected.sort(key=lambda i: i["published"] or "")
    return collected, fetched, failures


def run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config, os.environ.get("NEWSPOSTER_CONFIG_JSON"))
    problems = check_target(cfg)

    if args.check:
        return check(cfg, problems)

    if problems and not args.dry_run:
        for problem in problems:
            log(f"error: {problem}")
        return 2

    state = State(args.state, cap=cfg.max_seen_per_source)
    seeding = state.first_run and not args.post_on_first_run and not args.all
    if seeding:
        log("first run: seeding state, publishing nothing (--post-on-first-run overrides)")

    items, fetched, failures = collect(cfg, state, ignore_state=args.all)

    if seeding:
        for source_name, ids in fetched.items():
            state.mark(source_name, ids)
        state.save()
        log(f"seeded {sum(len(v) for v in fetched.values())} item(s); the next run posts what arrives after them")
        return 1 if failures == len(cfg.sources) else 0

    limit = args.limit or cfg.max_posts_per_run
    held = max(0, len(items) - limit) if limit else 0
    if limit:
        items = items[:limit]

    publisher = PUBLISHERS[cfg.target]
    published = 0
    errors = 0
    for index, item in enumerate(items):
        text = render(item, item.pop("_template"), cfg.target)
        if args.dry_run:
            print(f"[dry-run] {text}")
            published += 1
            continue
        try:
            result = publisher(text, item, cfg)
        except PublishError as exc:
            # Not marked seen, so the next run retries it.
            log(f"  FAILED to post {item['id']}: {exc}")
            errors += 1
            continue
        log(f"  posted ({result}): {text[:80]}")
        state.mark(item["source"], [item["id"]])
        published += 1
        if index < len(items) - 1 and cfg.delay_between_posts:
            time.sleep(cfg.delay_between_posts)

    if not args.all and not args.dry_run:
        state.save()

    holding = f", {held} held for the next run" if held else ""
    log(f"{published} post(s) published, {errors} publish error(s), {failures} source(s) failed{holding}")
    if failures == len(cfg.sources):
        return 1
    return 1 if errors else 0


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("NEWSPOSTER_CONFIG", os.path.join(here, "feeds.json")),
        help="config file (ignored when NEWSPOSTER_CONFIG_JSON is set)",
    )
    parser.add_argument(
        "--state",
        default=os.environ.get("NEWSPOSTER_STATE", os.path.join(here, "state.json")),
        help="path to the already-posted state file",
    )
    parser.add_argument("--check", action="store_true", help="validate config, credentials and feeds, then exit")
    parser.add_argument("--dry-run", action="store_true", help="render posts to stdout without publishing or saving state")
    parser.add_argument("--all", action="store_true", help="ignore state and leave it untouched (backfills, testing)")
    parser.add_argument("--limit", type=int, default=0, help="publish at most N posts this run (overrides max_posts_per_run)")
    parser.add_argument("--post-on-first-run", action="store_true", help="publish on a run with no state file instead of seeding")
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
