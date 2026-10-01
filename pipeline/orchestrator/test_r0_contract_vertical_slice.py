"""The page Testimonium contract and the corpus-frame knob, driven through the real stages.

Each forgery rewrites one Attestatores record and reseals every reference to it,
so the refusal asserted is the consumer's own check rather than an integrity
guard in front of it. Every stage runs as a subprocess over the synthetic fixture.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.stages import ATTESTATORES
from common.runtree.store import RunTree
from common.stage import (
    load_fixture,
    run_config_bindings,
)
from conftest import programs_through
from conftest import rebind_stage_seal_artifact as _rebind_stage_seal

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = ROOT / "proof"


def invoke_stage(run_root: Path, run_id: str, scenario: str, program: str):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / program),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


# --- Corpus-frame binding -------------------------------------------------------


def test_shard_size_knob_is_sealed_with_a_point_of_use_recheck_entry():
    """The shard-size knob is sealed in config_digest and rechecked at its point of use.

    `run_config_bindings(...)["sealed_config_digests"]` names it as
    `corpus-frame-shard`, bound to the digest of the sealed config bytes.
    """
    from common.stage import DEFAULT_CORPUS_FRAME_CONFIG_PATH, load_corpus_frame_policy

    fixture_data = load_fixture(str(FIXTURE_ROOT))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture_data, "happy")
    sealed = bindings["sealed_config_digests"]
    assert "corpus-frame-shard" in sealed, (
        f"run_config_bindings()'s sealed_config_digests is {sorted(sealed)}, which names no "
        "'corpus-frame-shard' entry; the shard-size knob must be sealed into config_digest "
        "with a point-of-use recheck"
    )
    _, expected_digest = load_corpus_frame_policy(DEFAULT_CORPUS_FRAME_CONFIG_PATH)
    assert sealed["corpus-frame-shard"] == expected_digest, (
        "the sealed corpus-frame-shard digest does not match the digest of the sealed "
        "config bytes; a renamed or mis-bound knob would pass a name-only check"
    )


# --- The page Testimonium contract --------------------------------------------


def _through_attestatores(root: Path, run_id: str, scenario: str = "happy") -> RunTree:
    for program in programs_through("attestatores"):
        result = invoke_stage(root, run_id, scenario, program)
        assert result.returncode == 0, f"{program}: {result.stderr}"
    return RunTree(root, run_id)


def _reseal(tree: RunTree, path: Path, record: dict) -> None:
    record["self_hash"] = self_hash(
        {key: value for key, value in record.items() if key != "self_hash"}
    )
    path.write_bytes(canonical_bytes(record))
    _rebind_stage_seal(tree, record["stage"])


def _page_testimonium_on(tree: RunTree, manifest: dict, page_ordinal: int) -> tuple[dict, dict]:
    for entry in manifest["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        if record["payload"].get("page_ordinal") == page_ordinal:
            return entry, record
    raise AssertionError(f"page {page_ordinal} has no page Testimonium")


def test_perlector_refuses_a_referenced_page_ordinal_outside_the_fixture(tmp_path):
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-page")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry = next(row for row in manifest["artifacts"] if row["kind"] == "page-testimonium")
    page = tree.read_artifact(ATTESTATORES, "page-testimonium", page_entry["artifact_id"])
    page["payload"]["page_ordinal"] = 99
    _reseal(tree, tree.resolve(page_entry["relative_path"]), page)

    result = invoke_stage(root, "forged-page", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    # The shared page contract reconciles `page_ordinal` against the
    # presentation, so it answers this forgery before the Perlector's own
    # subject-vs-presentation check does. The stage-local refusal also compares
    # `source_page_id` against the record's subject, which the shared contract
    # cannot see.
    assert "names a different page than the record" in result.stderr
    assert "Traceback" not in result.stderr


def test_perlector_refuses_a_page_presentation_that_disowns_its_record_subject(tmp_path):
    """A page Testimonium filed under one page may not present another page's ink.

    Every presentation is forged to the same other page id, ordinals untouched,
    so each presentation agrees with the first and the record's own page is the
    only thing left to disagree with.
    """
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "disowned-page")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry, page = _page_testimonium_on(tree, manifest, 2)
    forged_page_id = page["payload"]["presented"]["source_page_id"] + "-not-this-page"
    for shown in (page["payload"]["presented"], *page["payload"]["presentations"]):
        shown["source_page_id"] = forged_page_id
        shown["transform"]["source_page_id"] = forged_page_id
    _reseal(tree, tree.resolve(page_entry["relative_path"]), page)

    result = invoke_stage(root, "disowned-page", "happy", "pipeline/4_perlector/run.py")

    assert result.returncode != 0
    assert "wrong page Testimonium" in result.stderr
    assert "Traceback" not in result.stderr


def test_perlector_refuses_a_page_record_that_names_an_act_field_by_name(tmp_path):
    """A page record that claims an act role is not its closed schema, and says so."""
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "act-role")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry, page = _page_testimonium_on(tree, manifest, 2)
    page["payload"]["page_role"] = ["continuation"]
    _reseal(tree, tree.resolve(page_entry["relative_path"]), page)

    result = invoke_stage(root, "act-role", "happy", "pipeline/4_perlector/run.py")

    assert result.returncode != 0
    assert "not its closed schema" in result.stderr
    assert "Traceback" not in result.stderr
