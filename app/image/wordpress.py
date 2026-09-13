"""WordPress derivative and database-table relationship discovery."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.image.models import ImageRecord
from app.image.models import AssetClass

DERIVATIVE_PATTERN = re.compile(r"^(?P<base>.+)-(?P<width>\d+)x(?P<height>\d+)(?P<suffix>-(?:cropped|scaled|rotated))?$", re.I)


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    attachment_id: int
    original_path: str
    derivatives: tuple[str, ...] = ()


def map_derivatives(records: Iterable[ImageRecord], metadata: Iterable[AttachmentMetadata] = ()) -> None:
    by_path = {record.remote_path: record for record in records}
    for attachment in metadata:
        original = by_path.get(attachment.original_path)
        if original:
            original.attachment_id = attachment.attachment_id
        for derivative_path in attachment.derivatives:
            derivative = by_path.get(derivative_path)
            if derivative:
                derivative.attachment_id = attachment.attachment_id
                derivative.parent_path = attachment.original_path
                derivative.derivative_kind = "WORDPRESS_METADATA"
                derivative.asset_class = AssetClass.THUMBNAIL
    for record in by_path.values():
        if record.parent_path:
            continue
        path = PurePosixPath(record.remote_path)
        match = DERIVATIVE_PATTERN.match(path.stem)
        if not match:
            continue
        candidate = str(path.with_name(match.group("base") + path.suffix))
        if candidate in by_path:
            record.parent_path = candidate
            record.derivative_kind = f"{match.group('width')}x{match.group('height')}"
            record.asset_class = AssetClass.THUMBNAIL


@dataclass(frozen=True, slots=True)
class WordPressTables:
    prefix: str
    posts: str
    postmeta: str
    options: str
    termmeta: str | None
    additional: tuple[str, ...]


def discover_wordpress_tables(table_names: Iterable[str]) -> WordPressTables | None:
    names = set(table_names)
    candidates = []
    for name in names:
        if name.endswith("posts"):
            prefix = name[:-len("posts")]
            if all(prefix + suffix in names for suffix in ("postmeta", "options")):
                candidates.append(prefix)
    if not candidates:
        return None
    prefix = min(candidates, key=len)
    relevant_suffixes = ("terms", "term_taxonomy", "woocommerce_attribute_taxonomies")
    additional = tuple(sorted(prefix + suffix for suffix in relevant_suffixes if prefix + suffix in names))
    return WordPressTables(
        prefix, prefix + "posts", prefix + "postmeta", prefix + "options",
        prefix + "termmeta" if prefix + "termmeta" in names else None, additional,
    )
