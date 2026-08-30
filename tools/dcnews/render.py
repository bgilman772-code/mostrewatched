"""Render a news item into an Instagram-sized card PNG using headless Chromium.

Chromium is driven through its command line rather than Playwright, so this
needs no pip packages — any Chrome/Chromium install works. Set CHROME_BIN to
point at a specific binary.
"""

from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile

import pngtools
from datetime import datetime

CANVAS = (1080, 1350)  # 4:5 portrait — the tallest Instagram allows in feed

# Extra window height so the card never lands below the viewport fold.
VIEWPORT_SLACK = 220

BROWSER_CANDIDATES = (
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell",
    "/opt/pw-browsers/chromium/chrome-linux/chrome",
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { background:#0b1f33; }
  /* The card is explicitly sized rather than sized to the viewport: with a
     real Chrome binary the viewport is shorter than --window-size, and
     anything laid out against it gets clipped at the bottom. */
  .card {
    width:{W}px; height:{H}px; overflow:hidden;
    font-family: "Helvetica Neue", Helvetica, Arial, "DejaVu Sans", sans-serif;
    background: linear-gradient(160deg, #0b1f33 0%, #123047 55%, #0d2438 100%);
    color: #fff; display:flex; flex-direction:column; padding:78px 72px 64px;
  }
  .badge {
    align-self:flex-start; font-size:26px; letter-spacing:.22em; font-weight:700;
    text-transform:uppercase; padding:14px 26px; border-radius:6px;
    background:{ACCENT}; color:#08131f;
  }
  .headline {
    margin-top:56px; font-size:{FS}px; line-height:1.14; font-weight:800;
    letter-spacing:-.015em; flex:0 0 auto;
    display:-webkit-box; -webkit-line-clamp:7; -webkit-box-orient:vertical; overflow:hidden;
  }
  .summary {
    margin-top:34px; font-size:34px; line-height:1.42; color:#b9cede; font-weight:400;
    display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden;
  }
  .spacer { flex:1 1 auto; min-height:40px; }
  .rule { height:5px; width:130px; background:{ACCENT}; border-radius:3px; }
  .footer { margin-top:34px; display:flex; justify-content:space-between; align-items:flex-end; gap:24px; }
  .source { font-size:31px; font-weight:700; color:#e8f1f8; }
  .meta { font-size:25px; color:#8fa9bd; margin-top:8px; }
  .handle { font-size:25px; color:#8fa9bd; text-align:right; white-space:nowrap; }
</style></head><body><div class="card">
  <div class="badge">{BADGE}</div>
  <div class="headline">{HEADLINE}</div>
  {SUMMARY_BLOCK}
  <div class="spacer"></div>
  <div class="rule"></div>
  <div class="footer">
    <div>
      <div class="source">{SOURCE}</div>
      <div class="meta">{DATE}</div>
    </div>
    <div class="handle">{HANDLE}</div>
  </div>
</div></body></html>"""


class RenderError(Exception):
    """Card rendering failed."""


def find_browser(explicit: str = "") -> str:
    """Locate a Chromium/Chrome binary, or raise with instructions."""
    for candidate in filter(None, (explicit, os.environ.get("CHROME_BIN", ""))):
        resolved = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
        if resolved:
            return resolved
        raise RenderError(f"browser {candidate!r} not found (from CHROME_BIN or config)")
    for candidate in BROWSER_CANDIDATES:
        resolved = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
        if resolved:
            return resolved
    raise RenderError(
        "no Chrome/Chromium found. Install Chromium or set CHROME_BIN to its path."
    )


def headline_font_size(text: str) -> int:
    """Shrink the headline as it gets longer so it keeps fitting the card."""
    n = len(text)
    if n <= 40:
        return 88
    if n <= 70:
        return 76
    if n <= 100:
        return 66
    if n <= 140:
        return 58
    return 50


def pretty_date(iso_ts: str | None) -> str:
    if not iso_ts:
        return datetime.now().strftime("%B %-d, %Y")
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    try:
        return dt.strftime("%B %-d, %Y")
    except ValueError:  # platforms without %-d
        return dt.strftime("%B %d, %Y").replace(" 0", " ")


def build_html(
    headline: str,
    source: str,
    published: str | None = None,
    summary: str = "",
    tier: str = "dc",
    handle: str = "",
    accent: str = "",
) -> str:
    is_ny = tier == "navy_yard"
    badge = "Navy Yard" if is_ny else "Washington DC"
    accent = accent or ("#f2b134" if is_ny else "#5bb8e8")
    summary_block = (
        f'<div class="summary">{html.escape(summary)}</div>' if summary.strip() else ""
    )
    replacements = {
        "{W}": str(CANVAS[0]),
        "{H}": str(CANVAS[1]),
        "{FS}": str(headline_font_size(headline)),
        "{ACCENT}": accent,
        "{BADGE}": html.escape(badge),
        "{HEADLINE}": html.escape(headline),
        "{SUMMARY_BLOCK}": summary_block,
        "{SOURCE}": html.escape(source),
        "{DATE}": html.escape(pretty_date(published)),
        "{HANDLE}": html.escape(handle),
    }
    out = TEMPLATE
    for token, value in replacements.items():
        out = out.replace(token, value)
    return out


def render_card(
    headline: str,
    source: str,
    out_path: str,
    published: str | None = None,
    summary: str = "",
    tier: str = "dc",
    handle: str = "",
    browser: str = "",
    timeout: float = 60.0,
) -> str:
    """Render one card to out_path and return that path."""
    binary = find_browser(browser)
    doc = build_html(headline, source, published, summary, tier, handle)
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "card.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(doc)
        cmd = [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            # Render with vertical slack: with a real Chrome binary the viewport
            # is shorter than the window, and content below the fold never
            # renders. The extra height is cropped off below.
            f"--window-size={CANVAS[0]},{CANVAS[1] + VIEWPORT_SLACK}",
            "--virtual-time-budget=3000",
            f"--user-data-dir={os.path.join(tmp, 'profile')}",
            f"--screenshot={out_path}",
            f"file://{page}",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"chromium timed out after {timeout}s") from exc

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        stderr = proc.stderr.decode("utf-8", "replace")[-400:]
        raise RenderError(f"chromium produced no image (exit {proc.returncode}): {stderr}")

    try:
        if pngtools.dimensions(out_path) != CANVAS:
            pngtools.crop_topleft(out_path, *CANVAS)
    except pngtools.PngError as exc:
        raise RenderError(f"could not crop card to {CANVAS[0]}x{CANVAS[1]}: {exc}") from exc
    return out_path
