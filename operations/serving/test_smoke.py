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

import math
import secrets
import subprocess
from decimal import Decimal
from pathlib import Path
from string import ascii_letters, digits

import pytest
from PIL import Image, ImageDraw, ImageFont

from .errors import ServingConfigurationError
from .smoke import (
    NvidiaSmiUtilization,
    VisionSmokeCall,
    answer_is_page_witness,
    fresh_page_witness,
    page_witness_edit_distance,
    render_golden_page,
)
from .witness import PAGE_WITNESS_ALPHABET, PAGE_WITNESS_LENGTH

TEST_WITNESS = (PAGE_WITNESS_ALPHABET * 2)[:PAGE_WITNESS_LENGTH]


def test_smoke_accepts_two_reading_slips_but_refuses_broken_answers() -> None:
    assert answer_is_page_witness(f"PAGE-WITNESS: {TEST_WITNESS}", TEST_WITNESS)
    assert answer_is_page_witness(
        f"PAGE-WITNESS: C{TEST_WITNESS[1:20]} P{TEST_WITNESS[21:]}", TEST_WITNESS
    )
    for answer in (
        "",
        TEST_WITNESS,
        f"PAGE-WITNESS: {TEST_WITNESS[:10]}",
        f"PAGE-WITNESS: {'A' * PAGE_WITNESS_LENGTH}",
        f"PAGE-WITNESS: {TEST_WITNESS} ",
        f"PAGE-WITNESS: {TEST_WITNESS[:20]}!{TEST_WITNESS[21:]}",
    ):
        assert not answer_is_page_witness(answer, TEST_WITNESS)


@pytest.mark.parametrize(
    ("code", "distance"),
    [
        (TEST_WITNESS, 0),
        ("Z" + TEST_WITNESS[1:], 1),
        (TEST_WITNESS[:20] + "A" + TEST_WITNESS[20:], 1),
        (TEST_WITNESS[:-1], 1),
        (TEST_WITNESS[:-2], 2),
    ],
)
def test_smoke_records_substitution_insertion_and_truncation_distance(
    code: str, distance: int
) -> None:
    assert page_witness_edit_distance(f"PAGE-WITNESS: {code}", TEST_WITNESS) == distance
    assert answer_is_page_witness(f"PAGE-WITNESS: {code}", TEST_WITNESS)


def test_a_fresh_witness_is_one_the_smoke_callable_accepts_and_two_draws_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices = iter(PAGE_WITNESS_ALPHABET * 3)
    monkeypatch.setattr(secrets, "choice", lambda _alphabet: next(choices))
    first = fresh_page_witness()
    second = fresh_page_witness()

    VisionSmokeCall(first)  # the callable's own bounds and alphabet, at construction
    VisionSmokeCall(second)
    assert first != second
    assert len(first) == PAGE_WITNESS_LENGTH
    assert set(first) <= set(PAGE_WITNESS_ALPHABET)
    excluded = "IlL10OoQDcCoOpPsSuUvVwWxXzZkK"
    assert set(PAGE_WITNESS_ALPHABET) == set(ascii_letters + digits) - set(excluded)
    assert not set(first) & set(excluded)
    assert all(first[index] != first[index + 1] for index in range(len(first) - 1))
    assert len(first) * math.log2(len(PAGE_WITNESS_ALPHABET)) >= 200


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


def test_a_second_render_never_writes_over_a_different_golden_page(tmp_path: Path) -> None:
    """Evidence is added, never replaced (principle 4).

    The preflight receipts beside the page are content-addressed and refuse
    differing bytes at one address, so a repeated or resumed preflight cannot
    silently overwrite the page they point at.
    """

    page = tmp_path / "preflight" / "golden-page.png"
    first = render_golden_page(page, fresh_page_witness())

    with pytest.raises(ServingConfigurationError, match="never written over"):
        render_golden_page(page, fresh_page_witness())

    assert page.read_bytes() == first
    assert [item.name for item in page.parent.iterdir()] == ["golden-page.png"]


def test_re_rendering_the_same_witness_is_a_no_op_not_a_refusal(tmp_path: Path) -> None:
    """Identical bytes at the same name are the same evidence, not a conflict."""

    witness = fresh_page_witness()
    page = tmp_path / "preflight" / "golden-page.png"

    first = render_golden_page(page, witness)
    second = render_golden_page(page, witness)

    assert first == second == page.read_bytes()
    assert [item.name for item in page.parent.iterdir()] == ["golden-page.png"]


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
    # `W` is one of the widest glyphs in the golden-page font. At the fixed
    # 40pt this line can overrun the page and PIL clips it silently at the
    # canvas edge; render_golden_page must shrink the font (or refuse) rather
    # than let that happen.
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    font = ImageFont.load_default(size=40)
    widest_pair = max(
        (
            (left, right)
            for left in PAGE_WITNESS_ALPHABET
            for right in PAGE_WITNESS_ALPHABET
            if left != right
        ),
        key=lambda pair: draw.textlength(
            "".join(pair[index % 2] for index in range(PAGE_WITNESS_LENGTH)), font=font
        ),
    )
    worst_case_witness = "".join(widest_pair[index % 2] for index in range(PAGE_WITNESS_LENGTH))
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


def test_rendering_refuses_a_witness_that_exceeds_the_generator_length(
    tmp_path: Path,
) -> None:
    too_long_witness = "".join("MY"[index % 2] for index in range(PAGE_WITNESS_LENGTH + 1))
    page = tmp_path / "too-long.png"

    with pytest.raises(ServingConfigurationError):
        render_golden_page(page, too_long_witness)
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


def test_the_sampler_averages_every_visible_card_not_only_the_first() -> None:
    """`nvidia-smi` prints one line per visible GPU, and all must be averaged."""

    sampler = NvidiaSmiUtilization(
        runner=lambda argv: _completed("60\n40\n"),
        load_average=lambda: (2.0, 0.0, 0.0),
        cpu_count=lambda: 8,
    )

    samples = sampler()

    assert len(samples) == 1
    assert samples[0].gpu_percent == Decimal("50")


def test_a_host_with_no_countable_cpus_yields_no_sample() -> None:
    sampler = NvidiaSmiUtilization(
        runner=lambda argv: _completed("50\n"),
        load_average=lambda: (1.0, 0.0, 0.0),
        cpu_count=lambda: None,
    )

    assert sampler() == ()
