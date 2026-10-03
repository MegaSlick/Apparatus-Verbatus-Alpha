"""The final page census keeps the source page/frame locator from the Exemplar.

This assembles only synthetic PDF bytes at runtime. It calls the Armarium's
pre-export census directly after the real door and Exemplar, so only the census
handoff is under test.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.approval import synthetic_fixture_ingress_record
from common.contracts.canonical import digest_bytes
from common.contracts.errors import FatalAccounting
from common.contracts.stages import ARMARIUM, DESIGNATOR, DOOR
from common.runtree.store import RunTree
from common.stage import StageContext, adapter_recipe_for, load_fixture, run_config_bindings
from conftest import load_stage

ROOT = Path(__file__).resolve().parents[2]
EXEMPLAR_CLI = ROOT / "pipeline" / "1_exemplar" / "run.py"


def test_final_page_census_keeps_a_multipage_pdf_filename_digest_and_page_index(tmp_path):
    import door
    from synthetic_sources import two_page_pdf

    PDF_SETTINGS = door.render_config.load_pdf_render_settings(
        minimum_dpi=door.pdf_render.MIN_RENDER_DPI
    )

    data = two_page_pdf()
    files = {"iPhone/FS-88.pdf": data}
    sources = door.expand_sources(
        [
            {"relative_path": path, "sha256": digest_bytes(data), "bytes": len(data)}
            for path in files
        ],
        lambda path: files[path],
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
        sealed_config_digests=bindings["sealed_config_digests"],
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
        door.process_sources(
            door_context,
            tree,
            sources,
            lambda path: files[path],
            pdf_settings=PDF_SETTINGS,
        )
        == 2
    )
    door_context.seal_boundary()
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
    armarium = load_stage("7_armarium")
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
    with pytest.raises(FatalAccounting, match="changed under a sealed reference"):
        armarium.export_source_regions(tree, linked, census)
