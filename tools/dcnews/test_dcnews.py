#!/usr/bin/env python3
"""Tests for the DC/Navy Yard news backend. Standard library only:

    python3 test_dcnews.py

Network tests use a mock Graph API on localhost; nothing reaches Instagram.
The rendering test is skipped when no Chrome/Chromium is installed.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feeds
import instagram
import pngtools
import relevance
import render
from store import Item, Store, canonical_url


class TestRelevance(unittest.TestCase):
    def test_navy_yard_headlines(self):
        for title, summary in [
            ("New rooftop bar opens on Half Street SE near Nationals Park", ""),
            ("Yards Park to host summer concert series", "Capitol Riverfront BID announced it."),
            ("Metro single-tracking hits Navy Yard-Ballpark riders", ""),
            ("ANC 6D votes against zoning change", "The commission covers Navy Yard."),
        ]:
            with self.subTest(title=title):
                self.assertEqual(relevance.score(title, summary).tier, "navy_yard")

    def test_citywide_headlines(self):
        v = relevance.score("DC Council passes housing bill", "Mayor Bowser will sign it.")
        self.assertEqual(v.tier, "dc")

    def test_rejects_other_cities_navy_yards(self):
        v = relevance.score("Brooklyn Navy Yard studio expands", "The New York site adds stages.")
        self.assertEqual(v.tier, "")
        self.assertIn("brooklyn navy yard", v.rejected_reason)

    def test_rejects_other_washingtons(self):
        v = relevance.score("Washington state ferries delayed", "Seattle riders wait.")
        self.assertEqual(v.tier, "")

    def test_ignores_unrelated_news(self):
        self.assertEqual(relevance.score("Ten easy sheet pan dinners", "Weeknight cooking.").tier, "")

    def test_word_boundaries(self):
        """Substrings inside other words must not match."""
        self.assertEqual(relevance.score("Gnats swarm a farm street picnic", "").score, 0)

    def test_title_outweighs_summary(self):
        in_title = relevance.score("Navy Yard construction begins", "")
        in_summary = relevance.score("Construction begins", "The site is in Navy Yard.")
        self.assertGreater(in_title.score, in_summary.score)

    def test_custom_exclude(self):
        v = relevance.score("Navy Yard bar opens", "", extra_exclude=("bar opens",))
        self.assertEqual(v.tier, "")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_canonical_url_drops_tracking_and_case(self):
        self.assertEqual(
            canonical_url("https://WWW.Example.org/story/?utm_source=rss&id=4"),
            "https://example.org/story?id=4",
        )

    def test_same_article_queued_once(self):
        a = Item(source="A", title="t", url="https://example.org/x?utm_source=rss")
        b = Item(source="B", title="t syndicated", url="https://www.example.org/x/")
        self.assertTrue(self.store.add(a))
        self.assertFalse(self.store.add(b), "trailing slash + www + utm should dedupe")

    def test_status_transitions_and_counts(self):
        item = Item(source="A", title="t", url="https://example.org/y", tier="navy_yard", score=30)
        self.store.add(item)
        self.store.set_status(item.id, "approved", card_path="/tmp/a.png")
        row = self.store.get(item.id)
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["card_path"], "/tmp/a.png")
        self.store.set_status(item.id, "posted", ig_media_id="M1")
        self.assertEqual(self.store.counts(), {"posted": 1})

    def test_rejects_unknown_status_and_column(self):
        item = Item(source="A", title="t", url="https://example.org/z")
        self.store.add(item)
        with self.assertRaises(ValueError):
            self.store.set_status(item.id, "banana")
        with self.assertRaises(ValueError):
            self.store.set_status(item.id, "approved", nonexistent="x")

    def test_posted_since_window(self):
        item = Item(source="A", title="t", url="https://example.org/w")
        self.store.add(item)
        self.store.set_status(item.id, "posted")
        self.assertEqual(self.store.posted_since("1970-01-01T00:00:00+00:00"), 1)
        self.assertEqual(self.store.posted_since("2999-01-01T00:00:00+00:00"), 0)

    def test_feed_conditional_headers(self):
        self.assertEqual(self.store.feed_headers("https://f"), {})
        self.store.save_feed_state("https://f", 'W/"abc"', "Sat, 30 Aug 2026 12:00:00 GMT", "200")
        headers = self.store.feed_headers("https://f")
        self.assertEqual(headers["If-None-Match"], 'W/"abc"')
        self.assertIn("If-Modified-Since", headers)


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>A &amp; B</title><link>https://example.org/1</link>
<description>&lt;p&gt;body&lt;/p&gt;</description>
<pubDate>Sat, 30 Aug 2026 14:00:00 GMT</pubDate></item>
<item><title>No link</title><description>skip me</description></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>
<entry><title>Atom item</title><link rel="alternate" href="https://example.org/2"/>
<summary>sum</summary><published>2026-08-30T11:30:00Z</published></entry></feed>"""


