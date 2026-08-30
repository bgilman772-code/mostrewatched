#!/usr/bin/env python3
"""DC / Navy Yard news backend: ingest local news, filter it to the
neighborhood, render cards, and publish them to Instagram.

Items move through a queue: pending -> approved -> posted (or rejected).
Nothing is published without an explicit --live flag, so a misconfigured run
costs nothing.

    ./dcnews.py fetch                 # pull feeds, score, queue what's relevant
    ./dcnews.py queue                 # review what's waiting
    ./dcnews.py approve <id> [<id>…]  # or: approve --navy-yard --top 3
    ./dcnews.py render                # make cards for approved items
    ./dcnews.py publish               # dry run; add --live to actually post
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import feeds
import instagram
import relevance
from render import RenderError, render_card
from store import Item, Store, now_iso

HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not cfg.get("feeds"):
        raise ValueError(f"{path}: no feeds configured")
    for key, default in (
        ("store", os.path.join(HERE, "dcnews.db")),
        ("cards_dir", os.path.join(HERE, "cards")),
        ("card_base_url", ""),
        ("handle", ""),
    ):
        cfg.setdefault(key, default)
    cfg.setdefault("relevance", {})
    cfg.setdefault("publish", {})
    return cfg


def resolve_path(cfg_path: str, value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(os.path.dirname(os.path.abspath(cfg_path)), value)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_fetch(args, cfg, store: Store) -> int:
    rel = cfg["relevance"]
    added = skipped = failed = 0
    for feed in cfg["feeds"]:
        name, url = feed["name"], feed["url"]
        try:
            headers = {} if args.force else store.feed_headers(url)
            body, resp_headers = feeds.fetch(url, timeout=cfg.get("request_timeout", 20.0), extra_headers=headers)
        except feeds.NotModified:
            store.save_feed_state(url, None, None, "304")
            log(f"  {name}: unchanged since last fetch")
            continue
        except feeds.FeedError as exc:
            log(f"  {name}: FAILED — {exc}")
            store.save_feed_state(url, None, None, f"error: {exc}")
            failed += 1
            continue

        try:
            entries = feeds.parse(body, name)
        except feeds.FeedError as exc:
            log(f"  {name}: FAILED — {exc}")
            failed += 1
            continue

        store.save_feed_state(url, resp_headers.get("ETag"), resp_headers.get("Last-Modified"), "200")

        kept = 0
        for entry in entries:
            verdict = relevance.score(
                entry.title,
                entry.summary,
                navy_yard_threshold=rel.get("navy_yard_threshold", 8),
                dc_threshold=rel.get("dc_threshold", 6),
                extra_core=rel.get("extra_core"),
                extra_exclude=tuple(rel.get("exclude", [])),
            )
            if not verdict.relevant:
                skipped += 1
                continue
            item = Item(
                source=entry.source, title=entry.title, url=entry.url,
                summary=entry.summary, published=entry.published,
                score=verdict.score, tier=verdict.tier, matched=verdict.matched,
            )
            if store.add(item):
                added += 1
                kept += 1
        log(f"  {name}: {len(entries)} entries, {kept} queued")

    print(f"{added} new item(s) queued, {skipped} not local enough, {failed} feed(s) failed")
    return 1 if failed == len(cfg["feeds"]) else 0


def cmd_queue(args, cfg, store: Store) -> int:
    rows = store.list(status=args.status, limit=args.limit, min_score=args.min_score)
    if not rows:
        print(f"nothing with status {args.status!r}")
        return 0
    for row in rows:
        tier = "NAVY YARD" if row["tier"] == "navy_yard" else "DC"
        when = (row["published"] or row["fetched"] or "")[:16].replace("T", " ")
        print(f"{row['id']}  [{tier:^9}] score {row['score']:>3}  {when}  {row['source']}")
        print(f"    {row['title'][:96]}")
        if args.verbose:
            print(f"    matched: {', '.join(json.loads(row['matched'] or '[]')[:8])}")
            print(f"    {row['url']}")
    print(f"\n{len(rows)} item(s); counts: {store.counts()}")
    return 0


def cmd_approve(args, cfg, store: Store) -> int:
    ids = list(args.ids)
    if args.navy_yard or args.top:
        rows = store.list(status="pending", limit=args.top or 10)
        picked = [r["id"] for r in rows if not args.navy_yard or r["tier"] == "navy_yard"]
        ids.extend(picked[: args.top] if args.top else picked)
    if not ids:
        print("nothing to approve")
        return 0
    status = "rejected" if args.reject else "approved"
    for item_id in ids:
        if not store.get(item_id):
            log(f"  unknown id {item_id}")
            continue
        store.set_status(item_id, status)
        print(f"{status}: {item_id}")
    return 0


def cmd_render(args, cfg, store: Store) -> int:
    cards_dir = resolve_path(args.config, cfg["cards_dir"])
    rows = [store.get(i) for i in args.ids] if args.ids else store.list(status="approved", limit=args.limit)
    rows = [r for r in rows if r]
    if not rows:
        print("nothing approved to render")
        return 0
    made = failed = 0
    for row in rows:
        if row["card_path"] and os.path.exists(row["card_path"]) and not args.force:
            continue
        out = os.path.join(cards_dir, f"{row['id']}.png")
        try:
            render_card(
                headline=row["title"], source=row["source"], out_path=out,
                published=row["published"], summary=row["summary"] or "",
                tier=row["tier"], handle=cfg.get("handle", ""),
                browser=cfg.get("browser", ""),
            )
        except RenderError as exc:
            log(f"  {row['id']}: render failed — {exc}")
            store.set_status(row["id"], "failed", error=str(exc))
            failed += 1
            continue
        store.set_status(row["id"], "approved", card_path=out, error=None)
        print(f"rendered {out}")
        made += 1
    print(f"{made} card(s) rendered, {failed} failed")
    return 1 if failed and not made else 0


def _publish_gate(cfg, store: Store) -> tuple[bool, str]:
    """Local throttle, checked before the API's own 25-per-24h limit."""
    pub = cfg["publish"]
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    posted = store.posted_since(day_ago)
    cap = pub.get("max_per_day", 6)
    if posted >= cap:
        return False, f"daily cap reached ({posted}/{cap} in the last 24h)"
    return True, f"{posted}/{cap} posted in the last 24h"


