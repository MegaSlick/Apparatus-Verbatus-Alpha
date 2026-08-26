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
from common.exemplar_boundary import (
    _validate_exemplar_transform,
    verify_exemplar_crop_lineage,
    verify_sealed_page_pixels,
)
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


# --- Transform values are closed as well as their record shapes ----------------


def _valid_split_transform():
    return {"operation": "split", "region": {"space": "frame", "x": 0, "y": 0, "w": 5, "h": 4}}


def _valid_part_local_crop_transform():
    return {"operation": "crop", "bounds": {"space": "part", "x": 0, "y": 0, "w": 5, "h": 4}}


def _valid_deskew_transform():
    return {
        "operation": "deskew",
        "rotation": {
            "rotation_millidegrees": 0,
            "direction": "clockwise",
            "origin": "crop-centre",
            "canvas": "expand",
        },
    }


def test_well_formed_split_part_local_crop_and_deskew_transforms_validate():
    _validate_exemplar_transform(_valid_split_transform())
    _validate_exemplar_transform(_valid_part_local_crop_transform())
    _validate_exemplar_transform(_valid_deskew_transform())


@pytest.mark.parametrize(
    "field,value", [("w", 0), ("w", -1), ("h", 0), ("h", -1), ("x", -1), ("y", -1), ("w", 1.5)]
)
def test_a_split_region_with_a_non_positive_or_non_integer_bound_is_refused(field, value):
    """The shape check alone would accept this; only a re-render would ever have
    caught it downstream. Closed means the values are checked here too."""
    transform = _valid_split_transform()
    transform["region"][field] = value
    with pytest.raises(ContractError, match="split transform"):
        _validate_exemplar_transform(transform)


@pytest.mark.parametrize(
    "field,value", [("w", 0), ("w", -1), ("h", 0), ("h", -1), ("x", -1), ("y", -1), ("w", 1.5)]
)
def test_a_part_local_crop_with_a_non_positive_or_non_integer_bound_is_refused(field, value):
    transform = _valid_part_local_crop_transform()
    transform["bounds"][field] = value
    with pytest.raises(ContractError, match="derivative crop transform"):
        _validate_exemplar_transform(transform)


@pytest.mark.parametrize(
    "field,value",
    [
        ("rotation_millidegrees", 180_001),
        ("rotation_millidegrees", -180_001),
        ("rotation_millidegrees", 1.5),
        ("direction", "counterclockwise"),
        ("origin", "top-left"),
        ("canvas", "crop"),
    ],
)
def test_a_deskew_rotation_outside_its_closed_recipe_is_refused(field, value):
    transform = _valid_deskew_transform()
    transform["rotation"][field] = value
    with pytest.raises(ContractError, match="deskew transform"):
        _validate_exemplar_transform(transform)


# --- The master a sealed derivative page claims to account for ----------------
# A row can partition its declared frame while under-declaring the decoded master;
# re-derivation alone proves the output pixels but cannot detect the omitted area.


def _rows_digest(row):
    from common.contracts.canonical import canonical_bytes, digest_bytes

    return digest_bytes(
        canonical_bytes({key: value for key, value in row.items() if key != "manifest_row_sha256"})
    )


