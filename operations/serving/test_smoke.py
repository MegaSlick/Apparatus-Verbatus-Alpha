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


def test_rendering_refuses_a_witness_the_smoke_would_refuse(tmp_path: Path) -> None:
    with pytest.raises(ServingConfigurationError):
        render_golden_page(tmp_path / "page.png", "too-short")
    assert not (tmp_path / "page.png").exists()


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
