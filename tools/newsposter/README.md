# News poster

Fires news posts on a schedule. One run reads the configured feeds, drops
anything already posted, renders what's left through a template, publishes it,
records what went out, and exits — which is exactly the shape a Railway cron
service wants.

Python 3.9+, standard library only — no pip install.

```bash
cp feeds.example.json feeds.json
$EDITOR feeds.json
./newsposter.py --check      # config, credentials and feeds — publishes nothing
./newsposter.py --dry-run    # render the posts that would go out
./newsposter.py              # publish
```

## Sources

Two source types, mixed freely in one config:

| type | reads |
| --- | --- |
| `rss` | any RSS 2.0 or Atom URL |
| `nitter` | X/Twitter, via the sibling [`tools/nitter`](../nitter) reader |

A `nitter` source carries the source entry `tools/nitter` expects under its
`nitter` key (`type`, `handle`, `query`, `media_only`, `include_replies`), and
the instances to try come from the top-level `nitter_instances`. Nitter is a
fragile dependency for the reasons its own README sets out; RSS sources keep
working when it isn't.

Per-source keys: `name` (unique — it keys the state file), `type`, `url`
(`rss`), `nitter` (`nitter`), `template`, `max_per_run`.

Top-level keys: `target`, `template`, `max_posts_per_run` (default 5),
`delay_between_posts` (default 5s), `delay_between_requests` (default 2s),
`request_timeout` (default 20s), `max_seen_per_source` (default 500),
`nitter_instances`.

## Targets

`target` picks where posts go. Credentials always come from the environment,
never from the config file:

| target | env | notes |
| --- | --- | --- |
| `stdout` | — | default; prints JSON lines, publishes nothing |
| `webhook` | `NEWSPOSTER_WEBHOOK_URL` | Discord and Slack payload shapes are picked from the URL host; anything else gets `{"text": …, "item": {…}}` |
| `x` | `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET` | posts to `POST /2/tweets`, OAuth 1.0a user context |

`NEWSPOSTER_TARGET` overrides the config's `target`, so a Railway variable can
drop a live service back to `stdout` without a redeploy.

## Templates

`template` is a format string over the item fields — `{title}`, `{text}`,
`{url}`, `{author}`, `{published}`, `{source}`. A missing field renders empty
rather than raising. Default: `{title} {url}`.

For the `x` target the rendered post is held to 280 characters, counting every
URL as the 23 characters t.co bills it at. Overflow is taken out of `{title}`
or `{text}` — never out of the URL, which has to arrive intact.

## State, and the first run

Published item ids live in `state.json` (`--state`, or `NEWSPOSTER_STATE`).
Only ids that were *actually published* are recorded, so a run that hits
`max_posts_per_run` leaves the rest for the next run instead of dropping them,
and a publish that fails is retried rather than lost. The backlog drains
oldest-first, so a burst of items goes out in the order it happened.

**A run with no state file publishes nothing.** It seeds state from whatever
the feeds are carrying and says so; the next run posts what arrives after that.
Otherwise standing up a new feed would fire off everything in it at once.
`--post-on-first-run` overrides this, `--all` ignores state entirely and leaves
it untouched (backfills, testing a new source).

On Railway this file must live on a volume — a container filesystem is
discarded between cron runs, so state on it means a first run every run.

## Deploying to Railway

1. **New service → GitHub repo**, this repository.
2. **Settings → Root Directory**: `tools/newsposter`. `railway.json` there sets
   the build, the start command, and the schedule.
3. **Settings → Cron Schedule**: `*/30 * * * *` (already in `railway.json`;
   Railway runs the service to completion and does not restart it).
4. **Variables**:
   - `NEWSPOSTER_CONFIG_JSON` — the whole config, inline. A cron container has
     no writable checkout to keep a `feeds.json` in, so this is the form that
     gets used in production; it takes precedence over any config file.
   - `NEWSPOSTER_STATE` — `/data/state.json`.
   - `NEWSPOSTER_TARGET` and the credentials for it.
5. **Volume**: mount one at `/data`, matching `NEWSPOSTER_STATE`.

Set `NEWSPOSTER_TARGET=stdout` for the first deploy and read the logs before
pointing it at anything that publishes.

Pick the schedule to fit the feeds: an item that falls off the end of a feed
between two runs is never seen, and never posted.

## Exit codes

`0` normal, `1` if every source failed or a publish failed, `2` on a config,
credential, or filesystem error. Railway marks a cron run failed on a non-zero
exit, so `1` and `2` both surface in the service's run history.
