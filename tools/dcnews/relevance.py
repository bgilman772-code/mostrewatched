"""Score a news item on how much it is about DC, and specifically Navy Yard.

Two tiers come out of this:
  navy_yard — Near Southeast: the ballpark, Yards Park, Capitol Riverfront,
              the Metro stop, the streets in the quadrant.
  dc        — citywide news worth posting even without a Navy Yard hook.

Matching is on word boundaries, so "M Street SE" doesn't fire on "farm street"
and "nats" doesn't fire on "gnats".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Weighted term tiers. Anything in CORE marks the item as Navy Yard news.
CORE_TERMS = {
    "navy yard": 10, "washington navy yard": 10, "capitol riverfront": 10,
    "near southeast": 10, "yards park": 10, "the yards": 8, "canal park": 8,
    "nationals park": 9, "nats park": 9, "navy yard-ballpark": 10,
    "navy yard ballpark": 10, "buzzard point": 9, "audi field": 8,
    "diamond teague": 8, "anacostia riverwalk": 6, "district winery": 6,
    "anc 6d": 10, "half street se": 9, "m street se": 9, "n street se": 8,
    "tingey street": 9, "water street se": 8, "first street se": 8,
    "new jersey avenue se": 8, "south capitol street": 7, "potomac avenue se": 7,
    "20003": 6, "navy yard metro": 10, "the bullpen": 7,
}

ADJACENT_TERMS = {
    "capitol hill": 4, "barracks row": 5, "eastern market": 4,
    "southwest waterfront": 4, "the wharf": 4, "buzzard's point": 5,
    "anacostia": 3, "ward 6": 4, "ward 8": 3, "8th street se": 4,
    "pennsylvania avenue se": 3, "congressional cemetery": 3,
    "washington nationals": 4, "dc united": 4, "nationals": 3,
    "l'enfant plaza": 3, "waterfront station": 3, "rfk": 2, "east potomac": 2,
}

CITYWIDE_TERMS = {
    "washington, d.c.": 2, "washington dc": 2, "washington, dc": 2,
    "the district": 2, "dc council": 3, "d.c. council": 3, "mayor bowser": 3,
    "wmata": 2, "ddot": 2, "dc government": 2, "district of columbia": 2,
    "metropolitan police": 2, "mpd": 2, "dpw": 1, "dc public schools": 2,
    "dcps": 2, "union station": 2, "smithsonian": 1, "national mall": 2,
}

# Topic terms only add weight once the item is already locally relevant.
TOPIC_TERMS = {
    "shooting": 3, "stabbing": 3, "homicide": 3, "arrest": 2, "carjacking": 3,
    "road closure": 3, "street closure": 3, "detour": 2, "construction": 2,
    "development": 2, "groundbreaking": 3, "opening": 2, "opens": 2,
    "closing": 2, "closes": 2, "permanently closed": 3, "restaurant": 2,
    "bar": 1, "festival": 3, "parade": 2, "fireworks": 3, "concert": 2,
    "game": 1, "playoff": 3, "power outage": 3, "water main": 3, "flooding": 3,
    "crash": 2, "fire": 2, "evacuation": 3, "protest": 2, "zoning": 2,
    "affordable housing": 3, "metro delay": 3, "single-tracking": 3,
}

# "Navy Yard" names real places in other cities. Without a DC signal, drop it.
FALSE_FRIENDS = (
    "brooklyn navy yard", "philadelphia navy yard", "philly navy yard",
    "boston navy yard", "charlestown navy yard", "portsmouth naval",
    "norfolk naval", "mare island", "puget sound naval", "pearl harbor naval",
)

# Other Washingtons.
WRONG_WASHINGTON = (
    "washington state", "washington county", "seattle", "spokane", "tacoma",
    "olympia, wash", "wash., pa", "washington, pa", "washington, il",
    "washington, mo", "washington university",
)

DC_SIGNALS = (
    "d.c.", "dc ", " dc", "district of columbia", "washington, d.c",
    "washington dc", "southeast washington", "capitol hill", "anacostia",
)


@dataclass
class Verdict:
    score: int
    tier: str  # "navy_yard" | "dc" | ""
    matched: list[str]
    rejected_reason: str = ""

    @property
    def relevant(self) -> bool:
        return bool(self.tier) and not self.rejected_reason


def _compile(terms: dict[str, int]) -> list[tuple[re.Pattern, str, int]]:
    out = []
    for term, weight in terms.items():
        out.append((re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE), term, weight))
    return out


_CORE = _compile(CORE_TERMS)
_ADJACENT = _compile(ADJACENT_TERMS)
_CITYWIDE = _compile(CITYWIDE_TERMS)
_TOPIC = _compile(TOPIC_TERMS)


def _hits(compiled, text: str) -> list[tuple[str, int]]:
    return [(term, weight) for pattern, term, weight in compiled if pattern.search(text)]


def score(
    title: str,
    summary: str = "",
    navy_yard_threshold: int = 8,
    dc_threshold: int = 6,
    extra_core: dict[str, int] | None = None,
    extra_exclude: tuple[str, ...] = (),
) -> Verdict:
    """Score one item. Title carries double weight — it's what the card shows."""
    title_l = (title or "").lower()
    summary_l = (summary or "").lower()
    combined = f"{title_l} {summary_l}"

    for phrase in FALSE_FRIENDS:
        if phrase in combined and not any(sig in combined for sig in DC_SIGNALS):
            return Verdict(0, "", [], f"another city's navy yard ({phrase})")
    for phrase in WRONG_WASHINGTON:
        if phrase in combined:
            return Verdict(0, "", [], f"different Washington ({phrase})")
    for phrase in extra_exclude:
        if phrase.lower() in combined:
            return Verdict(0, "", [], f"excluded term ({phrase})")

    core = _CORE + (_compile(extra_core) if extra_core else [])

    total = 0
    matched: list[str] = []
    core_hit = False
    for compiled in (core, _ADJACENT, _CITYWIDE):
        for term, weight in _hits(compiled, combined):
            # Title mentions count double; a place named in the headline is the story.
            multiplier = 2 if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title_l) else 1
            total += weight * multiplier
            matched.append(term)
            if compiled is core:
                core_hit = True

    if total > 0:
        for term, weight in _hits(_TOPIC, combined):
            total += weight
            matched.append(term)

    if core_hit and total >= navy_yard_threshold:
        tier = "navy_yard"
    elif total >= dc_threshold:
        tier = "dc"
    else:
        tier = ""

    return Verdict(total, tier, matched)