def _sealed_derivative(master_size, declared_frame):
    """A master, a row declaring `declared_frame`, and the page it re-derives to."""
    from common.contracts.canonical import digest_bytes
    from common.imaging import imaging_library_versions, render_triage_derivative

    image = Image.new("RGB", master_size, (200, 30, 30))
    output = BytesIO()
    image.save(output, format="PNG")
    master = output.getvalue()
    part = {
        "region": {
            "space": "frame",
            "x": 0,
            "y": 0,
            "w": declared_frame["width"],
            "h": declared_frame["height"],
        },
        "crop_box": {
            "space": "part",
            "x": 0,
            "y": 0,
            "w": declared_frame["width"],
            "h": declared_frame["height"],
        },
        "rotation": {
            "rotation_millidegrees": 0,
            "direction": "clockwise",
            "origin": "crop-centre",
            "canvas": "expand",
        },
        "colour_mode": "rgb",
    }
    row = {
        "corpus_id": "parish-a",
        "source_frame_sha256": digest_bytes(master),
        "frame": declared_frame,
        "split": {"operation_order": "region-crop-rotate", "parts": [part]},
        "re_shoot_cluster_id": None,
        "confidence": 4,
        "mode": "auto",
        "actor": {"kind": "model", "identity": "triage", "revision": "r1"},
        "human_override": False,
    }
    row["manifest_row_sha256"] = _rows_digest(row)
    sealed, geometry = render_triage_derivative(master, page_index=0, part=part)
    mode_transform = (
        "triage-region-crop-rotate-convert"
        if geometry["source_mode"] == geometry["color_mode"]
        else f"triage-region-crop-rotate-convert-to-{geometry['color_mode'].lower()}"
    )
    contract = {
        **imaging_library_versions(),
        "source_mode": geometry["source_mode"],
        "source_bands": geometry["source_bands"],
        "mode_transform": mode_transform,
        "output": {"codec": "png", "color_mode": geometry["color_mode"]},
        "container_page_index": 0,
        "width": geometry["width"],
        "height": geometry["height"],
        "deterministic_encoder": "common.imaging.encode_image_deterministic-v1",
        "derivative_page": {
            "kind": "sealed-derivative-page-v1",
            "parent_frame_sha256": row["source_frame_sha256"],
            "parent_frame_page_index": 0,
            "triage_manifest_row": row,
            "triage_backlink": {
                "corpus_id": "parish-a",
                "source_frame_sha256": row["source_frame_sha256"],
                "triage_manifest_row_sha256": row["manifest_row_sha256"],
                "triage_part_index": 0,
            },
            "operation_order": "region-crop-rotate",
            "apply_recipe": {
                "schema": "triage-raster-apply-v1",
                "rotation_resample": "Pillow.Resampling.BICUBIC",
                "rotation_fill": "Pillow-default-zero",
                "rotation_expand": True,
                "colour_conversion": "Pillow.Image.convert-direct-or-via-RGB",
                "encoder": "common.imaging.encode_image_deterministic-v1",
            },
            "operations": [
                {"operation": "split", "region": part["region"]},
                {"operation": "crop", "bounds": part["crop_box"]},
                {"operation": "deskew", "rotation": part["rotation"]},
                {"operation": "convert", "colour_mode": part["colour_mode"]},
            ],
        },
    }
    parent = {
        "sha256": row["source_frame_sha256"],
        "stored_at": "1-exemplar/blobs/x",
        "source_frame_index": 0,
    }
    return contract, master, parent, sealed


@pytest.mark.parametrize(
    ("field", "forged"),
    [("container_sha256", "f" * 64), ("container_page_index", 1)],
)
def test_a_derivative_container_origin_must_bind_its_submitted_source(field, forged):
    """The nested master link cannot excuse a false outer container link."""
    from common.contracts.canonical import digest_bytes
    from common.exemplar_boundary import _verify_rendered_source_link

    contract, master, _parent, _sealed = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    rendered = {
        "container_format": "triage-split-raster",
        "container_sha256": digest_bytes(master),
        "container_page_index": 0,
        "render_contract": contract,
    }
    source = {"sha256": digest_bytes(master), "container_page_index": 0}
    forged_rendered = copy.deepcopy(rendered)
    forged_rendered[field] = forged

    with pytest.raises(ContractError, match="does not bind its submitted source"):
        _verify_rendered_source_link(forged_rendered, forged_rendered, source)


def test_a_page_cannot_change_the_render_origin_its_door_admission_sealed():
    from common.contracts.canonical import digest_bytes
    from common.exemplar_boundary import _verify_rendered_source_link

    contract, master, _parent, _sealed = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    digest = digest_bytes(master)
    admission_rendered = {
        "container_format": "triage-split-raster",
        "container_sha256": digest,
        "container_page_index": 0,
        "render_contract": contract,
    }
    page_rendered = copy.deepcopy(admission_rendered)
    page_rendered["container_format"] = "some-other-origin"

    with pytest.raises(ContractError, match="changed its Door admission's render origin"):
        _verify_rendered_source_link(
            page_rendered,
            admission_rendered,
            {"sha256": digest, "container_page_index": 0},
        )