def cmd_publish(args, cfg, store: Store) -> int:
    pub = cfg["publish"]
    base_url = (args.card_base_url or cfg.get("card_base_url", "")).rstrip("/")
    live = args.live

    if live and not base_url:
        log("error: card_base_url must be set — Instagram fetches the image over the public internet")
        return 2

    # Configuration problems are reported before the throttle, so a bad setup
    # never hides behind "daily cap reached".
    token = os.environ.get(pub.get("access_token_env", "IG_ACCESS_TOKEN"), "")
    user_id = os.environ.get(pub.get("ig_user_id_env", "IG_USER_ID"), "")
    if live and not (token and user_id):
        log(
            "error: set {} and {} in the environment before --live".format(
                pub.get("ig_user_id_env", "IG_USER_ID"), pub.get("access_token_env", "IG_ACCESS_TOKEN")
            )
        )
        return 2

    ok, note = _publish_gate(cfg, store)
    print(f"throttle: {note}")
    if not ok and live:
        return 0

    publisher = instagram.InstagramPublisher(
        ig_user_id=user_id, access_token=token,
        api_base=pub.get("api_base", instagram.DEFAULT_API_BASE),
        api_version=pub.get("api_version", instagram.DEFAULT_API_VERSION),
        dry_run=not live,
    )

    rows = [r for r in store.list(status="approved", limit=args.limit) if r["card_path"]]
    if not rows:
        print("nothing approved and rendered to publish")
        return 0

    posted = 0
    for row in rows:
        caption = instagram.build_caption(
            headline=row["title"], source=row["source"], url=row["url"],
            tier=row["tier"], hashtags=pub.get("hashtags", []),
        )
        image_url = f"{base_url}/{os.path.basename(row['card_path'])}" if base_url else "(card_base_url unset)"
        if not live:
            print(f"\n--- DRY RUN {row['id']} ---")
            print(f"image: {image_url}")
            print(f"local: {row['card_path']}")
            print(caption)
            posted += 1
            continue
        try:
            result = publisher.publish(image_url, caption)
        except instagram.InstagramError as exc:
            log(f"  {row['id']}: publish failed — {exc}")
            store.set_status(row["id"], "failed", error=str(exc))
            continue
        store.set_status(row["id"], "posted", ig_media_id=result.media_id, permalink=result.permalink, error=None)
        print(f"posted {row['id']} -> {result.permalink or result.media_id}")
        posted += 1
        ok, note = _publish_gate(cfg, store)
        if not ok:
            print(f"stopping: {note}")
            break

    print(f"\n{posted} item(s) {'published' if live else 'shown (dry run — add --live to post)'}")
    return 0


