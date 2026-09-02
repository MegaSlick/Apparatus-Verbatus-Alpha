"""The pod-side golden-page pieces ``bootstrap_main`` wires around ``VisionSmokeCall``.

``VisionSmokeCall`` itself is proven in ``test_manager.py``.  What is proven
here is the wiring that lets a pod be its own fixture author: a witness drawn
from the CSPRNG inside the callable's own alphabet and bound, a rendered page
the decoder accepts under the smallest tier's pixel cap, and a utilization
sampler that reports one measurement or none -- never a guessed number.  No
GPU, no ``nvidia-smi`` and no network are touched; the sampler's process
runner is injected.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image

from .errors import ServingConfigurationError
from .smoke import (
    NvidiaSmiUtilization,
    VisionSmokeCall,
    fresh_page_witness,
    render_golden_page,
)


def test_a_fresh_witness_is_one_the_smoke_callable_accepts_and_two_draws_differ() -> None:
    first = fresh_page_witness()
    second = fresh_page_witness()

    VisionSmokeCall(first)  # the callable's own bounds and alphabet, at construction
    VisionSmokeCall(second)
    assert first != second
    assert 32 <= len(first) <= 128


def test_the_rendered_golden_page_is_a_decodable_png_under_the_smallest_tier_cap(
    tmp_path: Path,
) -> None:
    witness = fresh_page_witness()
    page = tmp_path / "preflight" / "golden-page.png"

    encoded = render_golden_page(page, witness)

    assert page.read_bytes() == encoded
    with Image.open(page) as image:
        width, height = image.size
        assert image.format == "PNG"
    # 1344 is generic-24gb's longest-edge cap (config/pod_placement.toml); the
    # smoke refuses a page past the measured tier's square of it.
    assert width * height <= 1344 * 1344
    # The witness is in the pixels and nowhere in the bytes as text: a page
    # that carried it as metadata would prove nothing about reading.
    assert witness.encode() not in encoded


def test_two_differently_witnessed_pages_render_different_pixels(tmp_path: Path) -> None:
    # A render_golden_page that stopped drawing the witness (a blank white
    # page) would still be a decodable PNG under the pixel cap with no
    # plaintext witness in the bytes -- every assertion above would still
    # pass. Only comparing actual pixels across two distinct witnesses closes
    # that gap.
    first_witness = fresh_page_witness()
    second_witness = fresh_page_witness()
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"

    render_golden_page(first_path, first_witness)
    render_golden_page(second_path, second_witness)

    with Image.open(first_path) as first_image, Image.open(second_path) as second_image:
        first_pixels = first_image.convert("L").tobytes()
        second_pixels = second_image.convert("L").tobytes()

    assert first_pixels != second_pixels
    # And neither page is blank: some pixel must actually carry ink.
    assert any(byte != 255 for byte in first_pixels)
    assert any(byte != 255 for byte in second_pixels)


def test_rendering_refuses_a_witness_the_smoke_would_refuse(tmp_path: Path) -> None:
    with pytest.raises(ServingConfigurationError):
        render_golden_page(tmp_path / "page.png", "too-short")
    assert not (tmp_path / "page.png").exists()


def test_the_witness_line_fits_inside_the_page_bounds_for_the_worst_case_width(
    tmp_path: Path,
) -> None:
    # `W` is one of the widest glyphs in the golden-page font; a witness built
    # entirely of it is close to the widest line the CSPRNG could ever draw
    # (43 URL-safe characters, the production entropy length). At the fixed
    # 40pt this line overruns the page and PIL clips it silently at the
    # canvas edge; render_golden_page must shrink the font (or refuse) rather
    # than let that happen.
    worst_case_witness = "W" * 43
    page = tmp_path / "worst-case.png"

    render_golden_page(page, worst_case_witness)

    with Image.open(page) as image:
        # Every non-white pixel must sit strictly inside the canvas -- nothing
        # drawn flush against the right or bottom edge, which is what a
        # silently clipped line looks like.
        pixels = image.convert("L").load()
        width, height = image.size
        ink_columns = [x for x in range(width) for y in range(height) if pixels[x, y] != 255]
        assert ink_columns, "expected the rendered witness to leave visible ink"
        assert max(ink_columns) < width - 1


def test_rendering_refuses_a_witness_too_wide_to_fit_even_at_the_legibility_floor(
    tmp_path: Path,
) -> None:
    # A witness at the callable's own maximum length, built from the widest
    # glyphs, cannot be shrunk to fit the page even at the legibility floor.
    # render_golden_page must refuse rather than draw a clipped page.
    too_wide_witness = "W" * 128
    page = tmp_path / "too-wide.png"

    with pytest.raises(ServingConfigurationError):
        render_golden_page(page, too_wide_witness)
    assert not page.exists()


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["nvidia-smi"], returncode, stdout, "")


def test_the_sampler_reports_one_measured_sample_from_nvidia_smi_and_the_load_average() -> None:
    sampler = NvidiaSmiUtilization(
        runner=lambda argv: _completed("71\n"),
        load_average=lambda: (2.0, 0.0, 0.0),
        cpu_count=lambda: 8,
    )

    samples = sampler()

    assert len(samples) == 1
    assert samples[0].gpu_percent == Decimal("71")
    assert samples[0].cpu_percent == Decimal("25.0")


def test_the_sampler_clips_the_cpu_figure_at_one_hundred_percent() -> None:
    sampler = NvidiaSmiUtilization(
        runner=lambda argv: _completed("3\n"),
        load_average=lambda: (64.0, 0.0, 0.0),
        cpu_count=lambda: 2,
    )

    assert sampler()[0].cpu_percent == Decimal("100")


@pytest.mark.parametrize(
    "runner",
    [
        lambda argv: _completed("", returncode=1),
        lambda argv: _completed("not a number\n"),
        lambda argv: _completed(""),
        lambda argv: (_ for _ in ()).throw(OSError("no nvidia-smi on PATH")),
        lambda argv: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 30)),
    ],
)
def test_an_unmeasurable_card_yields_no_sample_rather_than_a_number(runner) -> None:  # type: ignore[no-untyped-def]
    """An empty tuple is what ``PreflightRunner`` turns into ``utilization-missing``."""

    sampler = NvidiaSmiUtilization(
        runner=runner, load_average=lambda: (1.0, 0.0, 0.0), cpu_count=lambda: 4
    )

    assert sampler() == ()


def test_a_host_with_no_countable_cpus_yields_no_sample() -> None:
    sampler = NvidiaSmiUtilization(
        runner=lambda argv: _completed("50\n"),
        load_average=lambda: (1.0, 0.0, 0.0),
        cpu_count=lambda: None,
    )

    assert sampler() == ()
