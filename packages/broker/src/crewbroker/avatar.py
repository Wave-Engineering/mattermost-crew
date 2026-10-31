"""A bot account's avatar: a deterministic identicon of its account name (#151; D-098).

Only a bot's owner, the broker, can set its image (spike 1.2), so `Broker.mint` sets it once.
`mint` never receives the agent's `dev_avatar`, an emoji (§5.3, D-103): rendering one would
need a font stack, and the avatar is display only. A pure `zlib`/`struct` PNG needs no new
dependency.
"""

from __future__ import annotations

import hashlib
import struct
import zlib

GRID = 5  # cells per side; the left three columns are mirrored onto the right two
BACKGROUND = (0xF0, 0xF0, 0xF0)


def _channel(value: int) -> int:
    return min(max(value, 40), 215)  # never near-white on the background, never black


def identicon_png(name: str, size: int = 128) -> bytes:
    """A `size`-pixel square RGB PNG, the same for the same `name`, and distinct per name."""
    h = hashlib.sha256(name.encode()).digest()
    fg = bytes(_channel(b) for b in h[0:3])
    bg = bytes(BACKGROUND)
    bits = int.from_bytes(h[3:5], "big")
    # Cell (r, c) mirrors column c onto GRID - 1 - c, so only the left three columns are drawn.
    on = [
        [bool(bits >> (r * 3 + min(c, GRID - 1 - c)) & 1) for c in range(GRID)]
        for r in range(GRID)
    ]
    rows = []
    for y in range(size):
        r = min(y * GRID // size, GRID - 1)
        line = b"".join(fg if on[r][min(x * GRID // size, GRID - 1)] else bg for x in range(size))
        rows.append(b"\x00" + line)  # filter type 0 per scanline

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )
