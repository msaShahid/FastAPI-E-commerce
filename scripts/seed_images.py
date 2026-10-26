"""Placeholder product and category pictures, generated with the standard library only (no image library needed).

Each is a soft two-colour gradient with a lighter badge in the middle, tinted by the category's colour and varied per product.
They are PLACEHOLDERS: replace them with real photography through the admin API (POST /products/{id}/image)."""

import colorsys
import struct
import zlib

SIZE = 480


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)  # filter type 0 on every scanline
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _rgb(hue: float, sat: float, val: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb((hue % 360) / 360, sat, val)
    return int(r * 255), int(g * 255), int(b * 255)


def placeholder(category_hue: int, variant: int = 0) -> bytes:
    """A deterministic PNG: the same (hue, variant) always gives the same picture, so a re-run changes nothing."""
    top = _rgb(category_hue + variant * 7, 0.35, 0.97)
    bottom = _rgb(category_hue + variant * 7 + 18, 0.55, 0.78)
    badge = _rgb(category_hue + variant * 7, 0.12, 1.0)
    cx = cy = SIZE // 2
    radius = SIZE // 4
    rows = []
    for y in range(SIZE):
        t = y / (SIZE - 1)
        base = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        row = bytearray()
        dy = (y - cy) ** 2
        for x in range(SIZE):
            row.extend(badge if (x - cx) ** 2 + dy <= radius**2 else base)
        rows.append(bytes(row))
    return _png(SIZE, SIZE, rows)
