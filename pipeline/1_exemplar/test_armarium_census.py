"""The final page census keeps the source page/frame locator from the Exemplar.

This assembles only synthetic PDF bytes at runtime. It exercises the real door and
Exemplar handoff, then calls the Armarium's pre-export census directly: System 03
does not claim a real Designator model, so fabricating all later act artifacts just
to reach an export would prove the wrong thing.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.approval import synthetic_fixture_ingress_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.stages import ARMARIUM, DESIGNATOR, DOOR
from common.runtree.store import RunTree
from common.stage import StageContext, adapter_recipe_for, load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"


def _armarium_module():
    spec = importlib.util.spec_from_file_location(
        "armarium_system03_test", ROOT / "pipeline" / "7_armarium" / "run.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_page_attribution_requires_verified_designator_crop_lineage(monkeypatch):
    """A raw region ordinal cannot itself prove a sealed page was marked out."""
    armarium = _armarium_module()

    class Tree:
        def build_manifest(self, stage):
            assert stage == DESIGNATOR
            return {"artifacts": [{"kind": "region", "artifact_id": "forged"}]}

        def read_artifact(self, stage, kind, artifact_id):
            assert (stage, kind, artifact_id) == (DESIGNATOR, "region", "forged")
            return {
                "subject_id": "act-1",
                "payload": {"transform": {"source_page_ordinal": 999}},
            }

    context = SimpleNamespace(tree=Tree(), run={})
    monkeypatch.setattr(armarium, "expected_acts", lambda _context: [{"act_id": "act-1"}])

    def forged_lineage(*_args):
        raise ContractError("the forged transform is not an Exemplar crop")

    monkeypatch.setattr(armarium, "verify_exemplar_crop_lineage", forged_lineage)
    with pytest.raises(FatalAccounting, match="cannot be verified"):
        armarium.pages_marked_out(context, {})


def test_an_outstanding_recovery_is_not_called_an_accepted_act_without_an_archetypus(monkeypatch):
    """The terminal message must name outstanding recovery, not a false acceptance."""
    armarium = _armarium_module()
    review = {"outcome": "recovery-requested", "artifact_id": "review-1"}
    monkeypatch.setattr(
        armarium,
        "artifacts_for",
        lambda _context, stage, _kind, _act_id, _cache: [review] if stage == "recensor" else [],
    )
    monkeypatch.setattr(armarium, "latest_attempt", lambda records, *_args, **_kwargs: records[0])

    with pytest.raises(FatalAccounting, match="outstanding recovery request"):
        armarium.categorize(SimpleNamespace(), "act-1", {})


def test_final_page_census_keeps_a_multipage_pdf_filename_digest_and_page_index(tmp_path):
    import door
    from admission import load_format_policy
    from synthetic_sources import two_page_pdf

    data = two_page_pdf()
    files = {"iPhone/FS-88.pdf": data}
    policy = load_format_policy()
    sources = door.expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path in files
        ],
        lambda path: files[path],
        policy,
    )
    assert [source.container_page_index for source in sources] == [0, 1]

    fixture = load_fixture(str(ROOT / "proof"))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture, "happy")
    tree = RunTree.create(
        tmp_path / "runs",
        "pdf-census",
        source_manifest=[
            {
                "relative_path": source.declared_path,
                "sha256": source.declared_sha256,
                "ordinal": source.ordinal,
                "bytes": source.declared_size,
                "container_page_index": source.container_page_index,
            }
            for source in sources
        ],
        config_digest=bindings["config_digest"],
        adapter_recipes=bindings["adapter_recipes"],
        witness_chairs=bindings["witness_chairs"],
        ingress=synthetic_fixture_ingress_record(),
        # Read from the shipped config rather than restated: a sealed run binds its
        # render recipe, so a literal here goes stale the moment the default moves
        # and fails as "the render contract changes the sealed pixel recipe" — which
        # is the guard doing its job, several files away from the actual edit.
        #
        # Through `door` rather than by importing `render_config` and `pdf_render`
        # directly: those two are door-private (`test_import_boundaries.py`), this
        # file is not on their allow-list, and widening that list to spare one test
        # an attribute lookup would trade a real boundary for a cosmetic one.
        render_settings={
            "pdf": door.render_config.load_pdf_render_settings(
                minimum_dpi=door.pdf_render.MIN_RENDER_DPI
            ).to_record()
        },
    )
    door_context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture=fixture,
        scenario="happy",
        stage=DOOR,
        adapter_revision=adapter_recipe_for(tree.read_run(), DOOR),
        args=None,
        registry=registry,
    )
    assert (
        door.process_sources(door_context, tree, sources, lambda path: files[path], policy=policy)
        == 2
    )
    door_context.finish(DOOR)

    sealed = subprocess.run(
        [
            sys.executable,
            str(EXEMPLAR_CLI),
            "--run-root",
            str(tmp_path / "runs"),
            "--run-id",
            "pdf-census",
            "--fixture-root",
            str(ROOT / "proof"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert sealed.returncode == 0, sealed.stderr

    armarium_context = StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture=fixture,
        scenario="happy",
        stage=ARMARIUM,
        adapter_revision=adapter_recipe_for(tree.read_run(), ARMARIUM),
        args=None,
        registry=None,
    )
    armarium = _armarium_module()
    census = armarium.page_census(armarium_context)
    assert [census[ordinal]["container_page_index"] for ordinal in sorted(census)] == [0, 1]
    assert {row["declared_path"] for row in census.values()} == {"iPhone/FS-88.pdf"}
    assert {row["declared_sha256"] for row in census.values()} == {digest_bytes(data)}

    first_digest, first_blob = tree.put_blob(DESIGNATOR, b"first synthetic crop")
    second_digest, second_blob = tree.put_blob(DESIGNATOR, b"second synthetic crop")
    linked = armarium.export_source_regions(
        tree,
        [
            {
                "region_id": "rgn_first",
                "image_path": first_blob.relative_path,
                "image_sha256": first_digest,
                "source_page_ordinal": 1,
                "source_page_id": census[1]["page_id"],
                "transform": {
                    "operation": "crop",
                    "source_page_ordinal": 1,
                    "source_page_id": census[1]["page_id"],
                    "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
                },
            },
            {
                "region_id": "rgn_second",
                "image_path": second_blob.relative_path,
                "image_sha256": second_digest,
                "source_page_ordinal": 2,
                "source_page_id": census[2]["page_id"],
                "transform": {
                    "operation": "crop",
                    "source_page_ordinal": 2,
                    "source_page_id": census[2]["page_id"],
                    "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
                },
            },
        ],
        census,
    )
    assert [row["container_page_index"] for row in linked] == [0, 1]
    assert {row["declared_path"] for row in linked} == {"iPhone/FS-88.pdf"}
    assert {row["declared_sha256"] for row in linked} == {digest_bytes(data)}

    tree.resolve(first_blob.relative_path).write_bytes(b"changed crop pixels")
    with pytest.raises(FatalAccounting, match="crop bytes changed before export"):
        armarium.export_source_regions(tree, linked, census)
