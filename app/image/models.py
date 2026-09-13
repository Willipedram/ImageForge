"""Typed image intelligence records persisted by the inventory service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AssetClass(StrEnum):
    PHOTO = "PHOTO"
    PRODUCT_PHOTO = "PRODUCT_PHOTO"
    LOGO = "LOGO"
    ICON = "ICON"
    ILLUSTRATION = "ILLUSTRATION"
    TRANSPARENT_GRAPHIC = "TRANSPARENT_GRAPHIC"
    SCREENSHOT = "SCREENSHOT"
    BACKGROUND = "BACKGROUND"
    THUMBNAIL = "THUMBNAIL"
    ANIMATION = "ANIMATION"
    VECTOR = "VECTOR"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class ImageRecord:
    job_id: str
    remote_path: str
    filename: str
    extension: str
    detected_format: str | None
    mime_type: str | None
    size: int
    width: int | None = None
    height: int | None = None
    aspect_ratio: float | None = None
    color_mode: str | None = None
    has_alpha: bool = False
    transparency_ratio: float | None = None
    has_semitransparency: bool = False
    is_animated: bool = False
    frame_count: int = 1
    has_exif: bool = False
    orientation: int | None = None
    has_icc_profile: bool = False
    checksum: str | None = None
    attachment_id: int | None = None
    parent_path: str | None = None
    derivative_kind: str | None = None
    asset_class: AssetClass = AssetClass.UNKNOWN
    suspicious: bool = False
    unnecessary_metadata: bool = False
    skip_reason: str | None = None
    signature_valid: bool = False

    def __post_init__(self) -> None:
        if self.aspect_ratio is None and self.width and self.height:
            self.aspect_ratio = self.width / self.height
