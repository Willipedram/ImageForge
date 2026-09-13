"""Strict PHP serialization support used for WordPress metadata values."""

from __future__ import annotations

from dataclasses import dataclass


class PHPSerializationError(ValueError):
    """Raised when data cannot be parsed without guessing."""


@dataclass(frozen=True, slots=True)
class PHPArray:
    items: tuple[tuple[object, object], ...]


def loads(payload: str | bytes) -> object:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    value, offset = _parse(data, 0)
    if offset != len(data):
        raise PHPSerializationError("Trailing bytes in serialized value.")
    return value


def dumps(value: object) -> str:
    return _dump(value).decode("utf-8")


def _parse(data: bytes, offset: int) -> tuple[object, int]:
    kind = data[offset:offset + 1]
    if kind == b"N":
        if data[offset:offset + 2] != b"N;": raise PHPSerializationError("Invalid null token.")
        return None, offset + 2
    if offset + 2 > len(data) or data[offset + 1:offset + 2] != b":":
        raise PHPSerializationError("Invalid serialized token.")
    if kind in {b"b", b"i", b"d"}:
        end = data.find(b";", offset + 2)
        if end < 0: raise PHPSerializationError("Unterminated scalar.")
        raw = data[offset + 2:end]
        try:
            value = (raw == b"1") if kind == b"b" else int(raw) if kind == b"i" else float(raw)
        except ValueError as exc: raise PHPSerializationError("Invalid scalar value.") from exc
        if kind == b"b" and raw not in {b"0", b"1"}: raise PHPSerializationError("Invalid boolean.")
        return value, end + 1
    if kind == b"s":
        colon = data.find(b":", offset + 2)
        if colon < 0: raise PHPSerializationError("Missing string length.")
        try: size = int(data[offset + 2:colon])
        except ValueError as exc: raise PHPSerializationError("Invalid string length.") from exc
        start, end = colon + 2, colon + 2 + size
        if data[colon + 1:colon + 2] != b'"' or data[end:end + 2] != b'";':
            raise PHPSerializationError("String byte length does not match its payload.")
        try: value = data[start:end].decode("utf-8")
        except UnicodeDecodeError as exc: raise PHPSerializationError("Serialized string is not UTF-8.") from exc
        return value, end + 2
    if kind == b"a":
        colon = data.find(b":", offset + 2)
        if colon < 0: raise PHPSerializationError("Missing array length.")
        try: count = int(data[offset + 2:colon])
        except ValueError as exc: raise PHPSerializationError("Invalid array length.") from exc
        if data[colon + 1:colon + 2] != b"{": raise PHPSerializationError("Invalid array opening.")
        cursor, items = colon + 2, []
        for _ in range(count):
            key, cursor = _parse(data, cursor); value, cursor = _parse(data, cursor); items.append((key, value))
        if data[cursor:cursor + 1] != b"}": raise PHPSerializationError("Invalid array closing.")
        return PHPArray(tuple(items)), cursor + 1
    # Objects, references and custom serialization require WordPress/PHP itself.
    raise PHPSerializationError(f"Unsupported serialized token {kind!r}.")


def _dump(value: object) -> bytes:
    if value is None: return b"N;"
    if isinstance(value, bool): return b"b:1;" if value else b"b:0;"
    if isinstance(value, int): return f"i:{value};".encode()
    if isinstance(value, float): return f"d:{value!r};".encode()
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"s:" + str(len(encoded)).encode() + b':"' + encoded + b'";'
    if isinstance(value, PHPArray):
        body = b"".join(_dump(key) + _dump(item) for key, item in value.items)
        return b"a:" + str(len(value.items)).encode() + b":{" + body + b"}"
    raise PHPSerializationError(f"Unsupported value type {type(value).__name__}.")
