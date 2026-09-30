"""The page render a reading is shown (`common/page_render.py`), and the
preference screen over a witness payload (`dossier.assert_no_order_bearing_field`).
"""

import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from common import page_render
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ATTESTATORES, DESIGNATOR, EXEMPLAR, PERLECTOR
from common.imaging import dimensions
from common.runtree.store import RunTree
from common.stage import StageContext
from conftest import load_stage, programs_through

ROOT = Path(__file__).resolve().parents[2]


dossier = load_stage("4_perlector", "dossier")
protocol = load_stage("4_perlector", "protocol")
# The run's sealed page-render bound, as a pass reads it.
PAGE_CONTEXT = protocol.load(ROOT / "config" / "perlector_protocol.toml")[0]["page_context"]
EDGE = PAGE_CONTEXT["maximum_edge"]


class _Context:
    stage = PERLECTOR
    sealed = False
    retain = StageContext.retain

    def __init__(self, tree, witness_context="named"):
        self.tree = tree
        self.run = tree.read_run()
        self.registry = ChairRegistry.from_toml(ROOT / "config/models.toml")
        self.witness_context = witness_context
        self.witness_context_config_path = ROOT / "config" / "witness_context.toml"

    @property
    def config_digest(self):
        return self.run["config_digest"]

    def input_ref(self, relative_path):
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    """A real run through the Attestatores, so the dossier is built over real
    regions and real testimonia rather than hand-built stand-ins."""
    root = tmp_path_factory.mktemp("dossier") / "runs"
    for program in programs_through("attestatores"):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "dossier-evidence",
                "--scenario",
                "happy",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{program}: {result.stderr}"

    tree = RunTree(root, "dossier-evidence")
    context = _Context(tree)
    first_region = next(
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region"
    )
    act_id = first_region["subject_id"]
    act_key = first_region["payload"]["act_key"]
    regions = [
        tree.read_artifact(DESIGNATOR, "region", entry["artifact_id"])["payload"]
        for entry in tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == "region" and entry["subject_id"] == act_id
    ]
    testimonia = [
        tree.read_artifact(ATTESTATORES, "testimonium", entry["artifact_id"])
        for entry in tree.build_manifest(ATTESTATORES)["artifacts"]
        if entry["kind"] == "testimonium" and entry["subject_id"] == act_id
    ]
    return context, act_id, act_key, regions, testimonia


def test_a_page_render_blob_is_reproducible_by_the_projects_own_encoder(evidence):
    """Pillow's bundled zlib differs per wheel, so a
    Pillow-saved render renames its content-addressed path on another platform.
    The blob must decode with the project's minimal filter-0 decoder and
    re-encode byte-identically through the deterministic encoder -- if either
    half fails, some library's compression choices have re-entered the file."""
    from common.imaging import decode_grayscale_png, encode_grayscale_png_deterministic

    context, act_id, act_key, regions, testimonia = evidence
    page_ids = {region["transform"]["source_page_id"] for region in regions}
    renders = [
        page_render.build_page_render(
            context,
            source_page_id=page_id,
            source_page_ordinal=next(
                region["transform"]["source_page_ordinal"]
                for region in regions
                if region["transform"]["source_page_id"] == page_id
            ),
            page_context=PAGE_CONTEXT,
            crop_bounds=[],
        )
        for page_id in sorted(page_ids)
    ]
    assert renders, "an act with no page renders would make this test vacuous"
    for render in renders:
        blob = context.tree.read_bytes(render["image_path"])
        width, height, rows = decode_grayscale_png(blob)
        assert encode_grayscale_png_deterministic(width, height, rows) == blob


def test_the_no_order_bearing_sweep_is_not_vacuous():
    """Prove the guard can go red: a payload carrying a trust/preference field
    must be caught."""
    payload = {"testimonia": [{"witness_label": "attestator_1", "reported": "alpha"}]}
    dossier.assert_no_order_bearing_field(payload)
    payload["testimonia"][0]["trust_score"] = 100
    with pytest.raises(ContractError, match="names a preference"):
        dossier.assert_no_order_bearing_field(payload)