class TestFeeds(unittest.TestCase):
    def test_parse_rss(self):
        entries = feeds.parse(RSS, "src")
        self.assertEqual(len(entries), 1, "items without a link are skipped")
        self.assertEqual(entries[0].title, "A & B")
        self.assertEqual(entries[0].summary, "body")
        self.assertEqual(entries[0].published, "2026-08-30T14:00:00+00:00")

    def test_parse_atom(self):
        entries = feeds.parse(ATOM, "src")
        self.assertEqual(entries[0].url, "https://example.org/2")
        self.assertEqual(entries[0].published, "2026-08-30T11:30:00+00:00")

    def test_rejects_non_feed(self):
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"<html><body>hi</body></html>", "src")
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"garbage <<", "src")

    def test_date_fallbacks(self):
        self.assertIsNone(feeds.parse_date("not a date"))
        self.assertIsNone(feeds.parse_date(""))
        self.assertEqual(feeds.parse_date("2026-08-30T11:30:00Z"), "2026-08-30T11:30:00+00:00")


def encode_png(width: int, height: int, filt: int, color: int = 2) -> tuple[bytes, list[bytearray]]:
    """Hand-encode a PNG using one scanline filter, returning it and its pixels."""
    bpp = 3 if color == 2 else 4
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row += bytes(((x * 7 + y) % 256, (y * 11) % 256, (x * x + y) % 256))
            if bpp == 4:
                row += b"\xff"
        rows.append(row)
    out = bytearray()
    prev = bytearray(width * bpp)
    for row in rows:
        out.append(filt)
        enc = bytearray(len(row))
        for i in range(len(row)):
            left = row[i - bpp] if i >= bpp else 0
            up = prev[i]
            upleft = prev[i - bpp] if i >= bpp else 0
            if filt == 0:
                enc[i] = row[i]
            elif filt == 1:
                enc[i] = (row[i] - left) & 0xFF
            elif filt == 2:
                enc[i] = (row[i] - up) & 0xFF
            elif filt == 3:
                enc[i] = (row[i] - ((left + up) >> 1)) & 0xFF
            else:
                enc[i] = (row[i] - pngtools._paeth(left, up, upleft)) & 0xFF
        out += enc
        prev = row

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    blob = pngtools.PNG_MAGIC + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b"")
    return blob, rows


def read_png_pixels(path: str) -> tuple[int, int, list[bytearray]]:
    with open(path, "rb") as fh:
        data = fh.read()
    idat = bytearray()
    header = None
    for ctype, body in pngtools._chunks(data):
        if ctype == b"IHDR":
            header = body
        elif ctype == b"IDAT":
            idat += body
    w, h, depth, color = struct.unpack(">IIBB", header[:10])
    bpp = 3 if color == 2 else 4
    return w, h, pngtools._unfilter(zlib.decompress(bytes(idat)), w, h, bpp)


class TestPngTools(unittest.TestCase):
    def test_crop_is_exact_for_every_filter(self):
        for color in (2, 6):
            for filt in range(5):
                with self.subTest(color=color, filter=filt):
                    blob, rows = encode_png(60, 40, filt, color)
                    with tempfile.TemporaryDirectory() as tmp:
                        src = os.path.join(tmp, "in.png")
                        dst = os.path.join(tmp, "out.png")
                        open(src, "wb").write(blob)
                        pngtools.crop_topleft(src, 25, 18, dst)
                        w, h, got = read_png_pixels(dst)
                        self.assertEqual((w, h), (25, 18))
                        bpp = 3 if color == 2 else 4
                        for y in range(18):
                            self.assertEqual(bytes(got[y]), bytes(rows[y][: 25 * bpp]))

    def test_rejects_oversized_crop_and_non_png(self):
        blob, _ = encode_png(10, 10, 0)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.png")
            open(p, "wb").write(blob)
            with self.assertRaises(pngtools.PngError):
                pngtools.crop_topleft(p, 99, 99)
            bad = os.path.join(tmp, "b.png")
            open(bad, "wb").write(b"nope")
            with self.assertRaises(pngtools.PngError):
                pngtools.dimensions(bad)


