# DC / Navy Yard news backend

Ingests local news feeds, filters them down to Washington DC — and especially
the Navy Yard / Near Southeast neighborhood — renders each story as a 1080x1350
card, and publishes it to Instagram through the official Content Publishing API.

Standard library only. No pip install. Card rendering shells out to Chrome or
Chromium, which is the one external thing it needs.

## Pipeline

```
feeds ──▶ relevance scoring ──▶ queue ──▶ approve ──▶ render card ──▶ publish
                (drops                  (SQLite)   (you, or auto)  (Instagram
             non-local news)                                       Graph API)
```

Items move `pending -> approved -> posted`, or `rejected`. Nothing posts
without an explicit `--live` flag, so a misconfigured run costs nothing.

## Setup

```bash
cp config.example.json config.json
./dcnews.py check-feeds      # prune whatever no longer responds
./dcnews.py fetch            # pull feeds, score, queue what's local
./dcnews.py queue -v         # review
./dcnews.py approve <id>     # or: approve --navy-yard
./dcnews.py render           # 1080x1350 PNG per approved item
./dcnews.py publish          # dry run — prints image URL and caption
./dcnews.py publish --live   # actually posts
```

`./dcnews.py run` does fetch → render → publish in one pass, for cron.
`./dcnews.py status` shows queue counts, the posting throttle, and feed health.

Run the tests with `python3 test_dcnews.py`.

## What counts as Navy Yard news

`relevance.py` scores each story's title and summary against three weighted
tiers, and the title counts double — a place named in the headline is the story.

- **core** (marks an item `navy_yard`): Navy Yard, Capitol Riverfront, Yards
  Park, Canal Park, The Yards, Nationals Park, Audi Field, Buzzard Point,
  Navy Yard-Ballpark, ANC 6D, Half/M/N/Tingey/Water Street SE, 20003…
- **adjacent**: Capitol Hill, Barracks Row, Eastern Market, The Wharf,
  Southwest Waterfront, Ward 6, the Nationals, DC United…
- **citywide**: DC Council, Mayor Bowser, WMATA, DDOT, MPD, DCPS…

Topic words (shooting, road closure, opening, festival, water main…) add weight
once a story is already local, so the neighborhood-relevant version of a story
outranks the generic one.

Two things it deliberately throws away:

- **Other cities' navy yards.** Brooklyn, Philadelphia, Boston, Charlestown,
  Portsmouth, Norfolk. A story matching those with no DC signal is dropped.
- **Other Washingtons.** Washington state, Washington County, Washington
  University, Seattle datelines.

Matching is on word boundaries, so "M Street SE" doesn't fire on "farm street".

Tune it without touching code via `config.json`:

```json
"relevance": {
  "navy_yard_threshold": 8,
  "dc_threshold": 6,
  "extra_core": {"the bullpen": 8, "my block": 10},
  "exclude": ["sponsored", "advertisement"]
}
```

Raise `dc_threshold` to post less citywide news; lower it to post more.

## Feeds

`config.example.json` ships with the main DC outlets (WTOP, PoPville, Washington
City Paper, The DC Line, Hill Rag, NBC4, WUSA9, Axios DC). **Verify them before
relying on the list** — feed URLs rot, and these were not reachable from the
environment this was built in:

```bash
./dcnews.py check-feeds
```

Drop whatever reports DEAD and add your own. Both RSS 2.0 and Atom work.
Fetches send `If-None-Match`/`If-Modified-Since`, so a feed that hasn't changed
costs one 304 rather than a re-parse.

Articles are deduplicated by canonical URL — tracking parameters, `www.`, and
trailing slashes are stripped first, so the same story syndicated to two feeds
is queued once.

## Cards

1080x1350 (4:5 portrait — the most feed space Instagram allows). Navy Yard items
get a gold badge, citywide items a blue one. The card carries the headline, a
summary line, the source name, and the date.

Rendering shells out to headless Chrome rather than using Playwright, so there
are no Python dependencies. Set `CHROME_BIN`, or `"browser"` in config, to
choose a binary; otherwise common install paths are tried.

One quirk worth knowing, since it caused a real bug: with a full Chrome binary
the viewport is ~87px shorter than `--window-size`, and content laid out past
the fold silently fails to render — footers come out clipped. Cards are
therefore rendered with vertical slack and cropped back to exactly 1080x1350 by
`pngtools.py`, a small stdlib PNG cropper.

## Publishing

Instagram's Content Publishing API is two calls plus a wait: create a media
container pointing at an image URL, poll until Instagram has downloaded it, then
publish the container.

**Instagram fetches the image itself, so `card_base_url` must be a public
HTTPS URL** — a local path will fail. The simplest option is to publish the
`cards/` directory to the same static host that already serves this repo, and
point `card_base_url` at it.

Requirements:

- An Instagram professional account (Business or Creator).
- A Meta app with the content publishing permission, and an access token.
- `graph.facebook.com` (account linked to a Facebook Page) or
  `graph.instagram.com` (Instagram Login, no Page) — set `publish.api_base`.

Credentials come from the environment, never the config file:

```bash
export IG_USER_ID=17841400000000000
export IG_ACCESS_TOKEN=EAAG...
./dcnews.py publish --live
```

Two throttles apply. Instagram allows 25 API-published posts per rolling 24
hours; `publish.max_per_day` (default 6) is the local cap, checked against
posts actually recorded in the database, and publishing stops as soon as it is
reached. Check the API's own count with `status`.

Captions are built as headline, source attribution, article URL, then hashtags
(Navy Yard items get neighborhood tags automatically), capped at Instagram's
2200 characters with attribution and tags preserved over headline text.

## Automating it

```cron
0 */3 * * * cd /path/to/tools/dcnews && ./dcnews.py run >> run.log 2>&1
```

That queues and renders but stops short of posting. To post unattended, set
both flags in config — and understand what you are turning on, because nothing
reads the story before it goes out:

```json
"publish": { "auto": true, "auto_approve_navy_yard": true, "max_per_day": 4 }
```

A safer middle ground is `auto_approve_navy_yard` with `auto: false`: cards get
rendered automatically and wait for you to run `publish --live`.

## Editorial and legal notes

Worth being deliberate about, since this republishes other people's reporting:

- Cards use **your own text rendering of a headline plus attribution** — no
  publisher photographs are copied. Keep it that way; reposting a news outlet's
  images is a copyright problem that aggregation of headlines largely is not.
- Every card and caption names the source and links the original. Don't remove
  the attribution.
- Headlines are reproduced verbatim from the feed. If you edit one for length,
  make sure the edit doesn't change its meaning.
- Automated posting means errors publish themselves. `max_per_day` and the
  review queue exist for that reason.

## Files

| file | role |
| --- | --- |
| `dcnews.py` | CLI: fetch, queue, approve, render, publish, run, status, check-feeds |
| `feeds.py` | RSS/Atom fetching and parsing, conditional GET |
| `relevance.py` | DC and Navy Yard scoring, false-friend rejection |
| `store.py` | SQLite queue, URL canonicalization, feed state |
| `render.py` | HTML card template, headless Chrome rendering |
| `pngtools.py` | stdlib PNG cropping |
| `instagram.py` | Content Publishing API client, caption building |
| `test_dcnews.py` | test suite (`python3 test_dcnews.py`) |

`config.json`, `dcnews.db`, and `cards/` are gitignored.
