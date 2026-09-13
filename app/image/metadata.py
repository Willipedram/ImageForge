"""Dependency-free dimensions from bounded image headers."""

from __future__ import annotations

import struct


def dimensions_from_header(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return width, height
            if marker in {0xD8, 0xD9}:
                offset += 2
                continue
            if offset + 4 > len(data):
                break
            segment_length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            offset += 2 + segment_length
    return None
