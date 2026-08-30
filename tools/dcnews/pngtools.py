"""Minimal PNG cropping, so card rendering needs no image library.

Chromium's --screenshot captures the whole window, and with a real Chrome
binary the viewport is shorter than the window — content laid out past the
viewport fold never renders. The workaround is to render with vertical slack
and crop back to the exact canvas, which is all this module does: crop a
top-left region out of an 8-bit RGB/RGBA PNG.
"""

from __future__ import annotations

import struct
import zlib

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class PngError(Exception):
    """The PNG could not be read or is in a form this module doesn't handle."""


def _chunks(data: bytes):
    if not data.startswith(PNG_MAGIC):
        raise PngError("not a PNG file")
    pos = len(PNG_MAGIC)
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        yield ctype, body
        pos += 12 + length  # length + type + data + crc


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body))


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list[bytearray]:
    """Undo per-scanline PNG filtering, returning raw pixel rows."""
    stride = width * bpp
    rows: list[bytearray] = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos : pos + stride])
        pos += stride
        if ftype == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise PngError(f"unknown scanline filter {ftype}")
        rows.append(line)
        prev = line
    return rows


def crop_topleft(path: str, width: int, height: int, out_path: str | None = None) -> str:
    """Crop the top-left width x height region of a PNG, in place by default."""
    with open(path, "rb") as fh:
        data = fh.read()

    header = None
    idat = bytearray()
    for ctype, body in _chunks(data):
        if ctype == b"IHDR":
            header = body
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    if header is None:
        raise PngError("no IHDR chunk")

    src_w, src_h, depth, color, compression, filt, interlace = struct.unpack(">IIBBBBB", header)
    if depth != 8 or color not in (2, 6) or interlace != 0:
        raise PngError(f"unsupported PNG (depth={depth}, color type={color}, interlace={interlace})")
    if width > src_w or height > src_h:
        raise PngError(f"crop {width}x{height} is larger than image {src_w}x{src_h}")

    bpp = 3 if color == 2 else 4
    rows = _unfilter(zlib.decompress(bytes(idat)), src_w, src_h, bpp)

    out = bytearray()
    for row in rows[:height]:
        out.append(0)  # filter type: none
        out += row[: width * bpp]

    new_header = struct.pack(">IIBBBBB", width, height, depth, color, compression, filt, interlace)
    blob = (
        PNG_MAGIC
        + _chunk(b"IHDR", new_header)
        + _chunk(b"IDAT", zlib.compress(bytes(out), 9))
        + _chunk(b"IEND", b"")
    )
    target = out_path or path
    with open(target, "wb") as fh:
        fh.write(blob)
    return target


def dimensions(path: str) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(33)
    if not head.startswith(PNG_MAGIC):
        raise PngError("not a PNG file")
    return struct.unpack(">II", head[16:24])