# Far past any interpreter's recursion allowance, so a sweep that reaches the
# bottom of this proves it is not spending the interpreter stack, and one that
# refuses proves it refuses by name rather than by crashing.
PATHOLOGICAL_DEPTH = 1_000_000


def test_the_no_order_bearing_sweep_walks_a_pathological_dossier_instead_of_the_stack():
    """The last no-picker screen to stop recursing, and it had the family's defect.

    The family is enumerated in `common/test_preference_screen_walks.py`. The
    round that converted the others reported four screens and complete coverage;
    there were six, and this was the one still recursing -- missed because it
    lives in dossier assembly and is not *called* a preference screen. It does
    the same forbidden-vocabulary walk over the same class of witness-derived
    data -- a dossier carries every Testimonium verbatim -- and it runs on the
    production path, before the digest, on every dossier this build produces.
    Recursing over a deep one raised `RecursionError`: a crash naming nothing,
    from the guard that keeps a witness preference out of the dossier.
    """
    nested: object = {"leaf": 1}
    for _ in range(PATHOLOGICAL_DEPTH):
        nested = {"nested": [nested]}

    # Clean to the bottom: depth alone must not stop the sweep.
    dossier.assert_no_order_bearing_field(nested)
    del nested

    buried: object = {"trust_score": 100}
    for _ in range(PATHOLOGICAL_DEPTH):
        buried = {"nested": [buried]}
    with pytest.raises(ContractError, match="names a preference") as caught:
        dossier.assert_no_order_bearing_field(buried)
    message = str(caught.value)
    # The position is elided rather than rendered whole: a path of several
    # million characters has named nothing an operator can read.
    assert "more levels" in message
    assert len(message) < 1000


def test_a_dossier_that_contains_itself_is_named_rather_than_swept_forever():
    """The recursive sweep ended a cycle by exhausting itself. A sweep with no
    stack to exhaust must say so, or it hangs -- and a hang reports less than
    the traceback it replaced."""
    looped: dict = {"testimonia": []}
    looped["testimonia"].append(looped)

    with pytest.raises(ContractError, match="contains itself"):
        dossier.assert_no_order_bearing_field(looped)


def test_the_sweep_still_names_the_first_offender_a_recursive_walk_would_have_found():
    """Order is observable through the refusal message, so the rewrite has to
    keep it: a key is checked after the preceding sibling's whole subtree and
    before its own value's, exactly where the recursive form checked it."""
    with pytest.raises(ContractError, match=r"\$\.a\.rank names a preference"):
        dossier.assert_no_order_bearing_field({"a": {"rank": 1}, "zz_trust": 2})
    with pytest.raises(ContractError, match=r"\$\.a_trust names a preference"):
        dossier.assert_no_order_bearing_field({"a_trust": 1, "b": {"rank": 2}})


@pytest.mark.parametrize("field", ["consensus", "majority", "vote", "quorum"])
def test_voting_synonyms_are_refused_by_the_sweep(field):
    """Every durable voting synonym is refused by name."""
    payload = {"testimonia": [{"witness_label": "attestator_1", field: True}]}
    with pytest.raises(ContractError, match="names a preference"):
        dossier.assert_no_order_bearing_field(payload)


def test_build_page_render_records_its_whole_transform_not_only_a_factor(evidence):
    """ARCHITECTURE invariant 3 asks that the exact image shown be reproducible
    from the Exemplar plus the recorded transforms. A bare factor is only
    reproducible by someone who also has this module's code, so the record
    names the source size, the target size and the resampler."""
    context, act_id, act_key, regions, testimonia = evidence
    render = page_render.build_page_render(
        context,
        source_page_id=regions[0]["transform"]["source_page_id"],
        source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
        page_context=PAGE_CONTEXT,
        crop_bounds=[],
    )
    assert render["transform"] == {
        "operation": "downscale-for-page-context",
        "source_dimensions": {"w": 200, "h": 260},
        # The synthetic fixture page is far inside the bound, so the honest
        # record is that nothing was resampled -- not a decorative resize
        # reported as a downscale.
        "target_dimensions": {"w": 200, "h": 260},
        "maximum_edge": EDGE,
        "resampler": "identity",
    }
    assert render["source"]["sha256"]
    assert context.tree.read_bytes(render["image_path"])


