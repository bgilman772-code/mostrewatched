# DC / Navy Yard news backend

Ingests local news feeds, filters them to Washington DC — and especially the
Navy Yard / Near Southeast neighborhood — renders each story as a 1080x1350
card, and hands the result to your existing Social-Clipper posting pipeline.

**This does not post anything itself.** It stops at a manifest: a rendered card
plus per-target caption text for Instagram and TikTok. Your pipeline, which
already holds the credentials, does the posting and reports back.

Standard library only. No pip install. Card rendering shells out to Chrome or
Chromium, which is the one external thing it needs.

## Pipeline

```
feeds ─▶ relevance ─▶ queue ─▶ approve ─▶ render card ─▶ export manifest ─▶ │ your publisher │
          scoring    (SQLite)  (you, or                                     │  IG + TikTok   │
                                 auto)                                      └───────┬────────┘
                          ▲                                                         │
                          └────────────────── mark-posted ◀─── results.json ────────┘
```

Items move `pending -> approved -> exported -> posted`, or `rejected` /
`failed`. Feeding results back with `mark-posted` is what keeps the queue
honest: it drives the daily cap, stops re-posting, and records permalinks.

## Setup

```bash
cp config.example.json config.json
./dcnews.py check-feeds      # prune whatever no longer responds
./dcnews.py fetch            # pull feeds, score, queue what's local
./dcnews.py queue -v         # review
./dcnews.py approve --navy-yard
./dcnews.py render           # 1080x1350 PNG per approved item
./dcnews.py export           # write manifest.json
# …your pipeline posts them…
./dcnews.py mark-posted --from results.json
```

`./dcnews.py run` does fetch → render → export in one pass, for cron.
`./dcnews.py status` shows queue counts, the export throttle, per-target
results, and feed health. Tests: `python3 test_dcnews.py`.

## The handoff contract

`export` writes `manifest.json`:

```json
{
  "generated": "2026-08-30T23:23:00+00:00",
  "targets": ["instagram", "tiktok"],
  "count": 1,
  "items": [
    {
      "id": "351c2354c5c76113",
      "tier": "navy_yard",
      "score": 65,
      "source": "WTOP",
      "headline": "New rooftop bar opens on Half Street SE near Nationals Park",
      "summary": "The 200-seat space sits by the Navy Yard-Ballpark Metro entrance.",
      "article_url": "https://example.org/bar",
      "published": "2026-08-30T14:00:00+00:00",
      "card": {
        "path": "/abs/path/cards/351c2354c5c76113.png",
        "url": "https://mostrewatched.example/cards/351c2354c5c76113.png",
        "width": 1080,
        "height": 1350
      },
      "targets": {
        "instagram": {"caption": "…headline, source, link, hashtags…"},
        "tiktok": {
          "title": "New rooftop bar opens on Half Street SE…",
          "description": "…",
          "hashtags": ["#NavyYard", "#CapitolRiverfront", "…"]
        }
      }
    }
  ]
}
```

Use `card.path` for a direct file upload, or `card.url` if you pull media from
a URL — set `card_base_url` in config to whatever public location you serve
`cards/` from. `--format jsonl` writes one item per line instead; `--stdout`
prints rather than writing a file; `--dry-run` builds the manifest without
marking anything exported.

Your pipeline reports back with a results file — JSON array or JSON Lines, one
entry per item per target:

```json
[
  {"id": "351c2354c5c76113", "target": "instagram", "status": "posted",
   "permalink": "https://www.instagram.com/p/ABC123/"},
  {"id": "351c2354c5c76113", "target": "tiktok", "status": "failed",
   "error": "photo post quota exceeded"}
]
```

```bash
./dcnews.py mark-posted --from results.json
./dcnews.py mark-posted 351c2354c5c76113 --target tiktok --permalink https://…  # one-off
```

An item counts as posted once **any** target succeeds; per-target detail is
kept either way and shown by `queue -v`. Re-reporting a target updates that
row rather than duplicating it, so a retry is safe.

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
once a story is already local, so the neighborhood version of a story outranks
the generic one.

Two things it deliberately throws away:

