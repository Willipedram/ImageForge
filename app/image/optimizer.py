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
from app.database.decisions import DecisionManifestRepository
from app.database.optimization import OptimizationRepository
from app.image.decision import (
    CandidateAssessment, DecisionProfile, FormatDecisionEngine, OriginalFacts,
    OutputChoice, PROFILES, ProfileThresholds,
)
from app.image.intelligence import detect_format
from app.image.models import AssetClass
from app.image.optimization_models import (
    CandidateResult, OptimizationConfig, OptimizationDecision, OptimizationResult,
)
from app.image.quality import QualityPipeline


class OfflineOptimizer:
    """Processes a single local file at a time and never touches the remote server."""

    def __init__(self, project_data: Path, repository: OptimizationRepository | None = None,
                 config: OptimizationConfig | None = None, resources: ResourceManager | None = None,
                 quality: QualityPipeline | None = None,
                 manifests: DecisionManifestRepository | None = None) -> None:
        self.project_data = project_data
        self.repository = repository
        self.config = config or OptimizationConfig()
        self.resources = resources or ResourceManager(ResourceLimits(self.config.max_workers, self.config.max_pixels))
        self.quality = quality or QualityPipeline()
        profile = DecisionProfile(self.config.decision_profile)
        defaults = PROFILES[profile]
        self.thresholds = ProfileThresholds(
            defaults.minimum_ssim,
            self.config.minimum_psnr if self.config.minimum_psnr is not None else defaults.minimum_psnr,
            defaults.maximum_perceptual_difference,
            self.config.minimum_savings_ratio if self.config.minimum_savings_ratio is not None else defaults.minimum_savings_ratio,
            self.config.minimum_savings_bytes if self.config.minimum_savings_bytes is not None else defaults.minimum_savings_bytes,
        )
        self.decision_engine = FormatDecisionEngine(profile, self.thresholds)
        self.manifests = manifests or (DecisionManifestRepository(repository.jobs) if repository else None)

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
                    job_id, original_path, original_bytes, normalized.size, alpha_before,
                    self._classify(original_path, normalized, alpha_before), validated,
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
        validation_started = time.monotonic()
        reason, passed, psnr = "Validated", True, None
        width = height = 0
        alpha = (False, None, False)
        orientation_correct = color_valid = False
        file_exists, signature_valid, decodable = path.is_file(), False, False
        try:
            with path.open("rb") as stream:
                signature = detect_format(stream.read(64))
            if signature != format_name:
                raise ValueError("Encoded signature does not match candidate format")
            signature_valid = True
            with image_module.open(path) as image:
                image.verify()
            with image_module.open(path) as image:
                image.load()
                decodable = True
                width, height = image.size
                alpha = self._alpha_metrics(image)
                orientation_correct = image.getexif().get(274, 1) in {None, 1}
                color_valid = image.mode not in {"CMYK", "LAB", "HSV", "I", "F"} and not image.mode.startswith("I;16")
                if image.size != source_image.size:
                    raise ValueError("Dimensions changed")
                if not orientation_correct:
                    raise ValueError("Orientation metadata was not normalized")
                if not color_valid:
                    raise ValueError("Candidate color mode is unsafe")
                if alpha != alpha_before:
                    raise ValueError("Transparency or semitransparency changed")
                if require_icc and not image.info.get("icc_profile"):
                    raise ValueError("ICC profile was not preserved")
                metrics = self.quality.evaluate(source_image, image)
                metric_values = {name: metric.value for name, metric in metrics.items()}
                psnr = metric_values.get("psnr")
        except Exception as exc:
            passed, reason = False, str(exc)
            metric_values = {}
        return CandidateResult(
            path=str(path), format=format_name, bytes=path.stat().st_size if path.exists() else 0,
            parameters=parameters, width=width, height=height, has_alpha=alpha[0],
            transparency_ratio=alpha[1], has_semitransparency=alpha[2],
            checksum=self._checksum(path) if path.exists() else "", validation_passed=passed,
            validation_reason=reason, psnr=psnr, metrics=metric_values,
            orientation_correct=orientation_correct, color_valid=color_valid,
            corruption_free=decodable, processing_seconds=time.monotonic() - validation_started,
            file_exists=file_exists, signature_valid=signature_valid, decodable=decodable,
        )

    def _select(self, job_id: str, original_path: str, original_bytes: int,
                dimensions: tuple[int, int], alpha: tuple[bool, float | None, bool], asset_class: AssetClass,
                candidates: list[CandidateResult]) -> OptimizationResult:
        original = OriginalFacts(
            original_bytes, dimensions[0], dimensions[1], *alpha,
            asset_class=asset_class, text_heavy=asset_class is AssetClass.SCREENSHOT,
        )
        assessments = [self._assessment(candidate) for candidate in candidates]
        decision = self.decision_engine.decide(original, assessments)
        if decision.selected is None:
            if self.manifests:
                self.manifests.save(job_id, original_path, original, decision, self.thresholds)
            self._remove_candidates(candidates)
            return OptimizationResult(
                job_id, original_path, original_bytes,
                OptimizationDecision.SKIPPED if decision.choice is OutputChoice.SKIP else OptimizationDecision.RETAINED_ORIGINAL,
                decision.reason, width=dimensions[0], height=dimensions[1], has_alpha=alpha[0],
                transparency_ratio=alpha[1], has_semitransparency=alpha[2], confidence=decision.confidence.value,
            )
        selected = next(candidate for candidate in candidates if candidate.path == decision.selected.path)
        processed = self.project_data / "jobs" / job_id / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        stem = PurePosixPath(original_path).stem
        safe_stem = "".join(character if character.isalnum() or character in "-_" else "_" for character in stem)[:80]
        destination = processed / f"{safe_stem}-{hashlib.sha256(original_path.encode()).hexdigest()[:10]}.{selected.format.casefold()}"
        Path(selected.path).replace(destination)
        selected.path = str(destination)
        decision.selected.path = str(destination)
        if self.config.preserve_candidate_previews:
            preview_directory = processed / "previews"
            preview_directory.mkdir(exist_ok=True)
            for index, (candidate, assessment) in enumerate(zip(candidates, assessments)):
                if candidate is selected or not candidate.validation_passed or candidate.format not in {"WEBP", "AVIF"}:
                    if candidate is not selected:
                        Path(candidate.path).unlink(missing_ok=True)
                    continue
                preview = preview_directory / (
                    f"{hashlib.sha256(original_path.encode()).hexdigest()[:10]}-"
                    f"{candidate.format.casefold()}-{index}-{candidate.bytes}{Path(candidate.path).suffix}"
                )
                Path(candidate.path).replace(preview)
                candidate.path = assessment.path = str(preview)
        else:
            self._remove_candidates(candidate for candidate in candidates if candidate is not selected)
        if self.manifests:
            self.manifests.save(job_id, original_path, original, decision, self.thresholds)
        savings = original_bytes - selected.bytes
        return OptimizationResult(
            job_id, original_path, original_bytes, OptimizationDecision.SELECTED,
            decision.reason,
            selected.path, selected.format, selected.bytes, selected.parameters,
            selected.width, selected.height, selected.has_alpha, selected.transparency_ratio,
            selected.has_semitransparency, selected.checksum, True, selected.validation_reason,
            decision.confidence.value, savings, savings / original_bytes,
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
        minimum = max(self.thresholds.minimum_savings_bytes, math.ceil(original_bytes * self.thresholds.minimum_savings_ratio))
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
            confidence="HIGH",
        )

    def _assessment(self, candidate: CandidateResult) -> CandidateAssessment:
        return CandidateAssessment(
            candidate.format, candidate.path, candidate.bytes,
            file_exists=candidate.file_exists, compatible=candidate.format in self.config.compatible_formats,
            signature_valid=candidate.signature_valid,
            decodable=candidate.decodable, width=candidate.width, height=candidate.height,
            orientation_correct=candidate.orientation_correct, has_alpha=candidate.has_alpha,
            transparency_ratio=candidate.transparency_ratio,
            has_semitransparency=candidate.has_semitransparency, color_valid=candidate.color_valid,
            corruption_free=candidate.corruption_free, metrics=candidate.metrics,
            processing_seconds=candidate.processing_seconds, parameters=candidate.parameters,
            checksum=candidate.checksum,
        )

    @staticmethod
    def _classify(original_path: str, image, alpha: tuple[bool, float | None, bool]) -> AssetClass:
        name = PurePosixPath(original_path).name.casefold()
        if "logo" in name:
            return AssetClass.LOGO
        if "icon" in name or max(image.size) <= 128:
            return AssetClass.ICON
        if "screenshot" in name:
            return AssetClass.SCREENSHOT
        if alpha[0]:
            return AssetClass.TRANSPARENT_GRAPHIC
        return AssetClass.PHOTO

    @staticmethod
    def _alpha_metrics(image) -> tuple[bool, float | None, bool]:
        if "A" not in image.getbands() and "transparency" not in image.info:
            return False, None, False
        alpha = image.convert("RGBA").getchannel("A").histogram()
        total = max(1, sum(alpha))
        return True, sum(alpha[:255]) / total, sum(alpha[1:255]) > 0

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
        if self.manifests and self.manifests.get(result.job_id, result.original_path) is None:
            self.manifests.save_terminal_result(
                result, self.decision_engine.profile.value, self.thresholds
            )
        return result

    @staticmethod
    def _retained(job_id: str, original_path: str, original_bytes: int, reason: str,
                  *, failed: bool = False, width: int | None = None, height: int | None = None) -> OptimizationResult:
        return OptimizationResult(
            job_id, original_path, original_bytes,
            OptimizationDecision.FAILED if failed else OptimizationDecision.RETAINED_ORIGINAL,
            reason, width=width, height=height,
        )
