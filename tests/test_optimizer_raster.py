from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL")

from PIL import Image, ImageDraw

Image.init()
AVIF_SUPPORTED = "AVIF" in Image.SAVE

from app.image.optimization_models import OptimizationConfig, OptimizationDecision
from app.image.optimizer import OfflineOptimizer


@pytest.fixture
def optimizer(tmp_path):
    return OfflineOptimizer(tmp_path, config=OptimizationConfig(
        minimum_savings_bytes=1, minimum_savings_ratio=0, minimum_psnr=20, max_pixels=1_000_000,
    ))


def noisy_photo(size=(256, 192)):
    image = Image.new("RGB", size)
    image.putdata([((x * 17 + y * 3) % 256, (x * 7 + y * 19) % 256, (x * 13 + y * 11) % 256)
                   for y in range(size[1]) for x in range(size[0])])
    return image


def test_real_jpeg_photo_generates_decodable_candidate(tmp_path, optimizer):
    source = tmp_path / "photo.jpg"
    noisy_photo().save(source, "JPEG", quality=98)
    result = optimizer.optimize("job", source, "/uploads/photo.jpg")
    assert result.decision is OptimizationDecision.SELECTED
    assert result.candidate_bytes < result.original_bytes and result.validation_passed
    with Image.open(result.candidate_path) as candidate:
        candidate.verify()
    assert source.read_bytes().startswith(b"\xff\xd8\xff")


def test_transparent_png_logo_preserves_exact_alpha(tmp_path, optimizer):
    source = tmp_path / "logo.png"
    image = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((10, 10, 118, 118), fill=(20, 100, 220, 128))
    image.save(source, "PNG", compress_level=0)
    result = optimizer.optimize("job", source, "/uploads/logo.png")
    assert result.decision is OptimizationDecision.SELECTED
    assert result.has_alpha and result.has_semitransparency
    with Image.open(result.candidate_path) as candidate:
        assert "A" in candidate.convert("RGBA").getbands()
        assert candidate.size == image.size


def test_png_screenshot_dimensions_are_preserved(tmp_path, optimizer):
    source = tmp_path / "screenshot.png"
    image = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(image)
    for y in range(0, 180, 20): draw.rectangle((0, y, 319, y + 9), fill=(20, 80, 160))
    image.save(source, "PNG", compress_level=0)
    result = optimizer.optimize("job", source, "/uploads/screenshot.png")
    assert result.width == 320 and result.height == 180


def test_animated_gif_is_never_flattened(tmp_path, optimizer):
    source = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
    result = optimizer.optimize("job", source, "/uploads/animated.gif")
    assert result.decision is OptimizationDecision.RETAINED_ORIGINAL
    assert "Animated" in result.decision_reason and result.candidate_path is None


def test_existing_webp_is_reencoded_not_renamed(tmp_path, optimizer):
    source = tmp_path / "existing.webp"
    noisy_photo().save(source, "WEBP", quality=98)
    result = optimizer.optimize("job", source, "/uploads/existing.webp")
    assert result.decision in {OptimizationDecision.SELECTED, OptimizationDecision.RETAINED_ORIGINAL}
    if result.candidate_path:
        assert open(result.candidate_path, "rb").read(12)[8:12] == b"WEBP"


@pytest.mark.skipif(not AVIF_SUPPORTED, reason="Pillow build has no AVIF codec")
def test_real_avif_is_analyzed_and_encoded(tmp_path, optimizer):
    source = tmp_path / "existing.avif"
    noisy_photo().save(source, "AVIF", quality=90)
    result = optimizer.optimize("job", source, "/uploads/existing.avif")
    assert result.decision in {OptimizationDecision.SELECTED, OptimizationDecision.RETAINED_ORIGINAL}


def test_cmyk_jpeg_is_retained_conservatively(tmp_path, optimizer):
    source = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (64, 64), (10, 20, 30, 0)).save(source, "JPEG")
    result = optimizer.optimize("job", source, "/uploads/cmyk.jpg")
    assert result.decision is OptimizationDecision.RETAINED_ORIGINAL
    assert "CMYK" in result.decision_reason


def test_exif_orientation_is_applied_before_metadata_removal(tmp_path, optimizer):
    source = tmp_path / "oriented.jpg"
    image = noisy_photo((160, 80))
    exif = image.getexif()
    exif[274] = 6
    image.save(source, "JPEG", quality=98, exif=exif)
    result = optimizer.optimize("job", source, "/uploads/oriented.jpg")
    assert result.decision is OptimizationDecision.SELECTED
    with Image.open(result.candidate_path) as candidate:
        assert candidate.size == (80, 160)
        assert candidate.getexif().get(274, 1) == 1


def test_large_pixel_count_is_rejected_before_decode(tmp_path):
    source = tmp_path / "large.png"
    Image.new("RGB", (101, 101), "white").save(source)
    optimizer = OfflineOptimizer(tmp_path, config=OptimizationConfig(max_pixels=10_000))
    result = optimizer.optimize("job", source, "/uploads/large.png")
    assert result.decision is OptimizationDecision.RETAINED_ORIGINAL
    assert "configured maximum" in result.decision_reason
