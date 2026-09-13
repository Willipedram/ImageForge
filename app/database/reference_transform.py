"""Structure-aware WordPress image-reference transformations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote, unquote

from app.database.wordpress_serialization import PHPArray, PHPSerializationError, dumps, loads
from app.online.models import ReferenceChange


class ChangeType(StrEnum):
    PLAIN = "PLAIN"
    HTML = "HTML"
    JSON = "JSON"
    PHP_SERIALIZED = "PHP_SERIALIZED"
    REVIEW = "REVIEW"


@dataclass(frozen=True, slots=True)
class TransformResult:
    value: str
    changed: bool
    change_type: ChangeType
    review_reason: str | None = None


def transform_value(value: str, changes: tuple[ReferenceChange, ...], *, html: bool = False) -> TransformResult:
    stripped = value.lstrip()
    if re.match(r"^[NbisadOCRr]:", stripped):
        try:
            parsed = loads(value); transformed, changed = _walk(parsed, changes)
            encoded = dumps(transformed)
            loads(encoded)  # round-trip before allowing persistence
            return TransformResult(encoded, changed, ChangeType.PHP_SERIALIZED)
        except PHPSerializationError as exc:
            return TransformResult(value, False, ChangeType.REVIEW, str(exc))
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value); transformed, changed = _walk(parsed, changes)
            encoded = json.dumps(transformed, ensure_ascii=False, separators=(",", ":"))
            json.loads(encoded)
            return TransformResult(encoded, changed, ChangeType.JSON)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return TransformResult(value, False, ChangeType.REVIEW, f"Invalid JSON: {exc}")
    if html or re.search(r"<(?:img|source|a)\b", value, re.I):
        updated = _replace_html(value, changes)
        return TransformResult(updated, updated != value, ChangeType.HTML)
    updated = _replace_text(value, changes)
    return TransformResult(updated, updated != value, ChangeType.PLAIN)


def contains_reference(value: str, path: str) -> bool:
    decoded = unquote(value).replace("\\/", "/")
    marker = "/uploads/"
    variants = (path, path.lstrip("/"), path.split(marker, 1)[1] if marker in path else path.lstrip("/"))
    return any(_bounded_replace(decoded, variant, variant + "#IMAGEFORGE-PROBE") != decoded
               for variant in variants)


def _walk(value, changes):
    if isinstance(value, str):
        updated = _replace_text(value, changes); return updated, updated != value
    if isinstance(value, list):
        output, changed = [], False
        for item in value:
            new, item_changed = _walk(item, changes); output.append(new); changed |= item_changed
        return output, changed
    if isinstance(value, dict):
        output, changed = {}, False
        for key, item in value.items():
            new, item_changed = _walk(item, changes); output[key] = new; changed |= item_changed
        return output, changed
    if isinstance(value, PHPArray):
        output, changed = [], False
        for key, item in value.items:
            new, item_changed = _walk(item, changes); output.append((key, new)); changed |= item_changed
        return PHPArray(tuple(output)), changed
    return value, False


def _replace_text(value: str, changes: tuple[ReferenceChange, ...]) -> str:
    result = value
    for change in changes:
        old, new = change.original_path, change.replacement_path
        marker = "/uploads/"
        old_upload = old.split(marker, 1)[1] if marker in old else old.lstrip("/")
        new_upload = new.split(marker, 1)[1] if marker in new else new.lstrip("/")
        variants = ((old, new), (old.lstrip("/"), new.lstrip("/")), (old_upload, new_upload),
                    (quote(old, safe="/"), quote(new, safe="/")),
                    (old.replace("/", "\\/"), new.replace("/", "\\/")))
        for source, target in variants: result = _bounded_replace(result, source, target)
    return result


def _bounded_replace(value: str, old: str, new: str) -> str:
    if not old: return value
    # Permit URL query/fragment delimiters but reject filename/path continuations.
    leading = r"(?<![\w%.-])" if old[0].isalnum() else ""
    pattern = re.compile(leading + re.escape(old) + r"(?![\w%./-])")
    return pattern.sub(lambda _: new, value)


def _replace_html(value: str, changes: tuple[ReferenceChange, ...]) -> str:
    # Only URL-bearing attributes are modified; text nodes and unrelated attributes remain untouched.
    pattern = re.compile(r"(?P<prefix>\b(?:src|href|poster|data-src|data-lazy-src|srcset)\s*=\s*)(?P<q>['\"])(?P<v>.*?)(?P=q)", re.I | re.S)
    return pattern.sub(lambda match: match.group("prefix") + match.group("q") +
                       _replace_text(match.group("v"), changes) + match.group("q"), value)