def test_a_page_the_acts_crops_cover_whole_is_rendered_as_layout_only(evidence):
    """Its crops already show every pixel at full resolution, so the page render is
    layout-sized and says why; a page the crops do not cover stays legible."""
    context, act_id, act_key, regions, testimonia = evidence
    page = {
        "source_page_id": regions[0]["transform"]["source_page_id"],
        "source_page_ordinal": regions[0]["transform"]["source_page_ordinal"],
    }
    halves = [{"x": 0, "y": 0, "w": 200, "h": 130}, {"x": 0, "y": 130, "w": 200, "h": 130}]
    covered = page_render.build_page_render(
        context, **page, page_context=PAGE_CONTEXT, crop_bounds=halves
    )
    partial = page_render.build_page_render(
        context, **page, page_context=PAGE_CONTEXT, crop_bounds=halves[:1]
    )
    assert covered["reason"] == page_render.COVERED_BY_CROP
    assert covered["transform"]["maximum_edge"] == PAGE_CONTEXT["covered_page_edge"]
    assert partial["reason"] == page_render.LEGIBLE_INK
    assert partial["transform"]["maximum_edge"] == EDGE
    spanning = page_render.build_page_render(
        context, **page, page_context=PAGE_CONTEXT, crop_bounds=halves[:1], multi_page=True
    )
    assert spanning["reason"] == page_render.MULTI_PAGE_ACT
    assert spanning["transform"]["maximum_edge"] == PAGE_CONTEXT["covered_page_edge"]


def test_a_page_past_the_bound_is_actually_downscaled_to_it():
    """The branch the 200x260 fixture page never reaches. Proved on real bytes
    rather than asserted, because a page-context render that quietly returned
    the full-resolution page would satisfy every other test in this file."""
    big = BytesIO()
    Image.new("L", (4000, 3000), color=200).save(big, format="PNG")
    rendered, transform = page_render._downscale_page(big.getvalue(), maximum_edge=EDGE)
    assert transform["source_dimensions"] == {"w": 4000, "h": 3000}
    assert transform["target_dimensions"] == {"w": EDGE, "h": EDGE * 3 // 4}
    assert transform["resampler"] == "pillow-lanczos"
    assert dimensions(rendered) == (EDGE, EDGE * 3 // 4)


def test_build_page_render_is_reused_byte_identically_on_a_repeat_call(evidence):
    context, act_id, act_key, regions, testimonia = evidence
    first = page_render.build_page_render(
        context,
        source_page_id=regions[0]["transform"]["source_page_id"],
        source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
        page_context=PAGE_CONTEXT,
        crop_bounds=[],
    )
    second = page_render.build_page_render(
        context,
        source_page_id=regions[0]["transform"]["source_page_id"],
        source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
        page_context=PAGE_CONTEXT,
        crop_bounds=[],
    )
    assert first == second


def test_a_page_render_refuses_page_bytes_swapped_after_the_artifact_check(evidence, monkeypatch):
    """The Perlector's page view is rendered only from bytes checked against the seal."""
    context, act_id, act_key, regions, testimonia = evidence
    page_id = regions[0]["transform"]["source_page_id"]
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    image_path = page["payload"]["image_path"]
    other = BytesIO()
    Image.new("L", (200, 260), color=7).save(other, format="PNG")
    read_bytes, read_artifact = context.tree.read_bytes, context.tree.read_artifact

    def verified_before_swap(*args):
        record = read_artifact(*args)
        monkeypatch.setattr(
            context.tree,
            "read_bytes",
            lambda path: other.getvalue() if path == image_path else read_bytes(path),
        )
        return record

    monkeypatch.setattr(context.tree, "read_artifact", verified_before_swap)
    with pytest.raises(SchemaRefusal, match="changed under a sealed reference"):
        page_render.build_page_render(
            context,
            source_page_id=page_id,
            source_page_ordinal=regions[0]["transform"]["source_page_ordinal"],
            page_context=PAGE_CONTEXT,
            crop_bounds=[],
        )
