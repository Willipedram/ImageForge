"""Signature-first image analysis with optional Pillow pixel intelligence."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

from app.image.models import AssetClass, ImageRecord

FORMAT_MIMES = {
    "JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif",
    "WEBP": "image/webp", "AVIF": "image/avif", "SVG": "image/svg+xml",
}


def detect_format(data: bytes) -> str | None:
    stripped = data.lstrip()
    if data.startswith(b"\xff\xd8\xff"): return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "PNG"
    if data[:6] in {b"GIF87a", b"GIF89a"}: return "GIF"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP": return "WEBP"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}: return "AVIF"
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in stripped[:2048]): return "SVG"
    return None


class ImageAnalyzer:
    def analyze(self, path: Path, *, job_id: str, remote_path: str, attachment_id: int | None = None) -> ImageRecord:
        data = path.read_bytes()
        detected = detect_format(data)
        filename = PurePosixPath(remote_path).name
        record = ImageRecord(
            job_id, remote_path, filename, PurePosixPath(filename).suffix.casefold(), detected,
            FORMAT_MIMES.get(detected), len(data), checksum=hashlib.sha256(data).hexdigest(),
            attachment_id=attachment_id, signature_valid=detected is not None,
        )
        if detected == "SVG":
            self._svg(data, record)
        else:
            self._binary_headers(data, record)
            if importlib.util.find_spec("PIL") is not None:
                self._pillow(path, record)
        if detected is None:
            record.skip_reason = "Unrecognized or invalid image signature"
            record.suspicious = True
        if record.is_animated and detected in {"WEBP", "AVIF"}:
            record.skip_reason = f"Animated {detected} requires explicit animation-safe support"
        if record.width and record.height:
            record.aspect_ratio = record.width / record.height
        record.asset_class = classify_asset(record)
        return record

    def _binary_headers(self, data: bytes, record: ImageRecord) -> None:
        if record.detected_format == "PNG" and len(data) >= 26:
            record.width, record.height = struct.unpack(">II", data[16:24])
            color_type = data[25]
            record.color_mode = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}.get(color_type)
            record.has_alpha = color_type in {4, 6} or b"tRNS" in data
            record.has_icc_profile = b"iCCP" in data
            record.has_exif = b"eXIf" in data
            alpha = _png_alpha_values(data, record.width, record.height, color_type, data[24])
            if alpha:
                record.transparency_ratio = sum(value < 255 for value in alpha) / len(alpha)
                record.has_semitransparency = any(0 < value < 255 for value in alpha)
        elif record.detected_format == "GIF" and len(data) >= 10:
            record.width, record.height = struct.unpack("<HH", data[6:10])
            record.color_mode = "P"
            record.frame_count = data.count(b"\x2c")
            record.is_animated = record.frame_count > 1
            record.has_alpha = any(data[index:index + 3] == b"\x21\xf9\x04" and data[index + 3] & 1
                                   for index in range(max(0, len(data) - 7)))
        elif record.detected_format == "WEBP" and len(data) >= 30 and data[12:16] == b"VP8X":
            flags = data[20]
            record.has_alpha = bool(flags & 0x10)
            record.is_animated = bool(flags & 0x02)
            record.frame_count = max(1, data.count(b"ANMF"))
            record.width = 1 + int.from_bytes(data[24:27], "little")
            record.height = 1 + int.from_bytes(data[27:30], "little")
        elif record.detected_format == "AVIF":
            record.is_animated = data[8:12] == b"avis"
            record.frame_count = 2 if record.is_animated else 1
        elif record.detected_format == "JPEG":
            record.color_mode = "RGB"
            record.has_exif = b"Exif\x00\x00" in data
            record.has_icc_profile = b"ICC_PROFILE\x00" in data
            self._jpeg_dimensions(data, record)

    @staticmethod
    def _jpeg_dimensions(data: bytes, record: ImageRecord) -> None:
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                record.height, record.width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return
            if offset + 4 >= len(data): return
            length = int.from_bytes(data[offset + 2:offset + 4], "big")
            if length < 2: return
            offset += length + 2

    @staticmethod
    def _pillow(path: Path, record: ImageRecord) -> None:
        image_module = importlib.import_module("PIL.Image")
        try:
            with image_module.open(path) as image:
                image.verify()
            with image_module.open(path) as image:
                record.width, record.height = image.size
                record.color_mode = image.mode
                record.frame_count = int(getattr(image, "n_frames", 1))
                record.is_animated = bool(getattr(image, "is_animated", False))
                record.has_exif = bool(image.info.get("exif"))
                record.has_icc_profile = bool(image.info.get("icc_profile"))
                exif = image.getexif()
                record.orientation = int(exif[274]) if 274 in exif else None
                if "A" in image.getbands() or "transparency" in image.info:
                    record.has_alpha = True
                    rgba = image.convert("RGBA")
                    histogram = rgba.getchannel("A").histogram()
                    total = max(1, sum(histogram))
                    record.transparency_ratio = sum(histogram[:255]) / total
                    record.has_semitransparency = sum(histogram[1:255]) > 0
        except Exception:
            record.signature_valid = False
            record.skip_reason = "Image signature was recognized but the file is not safely decodable"

    @staticmethod
    def _svg(data: bytes, record: ImageRecord) -> None:
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            record.suspicious = True
            record.skip_reason = "SVG contains a document type or entity declaration"
            return
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            record.suspicious = True
            record.skip_reason = "SVG is not well-formed XML"
            return
        record.color_mode = "VECTOR"
        record.has_alpha = b"opacity" in data or b"transparent" in data
        record.unnecessary_metadata = any(element.tag.rsplit("}", 1)[-1] in {"metadata", "desc"} for element in root.iter())
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].casefold()
            attributes = {key.rsplit("}", 1)[-1].casefold(): value for key, value in element.attrib.items()}
            if tag in {"script", "foreignobject"} or any(key.startswith("on") for key in attributes):
                record.suspicious = True
            if any(value.strip().casefold().startswith(("javascript:", "http:", "https:")) for value in attributes.values()):
                record.suspicious = True
        record.width = _svg_number(root.attrib.get("width"))
        record.height = _svg_number(root.attrib.get("height"))
        if (not record.width or not record.height) and root.attrib.get("viewBox"):
            values = root.attrib["viewBox"].replace(",", " ").split()
            if len(values) == 4:
                record.width, record.height = int(float(values[2])), int(float(values[3]))
        if record.suspicious:
            record.skip_reason = "SVG contains active or external content"


def _svg_number(value: str | None) -> int | None:
    if not value: return None
    match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", value)
    return int(float(match.group(1))) if match else None


def _png_alpha_values(data: bytes, width: int, height: int, color_type: int, bit_depth: int) -> list[int]:
    """Decode non-interlaced 8-bit PNG alpha scanlines without retaining color pixels."""
    if bit_depth != 8 or color_type not in {4, 6}:
        return []
    offset, compressed, interlace = 8, bytearray(), 1
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR" and len(payload) >= 13:
            interlace = payload[12]
        elif kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12
    if not compressed or interlace:
        return []
    channels = 2 if color_type == 4 else 4
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error:
        return []
    previous = bytearray(stride)
    cursor, alphas = 0, []
    for _ in range(height):
        if cursor + stride + 1 > len(raw):
            return []
        filter_type = raw[cursor]
        row = bytearray(raw[cursor + 1:cursor + 1 + stride])
        cursor += stride + 1
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (row[index] + left) & 255
            elif filter_type == 2:
                row[index] = (row[index] + above) & 255
            elif filter_type == 3:
                row[index] = (row[index] + ((left + above) // 2)) & 255
            elif filter_type == 4:
                estimate = left + above - upper_left
                distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
                predictor = (left, above, upper_left)[distances.index(min(distances))]
                row[index] = (row[index] + predictor) & 255
            elif filter_type != 0:
                return []
        alphas.extend(row[channels - 1::channels])
        previous = row
    return alphas


def classify_asset(record: ImageRecord) -> AssetClass:
    name = record.filename.casefold()
    path = record.remote_path.casefold()
    if record.detected_format == "SVG": return AssetClass.VECTOR
    if record.is_animated: return AssetClass.ANIMATION
    if "product" in path and record.detected_format in {"JPEG", "WEBP", "AVIF"}: return AssetClass.PRODUCT_PHOTO
    if "logo" in name: return AssetClass.LOGO
    if "icon" in name or (record.width and record.height and max(record.width, record.height) <= 128): return AssetClass.ICON
    if record.derivative_kind or "thumb" in name: return AssetClass.THUMBNAIL
    if "screenshot" in name: return AssetClass.SCREENSHOT
    if "background" in name or "/background" in path: return AssetClass.BACKGROUND
    if record.has_alpha: return AssetClass.TRANSPARENT_GRAPHIC
    if record.detected_format in {"JPEG", "WEBP", "AVIF"}: return AssetClass.PHOTO
    return AssetClass.UNKNOWN
