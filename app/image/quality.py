"""Pluggable visual-quality metrics independent of candidate selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class QualityMetric:
    name: str
    value: float
    higher_is_better: bool = True


class QualityEvaluator(Protocol):
    name: str

    def evaluate(self, reference, candidate) -> QualityMetric: ...


class PSNREvaluator:
    name = "psnr"

    def evaluate(self, reference, candidate) -> QualityMetric:
        difference = _difference_histogram(reference, candidate)
        squared = sum((index % 256) ** 2 * count for index, count in enumerate(difference))
        pixels = max(1, reference.width * reference.height * 3)
        mse = squared / pixels
        return QualityMetric(self.name, math.inf if mse == 0 else 20 * math.log10(255 / math.sqrt(mse)))


class SSIMEvaluator:
    """Global luminance SSIM; replaceable by tiled/multiscale implementations."""

    name = "ssim"

    def evaluate(self, reference, candidate) -> QualityMetric:
        if reference.size != candidate.size or not reference.width or not reference.height:
            return QualityMetric(self.name, 0.0)
        import importlib

        image_math = importlib.import_module("PIL.ImageMath")
        image_stat = importlib.import_module("PIL.ImageStat")
        left, right = reference.convert("L"), candidate.convert("L")
        left_stat, right_stat = image_stat.Stat(left), image_stat.Stat(right)
        mean_left, mean_right = left_stat.mean[0], right_stat.mean[0]
        variance_left, variance_right = left_stat.var[0], right_stat.var[0]
        product = image_math.eval("float(a * b)", a=left, b=right)
        covariance = image_stat.Stat(product).mean[0] - mean_left * mean_right
        c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
        score = ((2 * mean_left * mean_right + c1) * (2 * covariance + c2)) / (
            (mean_left ** 2 + mean_right ** 2 + c1) * (variance_left + variance_right + c2)
        )
        return QualityMetric(self.name, max(-1.0, min(1.0, score)))


class PerceptualDifferenceEvaluator:
    """Normalized RGB mean absolute difference; lower means more alike."""

    name = "perceptual_difference"

    def evaluate(self, reference, candidate) -> QualityMetric:
        left = reference.convert("RGB")
        right = candidate.convert("RGB")
        histogram = _difference_histogram(left, right)
        absolute = sum((index % 256) * count for index, count in enumerate(histogram))
        value = absolute / max(1, left.width * left.height * 3 * 255)
        return QualityMetric(self.name, value, higher_is_better=False)


class QualityPipeline:
    def __init__(self, evaluators: tuple[QualityEvaluator, ...] | None = None) -> None:
        self.evaluators = evaluators or (SSIMEvaluator(), PSNREvaluator(), PerceptualDifferenceEvaluator())

    def evaluate(self, reference, candidate) -> dict[str, QualityMetric]:
        return {evaluator.name: evaluator.evaluate(reference, candidate) for evaluator in self.evaluators}


def _difference_histogram(reference, candidate) -> list[int]:
    if reference.size != candidate.size:
        return [0] * 768
    import importlib

    image_chops = importlib.import_module("PIL.ImageChops")
    return image_chops.difference(reference.convert("RGB"), candidate.convert("RGB")).histogram()