- **Other cities' navy yards.** Brooklyn, Philadelphia, Boston, Charlestown,
  Portsmouth, Norfolk. A story matching those with no DC signal is dropped.
- **Other Washingtons.** Washington state, Washington County, Washington
  University, Seattle datelines.

Matching is on word boundaries, so "M Street SE" doesn't fire on "farm street".

Tune it in `config.json` without touching code:

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
relying on the list** — feed URLs rot, and none were reachable from the
environment this was built in:

```bash
./dcnews.py check-feeds
```

Drop whatever reports DEAD and add your own. RSS 2.0 and Atom both work.
Fetches send `If-None-Match`/`If-Modified-Since`, so an unchanged feed costs one
304 rather than a re-parse. Articles dedupe by canonical URL — tracking
params, `www.`, and trailing slashes are stripped — so a story syndicated to
two feeds is queued once.

## Cards

1080x1350 (4:5 portrait — the most feed space Instagram allows, and fine as a
TikTok photo post). Navy Yard items get a gold badge, citywide items blue. The
card carries the headline, a summary line, the source name, and the date.

Rendering shells out to headless Chrome rather than using Playwright, so there
are no Python dependencies. Set `CHROME_BIN`, or `"browser"` in config, to pick
a binary; otherwise common install paths are tried.

One quirk worth knowing, since it caused a real bug: with a full Chrome binary
the viewport is ~87px shorter than `--window-size`, and content laid out past
the fold silently fails to render — footers came out sliced in half. Cards are
therefore rendered with vertical slack and cropped back to exactly 1080x1350 by
`pngtools.py`, a small stdlib PNG cropper.

## Caption limits

In `captions.py`, as constants rather than assumptions buried in code — both
platforms have changed these before, so check them against current docs:

| | limit |
| --- | --- |
| Instagram caption | 2200 characters, 30 hashtags |
| TikTok photo title | 90 characters |
| TikTok photo description | 4000 characters |

Captions are built as headline, source attribution, article URL, then hashtags.
When something has to give, the headline is trimmed and attribution and tags
are kept.

## Throttle

`export.max_per_day` (default 6) caps how much is handed to the publisher per
rolling 24 hours, counted from items actually exported. It is not a substitute
for whatever rate limits your pipeline already enforces — Instagram allows 25
API-published posts per 24 hours, and TikTok has its own quota.

## Automating it

```cron
0 */3 * * * cd /path/to/tools/dcnews && ./dcnews.py run >> run.log 2>&1
```

That fetches, renders, and writes the manifest. Whether anything reaches an
audience is then entirely your pipeline's decision, which is the point of the
split: this side can run unattended without anything going out unreviewed.

To skip manual approval, set `export.auto_approve_navy_yard` — Navy Yard items
are then approved, rendered, and exported automatically. Citywide items still
wait for `approve`.

## Editorial and legal notes

Worth being deliberate about, since this republishes other people's reporting:

- Cards use **your own text rendering of a headline plus attribution** — no
  publisher photographs are copied. Keep it that way; reposting a news outlet's
  images is a copyright problem that headline aggregation largely is not.
- Every card and caption names the source and includes the original link.
- Headlines are reproduced verbatim from the feed. If you edit one for length,
  make sure the edit doesn't change its meaning.
- Automated posting means errors publish themselves. The review queue and
  `max_per_day` exist for that reason.
- Your privacy policy currently says the app "collects no information from the
  public" and handles "no data about any other person." Ingesting other
  people's reporting doesn't fit that description — worth updating before this
  goes live.

## Files

| file | role |
| --- | --- |
| `dcnews.py` | CLI: fetch, queue, approve, render, export, mark-posted, run, status, check-feeds |
| `feeds.py` | RSS/Atom fetching and parsing, conditional GET |
| `relevance.py` | DC and Navy Yard scoring, false-friend rejection |
| `store.py` | SQLite queue, per-target results, URL canonicalization |
| `render.py` | HTML card template, headless Chrome rendering |
| `pngtools.py` | stdlib PNG cropping |
| `captions.py` | Instagram and TikTok caption text |
| `test_dcnews.py` | 41 tests (`python3 test_dcnews.py`) |

`config.json`, `dcnews.db`, `manifest.json`, and `cards/` are gitignored.
