"""Bounded public-URL verification with cache/CDN awareness."""

from __future__ import annotations

import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from app.image.intelligence import FORMAT_MIMES, ImageAnalyzer, detect_format
from app.safety.models import HTTPResponse, HTTPVerification


class HTTPVerifier:
    def __init__(self, client: Callable[[str, int], HTTPResponse] | None = None,
                 *, maximum_bytes: int = 64 * 1024 * 1024) -> None:
        self.client = client or self._get
        self.maximum_bytes = maximum_bytes

    def verify(self, url: str, expected_format: str, expected_dimensions: tuple[int | None, int | None]) -> HTTPVerification:
        errors: list[str] = []
        try:
            response = self.client(url, self.maximum_bytes)
        except Exception as exc:
            return HTTPVerification(False, None, None, None, None, None, (),
                                    (f"HTTP request failed: {type(exc).__name__}: {exc}",))
        headers = {key.casefold(): value for key, value in response.headers.items()}
        mime = headers.get("content-type", "").split(";", 1)[0].strip().casefold() or None
        detected = detect_format(response.body[:2048])
        if not 200 <= response.status < 300: errors.append(f"HTTP status was {response.status}.")
        if mime != FORMAT_MIMES.get(expected_format): errors.append(f"HTTP MIME was {mime or 'missing'}.")
        if detected != expected_format: errors.append("HTTP body signature did not match the selected format.")
        width = height = None
        if detected:
            with tempfile.TemporaryDirectory(prefix="imageforge-http-") as directory:
                path = Path(directory) / "response.image"; path.write_bytes(response.body)
                record = ImageAnalyzer().analyze(path, job_id="http-verification", remote_path=url)
                width, height = record.width, record.height
                if not record.signature_valid: errors.append("HTTP image was not decodable.")
        expected_width, expected_height = expected_dimensions
        if expected_width and expected_height and (width, height) != expected_dimensions:
            errors.append(f"HTTP dimensions were {width}x{height}, expected {expected_width}x{expected_height}.")
        return HTTPVerification(not errors, response.status, mime, detected, width, height,
                                _cache_layers(headers), tuple(errors))

    @staticmethod
    def _get(url: str, maximum_bytes: int) -> HTTPResponse:
        request = urllib.request.Request(url, headers={"User-Agent": "ImageForge/0.1", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(maximum_bytes + 1)
            if len(body) > maximum_bytes: raise ValueError("HTTP image exceeds the verification size limit.")
            return HTTPResponse(response.status, dict(response.headers.items()), body, response.geturl())


def _cache_layers(headers: dict[str, str]) -> tuple[str, ...]:
    layers: list[str] = []
    server = headers.get("server", "").casefold()
    if "cloudflare" in server or "cf-cache-status" in headers: layers.append("Cloudflare")
    if "litespeed" in server or "x-litespeed-cache" in headers: layers.append("LiteSpeed Cache")
    if any(key in headers for key in ("x-cache", "x-cache-status", "age", "via")): layers.append("CDN/proxy cache")
    if any(key.startswith("x-wp-") for key in headers): layers.append("WordPress cache/plugin")
    return tuple(dict.fromkeys(layers))
