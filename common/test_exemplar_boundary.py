"""The shared Exemplar boundary check, exercised at its own interface.

`common/exemplar_boundary.py` is what the Designator runs before it crops and what
the Armarium runs before it exports. `pipeline/2_designator/test_exemplar_boundary.py`
covers it end to end by damaging a real run tree, which is the right test for the
cases damage can reach: a deleted page, a rewritten corpus seal, altered or missing
pixels.

Two of its checks cannot be reached that way, and were landed with nothing
exercising them at all — found by deleting each and watching the whole suite stay
green. Both compare the sealed evidence against arguments the *caller* supplies:
the `run.json` ledger row for this ordinal, and the run authority itself. Damaging
the tree cannot produce that disagreement, because every artifact in the chain is
pinned by a digest one level up, so the outer check fires first. A caller passing
the wrong row can, and that is a stage-integration bug rather than tampering — the
kind that ships quietly because everything downstream still validates.

So they are tested here, at the boundary function's own interface, which is where
the disagreement actually lives.

The crop-lineage cases below are here for the same reason. What a crop's *bytes*
are allowed to differ by is a disagreement between the encoder that sealed a run
tree and the one re-deriving it now — two builds, never one damaged tree — and
this interface is the only place both sides can be held at once.
"""

import copy
import struct
import subprocess
import sys
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common.contracts.errors import ContractError
from common.contracts.stages import DESIGNATOR, EXEMPLAR
from common.exemplar_boundary import verify_exemplar_crop_lineage, verify_sealed_page_pixels
from common.imaging import decode_grayscale_png, encode_grayscale_png
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


@pytest.fixture
def sealed(tmp_path):
    """One real synthetic run, and its first sealed page with its ledger row."""
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "boundary-unit",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "boundary-unit")
    run = tree.read_run()
    page = next(
        record
        for record in (
            tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
            for entry in tree.build_manifest(EXEMPLAR)["artifacts"]
            if entry["kind"] == "page"
        )
        if record["outcome"] == "sealed"
    )
    source = next(
        row for row in run["source_manifest"] if row["ordinal"] == page["payload"]["ordinal"]
    )
    return tree, run, source, page


def test_the_undamaged_page_and_its_own_ledger_row_verify(sealed):
    """The check passes on the real thing, so a failure below means what it says."""
    tree, run, source, page = sealed

    verify_sealed_page_pixels(tree, run, source, page)


def test_a_page_checked_against_another_filename_ledger_row_refuses(sealed):
    """The filename is the citation link (ruling 1), so it is checked, not carried.

    A stage that walks its own list of sources and its own list of pages, and pairs
    them up wrongly, produces exactly this: a sealed page verified against a row
    naming a different file. Everything downstream of it still validates, because
    every digest in the chain is intact — it is only the *link back to Tyrel's own
    file* that is now wrong, which is the one thing no later check looks at.
    """
    tree, run, source, page = sealed
    other_name = dict(source, relative_path="a-different-scan.png")

    with pytest.raises(ContractError, match="submitted filename ledger entry"):
        verify_sealed_page_pixels(tree, run, other_name, page)


def test_a_page_checked_against_another_ledger_digest_refuses(sealed):
    tree, run, source, page = sealed
    other_digest = dict(source, sha256="f" * 64)

    with pytest.raises(ContractError, match="submitted filename ledger entry"):
        verify_sealed_page_pixels(tree, run, other_digest, page)


def test_a_ledger_fact_the_page_never_carried_refuses(sealed):
    """A ledger row can gain a fact as well as change one, and both must refuse.

    `bytes` and `ledger_sha256` are optional on a source row — the fixture path has
    no local ledger. A row that claims one where the sealed page carries none is a
    row from a different submission, not a richer description of this one.
    """
    tree, run, source, page = sealed
    invented = dict(source, ledger_sha256="a" * 64)

    with pytest.raises(ContractError, match="submitted filename ledger entry"):
        verify_sealed_page_pixels(tree, run, invented, page)


def test_a_door_admission_bound_to_a_different_run_refuses(sealed):
    """The Door admission is re-read and re-checked, not trusted for existing.

    The page names its admission by digest, so the bytes are certainly the bytes the
    Exemplar sealed. What that proves is that nobody swapped the file — not that the
    admission inside it belongs to *this* run and *this* source. A stage handed the
    wrong run authority would otherwise crop pixels admitted under a configuration
    that no longer describes them.
    """
    tree, run, source, page = sealed
    other_run = dict(run, run_id="some-other-run")

    with pytest.raises(ContractError, match="Door admission does not match this source"):
        verify_sealed_page_pixels(tree, other_run, source, page)


# --- the crop lineage check ------------------------------------------------------
#
# Opus-F3. `verify_exemplar_crop_lineage` re-derives a crop from the sealed page
# and compares. It compared raw bytes, which made the check's verdict depend on
# which zlib build re-derived it: the audit's demonstration shimmed
# `zlib.compress` to emit a valid stream at a different level -- precisely what a
# different zlib build legitimately does -- and every crop in the run was refused
# as "a Designator region does not trace to its Exemplar page", with the pixels
# untouched and reproducing exactly. A benign environment change reported as
# tampered evidence is both a false alarm and a lost one.
#
# Two changes, and these tests hold both. `crop_png` no longer writes bytes a
# library gets to choose (`common/test_imaging_determinism.py`), and the
# comparison here is on the image, which is what invariant 3 asks for and what
# lets a run tree sealed under an earlier encoder still verify.