def test_a_derivative_page_whose_row_under_declares_its_master_is_refused():
    """The right half of this master reaches no page, and every other check passes.

    One part, covering the declared 4x4 frame exactly, cut from an 8x4 master.
    The row validates, the backlink names the master, and the recorded transform
    re-derives the sealed bytes exactly. Only the frame comparison notices that
    half the photograph was never accounted for.
    """
    from common.exemplar_boundary import _verify_triage_derivative

    honest = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    _verify_triage_derivative(honest[0], honest[1], honest[2], honest[3])

    contract, master, parent, sealed = _sealed_derivative((8, 4), {"width": 4, "height": 4})
    with pytest.raises(ContractError, match="declares a frame that is not the size of the master"):
        _verify_triage_derivative(contract, master, parent, sealed)


def test_a_re_derivation_mismatch_names_a_decoder_upgrade_when_one_explains_it():
    """The apply recipe's library versions are a record, not an enforcement.

    Refusing on version drift would make every archived run unverifiable on the
    next routine Pillow upgrade, so the byte comparison stays the property and
    the versions stay provenance (GOVERNANCE 6). But an operator reading "not
    reproducible" alone would go looking for forgery, and a decoder upgrade is
    the ordinary cause — so when the recorded versions differ from this host's,
    the refusal says which ones.
    """
    from common.exemplar_boundary import _verify_triage_derivative

    contract, master, parent, sealed = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    contract["renderer_version"] = "0.0.0-not-this-host"

    with pytest.raises(ContractError, match="not reproducible") as drifted:
        _verify_triage_derivative(contract, master, parent, sealed + b"x")
    assert "renderer_version '0.0.0-not-this-host'" in str(drifted.value)
    assert "recorded, not enforced" in str(drifted.value)

    contract, master, parent, sealed = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    with pytest.raises(ContractError, match="not reproducible") as undrifted:
        _verify_triage_derivative(contract, master, parent, sealed + b"x")
    assert "sealed under different imaging libraries" not in str(undrifted.value)


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("source_mode", "L"),
        ("source_bands", ["L"]),
        ("mode_transform", "identity"),
        ("output", {"codec": "png", "color_mode": "L"}),
        ("container_page_index", 1),
        ("width", 3),
        ("height", 3),
        ("deterministic_encoder", "some-other-encoder"),
    ],
)
def test_a_derivative_renderer_record_cannot_lie_about_rederived_pixels(field, forged):
    from common.exemplar_boundary import _verify_triage_derivative

    contract, master, parent, sealed = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    contract[field] = forged

    with pytest.raises(ContractError, match="renderer record does not describe"):
        _verify_triage_derivative(contract, master, parent, sealed)


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(lambda row: row.pop("actor"), id="missing-actor"),
        pytest.param(lambda row: row.__setitem__("mode", "undeclared"), id="unknown-mode"),
        pytest.param(
            lambda row: row.__setitem__(
                "actor", {"kind": "model", "identity": "triage", "revision": ""}
            ),
            id="unresolved-actor",
        ),
    ],
)
def test_a_self_hashed_manifest_row_still_needs_complete_mode_and_actor_provenance(forge):
    from common.exemplar_boundary import _verify_triage_derivative

    contract, master, parent, sealed = _sealed_derivative((4, 4), {"width": 4, "height": 4})
    row = contract["derivative_page"]["triage_manifest_row"]
    forge(row)
    row["manifest_row_sha256"] = _rows_digest(row)
    contract["derivative_page"]["triage_backlink"]["triage_manifest_row_sha256"] = row[
        "manifest_row_sha256"
    ]

    with pytest.raises(ContractError, match="triage row|triage manifest row"):
        _verify_triage_derivative(contract, master, parent, sealed)
