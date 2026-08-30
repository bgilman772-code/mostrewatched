#!/usr/bin/env python3
"""DC / Navy Yard news backend: ingest local news, filter it to the
neighborhood, render cards, and hand them to the posting pipeline.

This does not post anything itself. It stops at a manifest — rendered card
plus per-target caption text — which Social-Clipper's existing Instagram and
TikTok publishing consumes. Report results back with `mark-posted` so the
queue reflects what actually went out.

Items move: pending -> approved -> exported -> posted (or rejected / failed).

    ./dcnews.py fetch                 # pull feeds, score, queue what's relevant
    ./dcnews.py queue                 # review what's waiting
    ./dcnews.py approve <id> [<id>…]  # or: approve --navy-yard --top 3
    ./dcnews.py render                # 1080x1350 card per approved item
    ./dcnews.py export                # write manifest.json for the publisher
    ./dcnews.py mark-posted --from results.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import captions
import feeds
import relevance
from render import RenderError, render_card
from store import TARGETS, Item, Store, now_iso

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
    cfg.setdefault("export", {})
    unknown = [t for t in cfg["export"].get("targets", []) if t not in TARGETS]
    if unknown:
        raise ValueError(f"{path}: unknown export target(s): {', '.join(unknown)}")
    return cfg


def resolve_path(cfg_path: str, value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(os.path.dirname(os.path.abspath(cfg_path)), value)


# --------------------------------------------------------------------------- #
# Ingest
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
            for result in store.results_for(row["id"]):
                detail = result["permalink"] or result["error"] or ""
                print(f"    {result['target']}: {result['status']} {detail}")
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


# --------------------------------------------------------------------------- #
# Handoff to the posting pipeline
# --------------------------------------------------------------------------- #


def _export_gate(cfg, store: Store) -> tuple[bool, str]:
    """Cap on how much is handed to the publisher per rolling 24 hours."""
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    exported = store.exported_since(day_ago)
    cap = cfg["export"].get("max_per_day", 6)
    if exported >= cap:
        return False, f"daily cap reached ({exported}/{cap} exported in the last 24h)"
    return True, f"{exported}/{cap} exported in the last 24h"


def build_manifest_entry(row, cfg, targets: list[str], base_url: str) -> dict:
    extra_tags = cfg["export"].get("hashtags", [])
    card_url = f"{base_url}/{os.path.basename(row['card_path'])}" if base_url else ""
    entry = {
        "id": row["id"],
        "tier": row["tier"],
        "score": row["score"],
        "source": row["source"],
        "headline": row["title"],
        "summary": row["summary"] or "",
        "article_url": row["url"],
        "published": row["published"],
        "card": {
            "path": row["card_path"],
            "url": card_url,
            "width": 1080,
            "height": 1350,
        },
        "targets": {},
    }
    if "instagram" in targets:
        entry["targets"]["instagram"] = {
            "caption": captions.instagram_caption(
                row["title"], row["source"], row["url"], row["tier"], extra_tags
            ),
        }
    if "tiktok" in targets:
        entry["targets"]["tiktok"] = captions.tiktok_post(
            row["title"], row["source"], row["url"], row["tier"], extra_tags
        )
    return entry


def cmd_export(args, cfg, store: Store) -> int:
    """Write the manifest the posting pipeline reads."""
    targets = args.targets.split(",") if args.targets else cfg["export"].get("targets", list(TARGETS))
    targets = [t.strip() for t in targets if t.strip()]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        log(f"error: unknown target(s): {', '.join(unknown)} (known: {', '.join(TARGETS)})")
        return 2

    base_url = (args.card_base_url or cfg.get("card_base_url", "")).rstrip("/")
    if not base_url:
        log("note: card_base_url is unset, so manifest entries carry a local path only —")
        log("      both platforms fetch media over the public internet when pulling from a URL")

    ok, note = _export_gate(cfg, store)
    print(f"throttle: {note}")
    if not ok:
        return 0

    rows = [r for r in store.list(status="approved", limit=args.limit) if r["card_path"]]
    missing = [r for r in rows if not os.path.exists(r["card_path"])]
    for row in missing:
        log(f"  {row['id']}: card file is missing at {row['card_path']} — run `render --force`")
    rows = [r for r in rows if os.path.exists(r["card_path"])]

    if not rows:
        print("nothing approved and rendered to export")
        return 0

    entries = [build_manifest_entry(row, cfg, targets, base_url) for row in rows]
    manifest = {
        "generated": now_iso(),
        "targets": targets,
        "count": len(entries),
        "items": entries,
    }

    if args.stdout:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
    else:
        out_path = resolve_path(args.config, args.out or cfg["export"].get("manifest", "manifest.json"))
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            if args.format == "jsonl":
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            else:
                json.dump(manifest, fh, indent=2, ensure_ascii=False)
        print(f"wrote {out_path}")

    if not args.dry_run:
        for row in rows:
            store.set_status(row["id"], "exported", exported_at=now_iso())
    print(f"{len(entries)} item(s) exported for {', '.join(targets)}"
          + (" (dry run — queue unchanged)" if args.dry_run else ""))
    return 0


def cmd_mark_posted(args, cfg, store: Store) -> int:
    """Record what the posting pipeline actually did with each item."""
    updates: list[dict] = []
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        try:
            data = json.loads(text) if text.startswith(("[", "{")) else None
        except ValueError as exc:
            log(f"error: {args.from_file} is not valid JSON ({exc})")
            return 2
        if data is None:  # JSON Lines
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(data, dict):
            data = data.get("results", data.get("items", []))
        updates.extend(data)
    for item_id in args.ids:
        updates.append({
            "id": item_id,
            "target": args.target or "instagram",
            "status": "failed" if args.failed else "posted",
            "permalink": args.permalink,
            "error": args.error,
        })

    if not updates:
        print("nothing to record — pass ids or --from results.json")
        return 0

    recorded = skipped = 0
    for update in updates:
        item_id = update.get("id") or update.get("item_id")
        if not item_id or not store.get(item_id):
            log(f"  unknown id {item_id!r} — skipped")
            skipped += 1
            continue
        target = update.get("target", "instagram")
        if target not in TARGETS:
            log(f"  {item_id}: unknown target {target!r} — skipped")
            skipped += 1
            continue
        try:
            store.record_result(
                item_id, target,
                status=update.get("status", "posted"),
                permalink=update.get("permalink") or "",
                error=update.get("error") or "",
            )
        except ValueError as exc:
            log(f"  {item_id}: {exc}")
            skipped += 1
            continue
        recorded += 1
        print(f"{item_id} {target}: {update.get('status', 'posted')}")
    print(f"\n{recorded} result(s) recorded, {skipped} skipped")
    return 0


# --------------------------------------------------------------------------- #


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
    _, note = _export_gate(cfg, store)
    print("throttle:", note)
    rows = store.conn.execute(
        "SELECT target, status, COUNT(*) n FROM results GROUP BY target, status"
    ).fetchall()
    if rows:
        print("results:", ", ".join(f"{r['target']}/{r['status']}={r['n']}" for r in rows))
    feed_rows = store.conn.execute(
        "SELECT url, last_fetch, last_status FROM feed_state ORDER BY last_fetch DESC"
    ).fetchall()
    for row in feed_rows:
        print(f"  {row['last_fetch']}  {row['last_status'][:40]:<42} {row['url']}")
    return 0


def cmd_run(args, cfg, store: Store) -> int:
    """fetch -> auto-approve (optional) -> render -> export."""
    cmd_fetch(args, cfg, store)
    if cfg["export"].get("auto_approve_navy_yard"):
        for row in store.list(status="pending", limit=50):
            if row["tier"] == "navy_yard":
                store.set_status(row["id"], "approved")
    cmd_render(args, cfg, store)
    return cmd_export(args, cfg, store)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="pull feeds and queue relevant items")
    p.add_argument("--force", action="store_true", help="ignore cached ETag/Last-Modified")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("queue", help="list queued items")
    p.add_argument("--status", default="pending", help="pending, approved, exported, posted, rejected, failed")
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

    p = sub.add_parser("export", help="write the manifest for the posting pipeline")
    p.add_argument("--out", default="", help="manifest path (default: manifest.json beside the config)")
    p.add_argument("--targets", default="", help="comma-separated: instagram,tiktok")
    p.add_argument("--format", choices=["json", "jsonl"], default="json")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--stdout", action="store_true", help="print the manifest instead of writing a file")
    p.add_argument("--dry-run", action="store_true", help="build the manifest without marking items exported")
    p.add_argument("--card-base-url", default="", help="override the public base URL for cards")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("mark-posted", help="record results reported by the posting pipeline")
    p.add_argument("ids", nargs="*")
    p.add_argument("--from", dest="from_file", default="", help="results file (JSON or JSON Lines)")
    p.add_argument("--target", default="", help="instagram or tiktok (default: instagram)")
    p.add_argument("--permalink", default="")
    p.add_argument("--failed", action="store_true", help="record a failure instead of a success")
    p.add_argument("--error", default="")
    p.set_defaults(func=cmd_mark_posted)

    p = sub.add_parser("check-feeds", help="verify every configured feed responds")
    p.set_defaults(func=cmd_check_feeds)

    p = sub.add_parser("status", help="queue counts, throttle, results, and feed health")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("run", help="fetch, render, and export in one pass")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--out", default="")
    p.add_argument("--targets", default="")
    p.add_argument("--format", choices=["json", "jsonl"], default="json")
    p.add_argument("--stdout", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--card-base-url", default="")
    # `run` delegates to cmd_render/cmd_export, which read these.
    p.set_defaults(func=cmd_run, ids=[])

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