@pytest.fixture
def cropped(tmp_path):
    """One real synthetic run, and the first act region the Designator cut."""
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            "happy",
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "crop-lineage",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "crop-lineage")
    region = next(
        record
        for record in (
            tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
            for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
            if entry["kind"] == "region"
        )
        if record["payload"]["origin"] == "proposal"
    )
    return tree, tree.read_run(), region


def restated(tree, region, crop_bytes):
    """The same region record, naming a crop stored under different bytes.

    This is what a run tree sealed by another encoder looks like from here: the
    record names its own crop's digest, and the blob under that digest holds
    exactly the bytes that encoder wrote. The on-disk region artifact is left
    alone deliberately -- the act-identity binding reads *it* to check the
    Designator's proposal seal, and rewriting it would change what these tests
    are about.
    """
    digest, published = tree.put_blob(DESIGNATOR, crop_bytes)
    substituted = copy.deepcopy(region)
    substituted["payload"]["image_path"] = published.relative_path
    substituted["payload"]["image_sha256"] = digest
    return substituted


def sealed_crop(tree, region):
    """The exact crop bytes the run wrote for this region."""
    return tree.read_bytes(region["payload"]["image_path"])


def reframed(crop_bytes, **save):
    """The same picture, written by an encoder that made different choices."""
    with Image.open(BytesIO(crop_bytes)) as image:
        image.load()
        output = BytesIO()
        image.save(output, format="PNG", **save)
    return output.getvalue()


def test_the_crop_the_run_wrote_verifies(cropped):
    """The check passes on the real thing, so a failure below means what it says."""
    tree, run, region = cropped

    verified = verify_exemplar_crop_lineage(tree, run, region)

    assert verified["region_id"] == region["payload"]["region_id"]


def test_the_same_crop_written_by_another_encoder_is_not_forged_evidence(cropped):
    """The audit's red demonstration, as durable as the encoder it survives.

    Pillow's own PNG writer stands in for any other valid encoder: a different
    zlib build, a wheel with a different bundled one, the compressor this
    pipeline itself used before act crops were made deterministic. Every pixel
    is the crop of the sealed page; only the stream framing differs.
    """
    tree, run, region = cropped
    crop = sealed_crop(tree, region)
    other_encoding = reframed(crop, optimize=False, compress_level=1)
    assert other_encoding != crop

    verified = verify_exemplar_crop_lineage(tree, run, restated(tree, region, other_encoding))

    assert verified["verified_dimensions"] == {
        "w": region["payload"]["transform"]["bounds"]["w"],
        "h": region["payload"]["transform"]["bounds"]["h"],
    }


def test_the_same_crop_under_this_pipelines_previous_encoder_still_verifies(cropped):
    """Stated against the exact encoder a sealed tree on `main` was written with,
    rather than only against 'some other encoder': `encode_grayscale_png` is
    `zlib.compress(level=9)`, which is what `crop_png` emitted before this
    change. An existing run tree verifies under the new build."""
    tree, run, region = cropped
    crop = sealed_crop(tree, region)
    width, height, rows = decode_grayscale_png(crop)
    previous = encode_grayscale_png(width, height, rows)
    assert previous != crop

    verify_exemplar_crop_lineage(tree, run, restated(tree, region, previous))


def test_a_single_changed_pixel_is_still_refused_by_name(cropped):
    """The refusal that has to survive making the check tolerant of framing."""
    tree, run, region = cropped
    with Image.open(BytesIO(sealed_crop(tree, region))) as image:
        image.load()
        tampered = image.copy()
        tampered.putpixel((0, 0), 255 - image.getpixel((0, 0)))
        output = BytesIO()
        tampered.save(output, format="PNG")

    with pytest.raises(ContractError, match="pixels are not the exact crop"):
        verify_exemplar_crop_lineage(tree, run, restated(tree, region, output.getvalue()))


def test_a_crop_carrying_payload_beside_its_pixels_is_refused(cropped):
    """What the byte comparison used to refuse for free. Accepting two framings
    of one image says nothing about a chunk travelling beside the picture, so
    that is now said on its own."""
    tree, run, region = cropped
    crop = sealed_crop(tree, region)
    tag, data = b"tEXt", b"note\x00anything at all"
    smuggled = (
        crop[:-12]
        + struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data))
        + crop[-12:]
    )

    with pytest.raises(ContractError, match="carries content beyond the crop itself"):
        verify_exemplar_crop_lineage(tree, run, restated(tree, region, smuggled))


def test_a_crop_that_is_not_an_image_at_all_refuses_as_that(cropped):
    """Undecodable is its own fault and says so, rather than arriving as a claim
    about pixels nobody could read."""
    tree, run, region = cropped

    with pytest.raises(ContractError, match="not a decodable image"):
        verify_exemplar_crop_lineage(tree, run, restated(tree, region, b"not a png at all"))
