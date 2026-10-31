"""#151: the bot avatar the broker sets at mint, a deterministic identicon of the account name."""

from __future__ import annotations

import struct
import zlib

from crewbroker.avatar import identicon_png


def chunks(png: bytes) -> dict[bytes, bytes]:
    """Each chunk's data by type, with every CRC checked."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    out: dict[bytes, bytes] = {}
    at = 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        kind, data = png[at + 4 : at + 8], png[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", png[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF, kind
        out[kind] = data
        at += 12 + length
    return out


def test_identicon_is_png_and_deterministic() -> None:
    png = identicon_png("babel.oaw")
    parts = chunks(png)
    assert list(parts) == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour = struct.unpack(">IIBB", parts[b"IHDR"][:10])
    assert (width, height, depth, colour) == (128, 128, 8, 2)  # 8-bit RGB
    raw = zlib.decompress(parts[b"IDAT"])
    assert len(raw) == 128 * (1 + 128 * 3)  # one filter byte per scanline
    assert identicon_png("babel.oaw") == png
    assert identicon_png("kessel.oaw") != png


def test_identicon_is_mirrored() -> None:
    """The grid is mirrored left to right, so every scanline reads the same both ways."""
    size = 20
    raw = zlib.decompress(chunks(identicon_png("babel.oaw", size))[b"IDAT"])
    stride = 1 + size * 3
    for y in range(size):
        pixels = [raw[y * stride + 1 + x * 3 : y * stride + 4 + x * 3] for x in range(size)]
        assert pixels == pixels[::-1]