class TestRender(unittest.TestCase):
    def test_headline_escaping(self):
        doc = render.build_html('Bar & "Grill" <script>', "Src", tier="navy_yard")
        self.assertIn("Bar &amp; &quot;Grill&quot; &lt;script&gt;", doc)
        self.assertNotIn("<script>", doc)

    def test_badge_follows_tier(self):
        self.assertIn("Navy Yard", render.build_html("h", "s", tier="navy_yard"))
        self.assertIn("Washington DC", render.build_html("h", "s", tier="dc"))

    def test_font_shrinks_with_length(self):
        self.assertGreater(render.headline_font_size("short"), render.headline_font_size("x" * 200))

    def test_summary_omitted_when_blank(self):
        self.assertNotIn('class="summary"', render.build_html("h", "s", summary="   "))

    def test_renders_exact_canvas(self):
        try:
            render.find_browser()
        except render.RenderError as exc:
            self.skipTest(f"no browser available: {exc}")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "card.png")
            render.render_card(
                "New rooftop bar opens on Half Street SE near Nationals Park",
                "Test Source", out, published="2026-08-30T14:00:00+00:00",
                summary="A summary line.", tier="navy_yard", handle="@handle",
            )
            self.assertEqual(pngtools.dimensions(out), render.CANVAS)
            w, h, rows = read_png_pixels(out)
            bottom = rows[h - 30]
            self.assertTrue(any(bottom[i] > 10 for i in range(0, len(bottom), 3)),
                            "bottom of the card should be painted, not clipped")


class MockGraphHandler(BaseHTTPRequestHandler):
    polls = 0

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = parse_qs(self.rfile.read(length).decode())
        if path.endswith("/media"):
            if not body.get("image_url"):
                return self._send(400, {"error": {"code": 100, "message": "image_url required"}})
            return self._send(200, {"id": "CONTAINER123"})
        if path.endswith("/media_publish"):
            return self._send(200, {"id": "MEDIA456"})
        self._send(404, {"error": {"message": "no route"}})

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("/CONTAINER123"):
            MockGraphHandler.polls += 1
            return self._send(200, {"status_code": "FINISHED" if MockGraphHandler.polls >= 2 else "IN_PROGRESS"})
        if path.endswith("/MEDIA456"):
            return self._send(200, {"permalink": "https://www.instagram.com/p/ABC/"})
        if path.endswith("/content_publishing_limit"):
            return self._send(200, {"data": [{"quota_usage": 3, "config": {"quota_total": 25}}]})
        self._send(404, {"error": {"message": "no route"}})


class TestInstagram(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockGraphHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def publisher(self):
        return instagram.InstagramPublisher("IGUSER", "TOKEN", api_base=self.base, dry_run=False)

    def test_publish_flow_polls_until_finished(self):
        MockGraphHandler.polls = 0
        result = self.publisher().publish("https://cards.example.org/a.png", "caption")
        self.assertEqual(result.media_id, "MEDIA456")
        self.assertEqual(result.permalink, "https://www.instagram.com/p/ABC/")
        self.assertGreaterEqual(MockGraphHandler.polls, 2)

    def test_container_error_is_surfaced(self):
        with self.assertRaises(instagram.InstagramError) as ctx:
            self.publisher().create_container("", "caption")
        self.assertIn("image_url required", str(ctx.exception))

    def test_publishing_limit(self):
        self.assertEqual(self.publisher().publishing_limit()["quota_usage"], 3)

    def test_dry_run_makes_no_calls(self):
        MockGraphHandler.polls = 0
        dry = instagram.InstagramPublisher("", "", api_base=self.base, dry_run=True)
        self.assertEqual(dry.publish("https://x/y.png", "c").media_id, "dry-run")
        self.assertEqual(MockGraphHandler.polls, 0)

    def test_live_requires_credentials(self):
        with self.assertRaises(instagram.InstagramError):
            instagram.InstagramPublisher("", "", dry_run=False)

    def test_caption_structure(self):
        caption = instagram.build_caption("Headline", "WTOP", "https://u", "navy_yard", ["#Ward6"])
        self.assertTrue(caption.startswith("Headline"))
        self.assertIn("Source: WTOP", caption)
        self.assertIn("#NavyYard", caption)
        self.assertIn("#Ward6", caption)

    def test_caption_truncates_but_keeps_attribution(self):
        caption = instagram.build_caption("Navy Yard " * 400, "WTOP", "https://u", "navy_yard")
        self.assertLessEqual(len(caption), instagram.MAX_CAPTION)
        self.assertIn("Source: WTOP", caption)
        self.assertIn("#NavyYard", caption)

    def test_caption_dedupes_hashtags(self):
        caption = instagram.build_caption("H", "S", "", "navy_yard", ["#navyyard", "#NavyYard"])
        tags = [t.lower() for t in caption.split() if t.startswith("#")]
        self.assertEqual(tags.count("#navyyard"), 1, f"exact tag repeated: {tags}")
        self.assertEqual(len(tags), len(set(tags)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
