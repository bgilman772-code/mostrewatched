"""Build the caption text each posting target expects.

This module produces text only. Posting is handed off to the existing
Social-Clipper pipeline via the export manifest — nothing here talks to an API.

Limits are conservative defaults; both platforms have changed them before, so
they're constants you can adjust rather than assumptions buried in the code.
"""

from __future__ import annotations

# Instagram: caption limit and the point where extra hashtags stop counting.
IG_MAX_CAPTION = 2200
IG_MAX_HASHTAGS = 30

# TikTok photo posts carry a short title plus a longer description.
TIKTOK_MAX_TITLE = 90
TIKTOK_MAX_DESCRIPTION = 4000

BASE_TAGS = ["#WashingtonDC", "#DCNews"]
NAVY_YARD_TAGS = ["#NavyYard", "#CapitolRiverfront", "#NavyYardDC"]


def build_hashtags(tier: str, extra: list[str] | None = None, limit: int = IG_MAX_HASHTAGS) -> list[str]:
    """Neighborhood tags first for Navy Yard items, then the configured ones."""
    tags = list(BASE_TAGS)
    if tier == "navy_yard":
        tags = NAVY_YARD_TAGS + tags
    tags += list(extra or [])
    seen, out = set(), []
    for tag in tags:
        tag = tag if tag.startswith("#") else f"#{tag}"
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            out.append(tag)
    return out[:limit]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def instagram_caption(
    headline: str,
    source: str,
    url: str = "",
    tier: str = "dc",
    extra_tags: list[str] | None = None,
) -> str:
    """Headline, attribution, article URL, then hashtags.

    Instagram captions aren't clickable; the URL is included as plain text so a
    reader can find the original.
    """
    tags = build_hashtags(tier, extra_tags)
    parts = [headline.strip()]
    if source.strip():
        parts.append(f"Source: {source.strip()}")
    if url.strip():
        parts.append(url.strip())
    parts.append(" ".join(tags))

    caption = "\n\n".join(p for p in parts if p)
    if len(caption) > IG_MAX_CAPTION:
        # Trim the headline rather than losing attribution or tags.
        overflow = len(caption) - IG_MAX_CAPTION
        parts[0] = _truncate(parts[0], max(1, len(parts[0]) - overflow))
        caption = "\n\n".join(p for p in parts if p)
    return caption


def tiktok_post(
    headline: str,
    source: str,
    url: str = "",
    tier: str = "dc",
    extra_tags: list[str] | None = None,
) -> dict:
    """Title and description for a TikTok photo post."""
    tags = build_hashtags(tier, extra_tags)
    description_parts = [headline.strip()]
    if source.strip():
        description_parts.append(f"Source: {source.strip()}")
    if url.strip():
        description_parts.append(url.strip())
    description_parts.append(" ".join(tags))
    return {
        "title": _truncate(headline.strip(), TIKTOK_MAX_TITLE),
        "description": _truncate("\n\n".join(p for p in description_parts if p), TIKTOK_MAX_DESCRIPTION),
        "hashtags": tags,
    }
