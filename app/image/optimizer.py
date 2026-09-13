"""Offline candidate generation, validation, comparison, and selection."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import math
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from app.core.resources import ResourceLimitExceeded, ResourceLimits, ResourceManager
from app.database.optimization import OptimizationRepository
from app.image.intelligence import detect_format
from app.image.optimization_models import (
    CandidateResult, OptimizationConfig, OptimizationDecision, OptimizationResult,
)


class OfflineOptimizer:
    """Processes a single local file at a time and never touches the remote server."""

    def __init__(self, project_data: Path, repository: OptimizationRepository | None = None,
                 config: OptimizationConfig | None = None, resources: ResourceManager | None = None) -> None:
        self.project_data = project_data
        self.repository = repository
        self.config = config or OptimizationConfig()
        self.resources = resources or ResourceManager(ResourceLimits(self.config.max_workers, self.config.max_pixels))

    def optimize(self, job_id: str, source: Path, original_path: str) -> OptimizationResult:
        started = time.monotonic()
        original_bytes = source.stat().st_size
        if original_bytes > self.config.max_file_bytes:
            return self._store(self._retained(job_id, original_path, original_bytes, "File exceeds configured byte limit"))
        with source.open("rb") as stream:
            header = stream.read(64 * 1024)
        detected = detect_format(header)
        if detected is None:
            return self._store(self._retained(job_id, original_path, original_bytes, "Corrupted or unsupported image", failed=True))
        if detected == "SVG":
            return self._store(self._optimize_svg(job_id, source, original_path, original_bytes))
        if importlib.util.find_spec("PIL") is None:
            return self._store(self._retained(job_id, original_path, original_bytes, "Pillow is required for raster encoding", failed=True))

        image_module = importlib.import_module("PIL.Image")
        ops_module = importlib.import_module("PIL.ImageOps")
        try:
            with image_module.open(source) as probe:
                width, height = probe.size
                frames = int(getattr(probe, "n_frames", 1))
                mode = probe.mode
            with self.resources.reserve(width * height):
                if frames > 1:
                    return self._store(self._retained(
                        job_id, original_path, original_bytes,
                        "Animated images are retained to prevent accidental frame loss",
                        width=width, height=height,
                    ))
                if mode in {"CMYK", "LAB", "HSV", "I", "I;16", "F"} or mode.startswith("I;16"):
                    return self._store(self._retained(
                        job_id, original_path, original_bytes,
                        f"Conservative retention for color mode {mode}", width=width, height=height,
                    ))
                with image_module.open(source) as opened:
                    source_icc = opened.info.get("icc_profile")
                    source_exif = opened.getexif()
                    normalized = ops_module.exif_transpose(opened)
                    normalized.load()
                    normalized = normalized.copy()
                if 274 in source_exif:
                    del source_exif[274]
                alpha_before = self._alpha_metrics(normalized)
                candidates = self._generate_candidates(
                    normalized, detected, source_icc, source_exif.tobytes(),
                    job_id, original_path, started,
                )
                validated = []
                for candidate in candidates:
                    if time.monotonic() - started > self.config.timeout_seconds:
                        self._remove_candidates(candidates)
                        return self._store(self._retained(
                            job_id, original_path, original_bytes, "Optimization exceeded the configured time limit"
                        ))
                    validated.append(self._validate(candidate, normalized, alpha_before, bool(source_icc)))
                return self._store(self._select(
                    job_id, original_path, original_bytes, normalized.size, alpha_before, validated,
                ))
        except ResourceLimitExceeded as exc:
            return self._store(self._retained(job_id, original_path, original_bytes, str(exc)))
        except TimeoutError as exc:
            self._clear_candidate_directory(job_id)
            return self._store(self._retained(job_id, original_path, original_bytes, str(exc)))
        except Exception as exc:
            return self._store(self._retained(
                job_id, original_path, original_bytes,
                f"Input could not be decoded safely ({type(exc).__name__})", failed=True,
            ))

    def optimize_many(self, job_id: str, inputs):
        """Yield results incrementally; callers need not retain the collection."""
        for source, original_path in inputs:
            yield self.optimize(job_id, source, original_path)

    def _generate_candidates(self, image, detected: str, icc: bytes | None, exif: bytes,
                             job_id: str, original_path: str, started: float) -> list[tuple[Path, str, dict[str, object]]]:
        image_module = importlib.import_module("PIL.Image")
        image_module.init()
        directory = self.project_data / "jobs" / job_id / "processed" / ".candidates"
        directory.mkdir(parents=True, exist_ok=True)
        token = hashlib.sha256(original_path.encode("utf-8")).hexdigest()[:16]
        candidates: list[tuple[Path, str, dict[str, object]]] = []

        def save(format_name: str, suffix: str, parameters: dict[str, object]) -> None:
            if time.monotonic() - started > self.config.timeout_seconds:
                raise TimeoutError("Optimization exceeded the configured time limit")
            path = directory / f"{token}-{len(candidates)}-{format_name.casefold()}{suffix}"
            options = dict(parameters)
            if icc:
                options["icc_profile"] = icc
            if exif and format_name in {"JPEG", "WEBP", "AVIF"}:
                options["exif"] = exif
            try:
                image.save(path, format=format_name, **options)
            except (KeyError, OSError, ValueError):
                path.unlink(missing_ok=True)
                return
            candidates.append((path, format_name, parameters))

        if detected == "JPEG":
            save("JPEG", ".jpg", {
                "quality": self.config.jpeg_quality, "optimize": True,
                "progressive": True, "subsampling": 2,
            })
        elif detected == "PNG":
            save("PNG", ".png", {"optimize": True, "compress_level": 9})
        elif detected == "WEBP":
            save("WEBP", ".webp", {"quality": self.config.webp_quality, "method": 6})
        elif detected == "AVIF" and "AVIF" in image_module.SAVE:
            save("AVIF", ".avif", {"quality": self.config.avif_quality})

        colors = image.convert("RGB").getcolors(maxcolors=257)
        graphic = self._alpha_metrics(image)[0] or colors is not None
        save("WEBP", ".webp", {
            "lossless": graphic, "quality": 100 if graphic else self.config.webp_quality,
            "method": 6,
        })
        if "AVIF" in image_module.SAVE and not graphic:
            save("AVIF", ".avif", {"quality": self.config.avif_quality})
        return candidates

    def _validate(self, candidate: tuple[Path, str, dict[str, object]], source_image,
                  alpha_before: tuple[bool, float | None, bool], require_icc: bool) -> CandidateResult:
        path, format_name, parameters = candidate
        image_module = importlib.import_module("PIL.Image")
        reason, passed, psnr = "Validated", True, None
        width = height = 0
        alpha = (False, None, False)
        try:
            signature = detect_format(path.read_bytes()[:64])
            if signature != format_name:
                raise ValueError("Encoded signature does not match candidate format")
            with image_module.open(path) as image:
                image.verify()
            with image_module.open(path) as image:
                image.load()
                width, height = image.size
                alpha = self._alpha_metrics(image)
                if image.size != source_image.size:
                    raise ValueError("Dimensions changed")
                if alpha != alpha_before:
                    raise ValueError("Transparency or semitransparency changed")
                if require_icc and not image.info.get("icc_profile"):
                    raise ValueError("ICC profile was not preserved")
                psnr = self._psnr(source_image, image)
                if not parameters.get("lossless") and format_name not in {"PNG"} and psnr < self.config.minimum_psnr:
                    raise ValueError(f"Visual fidelity below threshold ({psnr:.2f} dB)")
        except Exception as exc:
            passed, reason = False, str(exc)
        return CandidateResult(
            str(path), format_name, path.stat().st_size if path.exists() else 0, parameters,
            width, height, *alpha, self._checksum(path) if path.exists() else "", passed, reason, psnr,
        )

    def _select(self, job_id: str, original_path: str, original_bytes: int,
                dimensions: tuple[int, int], alpha: tuple[bool, float | None, bool],
                candidates: list[CandidateResult]) -> OptimizationResult:
        minimum = max(self.config.minimum_savings_bytes, math.ceil(original_bytes * self.config.minimum_savings_ratio))
        eligible = [candidate for candidate in candidates if candidate.validation_passed and original_bytes - candidate.bytes >= minimum]
        if not eligible:
            self._remove_candidates(candidates)
            return self._retained(
                job_id, original_path, original_bytes,
                "No validated candidate achieved meaningful savings", width=dimensions[0], height=dimensions[1],
            )
        selected = min(eligible, key=lambda candidate: candidate.bytes)
        processed = self.project_data / "jobs" / job_id / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        stem = PurePosixPath(original_path).stem
        safe_stem = "".join(character if character.isalnum() or character in "-_" else "_" for character in stem)[:80]
        destination = processed / f"{safe_stem}-{hashlib.sha256(original_path.encode()).hexdigest()[:10]}.{selected.format.casefold()}"
        Path(selected.path).replace(destination)
        selected.path = str(destination)
        self._remove_candidates(candidate for candidate in candidates if candidate is not selected)
        savings = original_bytes - selected.bytes
        return OptimizationResult(
            job_id, original_path, original_bytes, OptimizationDecision.SELECTED,
            f"Selected smallest validated candidate with {savings:,} byte savings",
            selected.path, selected.format, selected.bytes, selected.parameters,
            selected.width, selected.height, selected.has_alpha, selected.transparency_ratio,
            selected.has_semitransparency, selected.checksum, True, selected.validation_reason,
            savings, savings / original_bytes,
        )

    def _optimize_svg(self, job_id: str, source: Path, original_path: str, original_bytes: int) -> OptimizationResult:
        data = source.read_bytes()
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            return self._retained(job_id, original_path, original_bytes, "Suspicious SVG retained", failed=True)
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return self._retained(job_id, original_path, original_bytes, "Invalid SVG XML", failed=True)
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1].casefold()
            attributes = {key.rsplit("}", 1)[-1].casefold(): value for key, value in element.attrib.items()}
            if tag in {"script", "foreignobject"} or any(key.startswith("on") for key in attributes):
                return self._retained(job_id, original_path, original_bytes, "Suspicious SVG retained", failed=True)
            if any(value.strip().casefold().startswith(("javascript:", "http:", "https:")) for value in attributes.values()):
                return self._retained(job_id, original_path, original_bytes, "SVG with external content retained", failed=True)
        for parent in root.iter():
            for child in list(parent):
                if child.tag.rsplit("}", 1)[-1] in {"metadata", "desc"}:
                    parent.remove(child)
        candidate_data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        try:
            ET.fromstring(candidate_data)
        except ET.ParseError:
            return self._retained(job_id, original_path, original_bytes, "SVG candidate validation failed", failed=True)
        savings = original_bytes - len(candidate_data)
        minimum = max(self.config.minimum_savings_bytes, math.ceil(original_bytes * self.config.minimum_savings_ratio))
        if savings < minimum:
            return self._retained(job_id, original_path, original_bytes, "Safe SVG cleanup did not provide meaningful savings")
        processed = self.project_data / "jobs" / job_id / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        destination = processed / f"{source.stem}-{hashlib.sha256(original_path.encode()).hexdigest()[:10]}.svg"
        destination.write_bytes(candidate_data)
        width = self._svg_dimension(root.attrib.get("width"))
        height = self._svg_dimension(root.attrib.get("height"))
        if (not width or not height) and root.attrib.get("viewBox"):
            values = root.attrib["viewBox"].replace(",", " ").split()
            if len(values) == 4:
                width, height = round(float(values[2])), round(float(values[3]))
        return OptimizationResult(
            job_id, original_path, original_bytes, OptimizationDecision.SELECTED,
            "Removed safe non-visual SVG metadata", str(destination), "SVG", len(candidate_data),
            {"removed": ["metadata", "desc"]}, width=width, height=height,
            checksum=self._checksum(destination),
            validation_passed=True, savings_bytes=savings, savings_ratio=savings / original_bytes,
        )

    @staticmethod
    def _alpha_metrics(image) -> tuple[bool, float | None, bool]:
        if "A" not in image.getbands() and "transparency" not in image.info:
            return False, None, False
        alpha = image.convert("RGBA").getchannel("A").histogram()
        total = max(1, sum(alpha))
        return True, sum(alpha[:255]) / total, sum(alpha[1:255]) > 0

    @staticmethod
    def _psnr(first, second) -> float:
        left, right = first.convert("RGB"), second.convert("RGB")
        histogram = importlib.import_module("PIL.ImageChops").difference(left, right).histogram()
        squared = sum((index % 256) ** 2 * count for index, count in enumerate(histogram))
        mse = squared / max(1, left.width * left.height * 3)
        return math.inf if mse == 0 else 20 * math.log10(255 / math.sqrt(mse))

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _svg_dimension(value: str | None) -> int | None:
        match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)", value or "")
        return round(float(match.group(1))) if match else None

    @staticmethod
    def _remove_candidates(candidates) -> None:
        for candidate in candidates:
            Path(candidate.path).unlink(missing_ok=True)

    def _clear_candidate_directory(self, job_id: str) -> None:
        directory = self.project_data / "jobs" / job_id / "processed" / ".candidates"
        if directory.exists():
            for path in directory.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)

    def _store(self, result: OptimizationResult) -> OptimizationResult:
        if self.repository:
            self.repository.save(result)
        return result

    @staticmethod
    def _retained(job_id: str, original_path: str, original_bytes: int, reason: str,
                  *, failed: bool = False, width: int | None = None, height: int | None = None) -> OptimizationResult:
        return OptimizationResult(
            job_id, original_path, original_bytes,
            OptimizationDecision.FAILED if failed else OptimizationDecision.RETAINED_ORIGINAL,
            reason, width=width, height=height,
        )