def cmd_check_feeds(args, cfg, store: Store) -> int:
    alive = 0
    for feed in cfg["feeds"]:
        name, url = feed["name"], feed["url"]
        try:
            body, _ = feeds.fetch(url, timeout=cfg.get("request_timeout", 20.0), attempts=1)
            entries = feeds.parse(body, name)
            print(f"OK    {name:<28} {len(entries):>3} entries  {url}")
            alive += 1
        except (feeds.FeedError, feeds.NotModified) as exc:
            print(f"DEAD  {name:<28} {exc}  {url}")
    print(f"\n{alive}/{len(cfg['feeds'])} feed(s) usable")
    return 0 if alive else 1


def cmd_status(args, cfg, store: Store) -> int:
    counts = store.counts()
    print("queue:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty")
    ok, note = _publish_gate(cfg, store)
    print("throttle:", note)
    rows = store.conn.execute("SELECT url, last_fetch, last_status FROM feed_state ORDER BY last_fetch DESC").fetchall()
    for row in rows:
        print(f"  {row['last_fetch']}  {row['last_status'][:40]:<42} {row['url']}")
    return 0


def cmd_run(args, cfg, store: Store) -> int:
    """fetch -> auto-approve (optional) -> render -> publish."""
    cmd_fetch(args, cfg, store)
    if cfg["publish"].get("auto_approve_navy_yard"):
        for row in store.list(status="pending", limit=50):
            if row["tier"] == "navy_yard":
                store.set_status(row["id"], "approved")
    cmd_render(args, cfg, store)
    if cfg["publish"].get("auto") or args.live:
        return cmd_publish(args, cfg, store)
    print("\nauto-publish is off — review with `queue`, then `approve` and `publish`")
    return 0


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="pull feeds and queue relevant items")
    p.add_argument("--force", action="store_true", help="ignore cached ETag/Last-Modified")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("queue", help="list queued items")
    p.add_argument("--status", default="pending", help="pending, approved, posted, rejected, failed")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("approve", help="approve items for posting")
    p.add_argument("ids", nargs="*")
    p.add_argument("--navy-yard", action="store_true", help="approve pending Navy Yard items")
    p.add_argument("--top", type=int, default=0, help="approve the N highest-scoring pending items")
    p.add_argument("--reject", action="store_true", help="reject instead of approve")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("render", help="render cards for approved items")
    p.add_argument("ids", nargs="*")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--force", action="store_true", help="re-render even if a card exists")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("publish", help="publish rendered cards (dry run unless --live)")
    p.add_argument("--live", action="store_true", help="actually post to Instagram")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--card-base-url", default="", help="override the public base URL for cards")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("check-feeds", help="verify every configured feed responds")
    p.set_defaults(func=cmd_check_feeds)

    p = sub.add_parser("status", help="queue counts, throttle, and feed health")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("run", help="fetch, render, and optionally publish in one pass")
    p.add_argument("--live", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--card-base-url", default="")
    p.set_defaults(func=cmd_run)

    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
        db_path = resolve_path(args.config, cfg["store"])
        with Store(db_path) as store:
            return args.func(args, cfg, store)
    except (OSError, ValueError) as exc:
        log(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
