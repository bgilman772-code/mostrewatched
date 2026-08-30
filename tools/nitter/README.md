# Nitter reader

Read-only fetcher for Twitter/X posts, backed by [Nitter](https://github.com/zedeus/nitter)
RSS feeds. No X developer account, no API key, no OAuth — Nitter exposes every
profile and search as RSS, and this script polls those feeds and emits the posts
it hasn't seen before.

This reads only. Nitter has no posting capability, so nothing here can publish.

## Setup

```bash
cp feeds.example.json feeds.json
$EDITOR feeds.json
./nitter_reader.py --check      # are the configured instances alive?
./nitter_reader.py              # emit new posts as JSON lines
```

Python 3.9+, standard library only — no pip install.

## Config

`feeds.json` holds the instances to try and the sources to read:

```json
{
  "instances": ["https://nitter.net", "https://xcancel.com"],
  "sources": [
    {"name": "clips", "type": "user", "handle": "someone", "media_only": true},
    {"name": "mentions", "type": "search", "query": "\"most rewatched\" filter:videos"}
  ]
}
```

Source keys:

| key | applies to | meaning |
| --- | --- | --- |
| `name` | both | label for output and state tracking; must be unique |
| `type` | both | `user` or `search` |
| `handle` | `user` | account to read, with or without the leading `@` |
| `include_replies` | `user` | read `/with_replies` instead of the plain timeline |
| `media_only` | `user` | read `/media` — only posts carrying images or video |
| `query` | `search` | any query X search accepts, e.g. `from:someone filter:videos` |

Optional top-level keys: `request_timeout` (seconds, default 20),
`delay_between_requests` (seconds, default 2 — be polite, instances are
volunteer-run), `max_seen_per_source` (default 500).

## Output

One JSON object per line:

```json
{"source": "clips", "id": "…/status/1#m", "author": "@someone",
 "title": "…", "text": "…", "published": "2026-08-30T12:00:00+00:00",
 "url": "https://x.com/someone/status/1", "nitter_url": "…",
 "media": ["https://pbs.twimg.com/media/AAA.jpg"], "instance": "https://nitter.net"}
```

Two details worth knowing:

- `url` is rewritten to `x.com` so the record stays meaningful after the
  instance that served it disappears. `nitter_url` keeps the original.
- `media` URLs are un-proxied back to `pbs.twimg.com` / `video.twimg.com`.
  Nitter serves media through its own `/pic/` path, which dies with the
  instance; the twimg originals keep resolving.

Use `--format text` for a readable dump, `--limit N` to cap output.

## State

Post ids already emitted are recorded in `state.json` (alongside the script,
override with `--state`), so repeated runs only surface new posts. `--all`
ignores the state file and leaves it untouched — useful for backfills and for
testing a new source without burning its history.

`feeds.json` and `state.json` are gitignored; only `feeds.example.json` is
checked in.

## Instance failover

Public instances rate-limit, break, and vanish. Every request walks the
`instances` list in order and the first one that returns parseable RSS wins; a
source only fails once all of them have. `--shuffle` randomises the order to
spread load across instances instead of always hammering the first.

Exit codes: `0` normal, `1` if every source failed (or, with `--check`, if no
instance is usable), `2` on a config or filesystem error.

## Operating notes

Nitter is a fragile dependency, and knowingly so:

- On 24 August 2026 X Corp. sent cease-and-desist letters demanding takedown of
  Nitter instances and the project repository. Public instances may disappear.
- Self-hosting is the durable option but is no longer credential-free: the
  docker-compose setup mounts a `sessions.jsonl` seeded with tokens from real X
  accounts. That is against X's terms and the accounts do get banned.
- Feeds return only recent posts — this is a poller, not a backfill tool. Run it
  often enough that nothing falls off the end of the feed between runs.

Treat a dead-instance day as expected, not as a bug: `--check` first, and keep
the `instances` list longer than you think you need.

## Scheduling

Any cron-shaped thing works, since the script is stateful across runs:

```cron
*/30 * * * * cd /path/to/tools/nitter && ./nitter_reader.py >> posts.jsonl 2>> reader.log
```
