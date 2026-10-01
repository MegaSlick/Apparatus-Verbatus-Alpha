"""The page Testimonium contract and the corpus-frame knob, driven through the real stages.

Each forgery rewrites one Attestatores record and reseals every reference to it,
so the refusal asserted is the consumer's own check rather than an integrity
guard in front of it. Every stage runs as a subprocess over the synthetic fixture.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.stages import ATTESTATORES
from common.fixture_identity import act_identity
from common.runtree.store import RunTree
from common.stage import (
    act_by_key,
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


@pytest.fixture(scope="module")
def fixture():
    return load_fixture(str(FIXTURE_ROOT))


def _attestatores_artifacts(tree: RunTree) -> list[dict]:
    manifest = tree.build_manifest(ATTESTATORES)
    return [
        tree.read_artifact(ATTESTATORES, entry["kind"], entry["artifact_id"])
        for entry in manifest["artifacts"]
    ]


# --- Corpus-frame binding -------------------------------------------------------


def test_shard_size_knob_is_sealed_with_a_point_of_use_recheck_entry():
    """R0_CONTRACT_NOTE.md: "shard size <=1,000 is R0's own sealed knob in
    config_digest with point-of-use recheck."

    Mirrors the existing `designator-padding` entry in
    `run_config_bindings(...)["sealed_config_digests"]`
    (`common/stage.py::StageContext.require_sealed_config` is the point-of-use
    recheck mechanism already built for that entry). On the base commit
    `sealed_config_digests` carries exactly one key, `designator-padding`; nothing
    names a shard-size knob at all.
    """
    from common.stage import DEFAULT_CORPUS_FRAME_CONFIG_PATH, load_corpus_frame_policy

    fixture_data = load_fixture(str(FIXTURE_ROOT))
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    bindings = run_config_bindings(registry.config, fixture_data, "happy")
    sealed = bindings["sealed_config_digests"]
    assert "corpus-frame-shard" in sealed, (
        f"run_config_bindings()'s sealed_config_digests is {sorted(sealed)}, which names no "
        "'corpus-frame-shard' entry; R0's shard-size knob must be sealed into config_digest "
        "with a point-of-use recheck, exactly as 'designator-padding' already is"
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


def _reseal_page_and_references(
    tree: RunTree, manifest: dict, page_entry: dict, page: dict
) -> None:
    """Reseal every consuming reference so a semantic page forgery reaches its validator."""
    page_path = tree.resolve(page_entry["relative_path"])
    _reseal(tree, page_path, page)
    page_digest = digest_bytes(page_path.read_bytes())
    for attachment_entry in manifest["artifacts"]:
        if attachment_entry["kind"] != "act-attachment":
            continue
        attachment_path = tree.resolve(attachment_entry["relative_path"])
        attachment = tree.read_artifact(
            ATTESTATORES, "act-attachment", attachment_entry["artifact_id"]
        )
        changed = False
        for row in attachment["payload"]["attachments"]:
            reference = row["testimonium_ref"]
            if reference["relative_path"] == page_entry["relative_path"]:
                reference["sha256"] = page_digest
                changed = True
        if changed:
            _reseal(tree, attachment_path, attachment)


def test_perlector_refuses_a_referenced_page_ordinal_outside_the_fixture(tmp_path):
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "forged-page")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry = next(row for row in manifest["artifacts"] if row["kind"] == "page-testimonium")
    page = tree.read_artifact(ATTESTATORES, "page-testimonium", page_entry["artifact_id"])
    page["payload"]["page_ordinal"] = 99
    _reseal_page_and_references(tree, manifest, page_entry, page)

    result = invoke_stage(root, "forged-page", "happy", "pipeline/4_perlector/run.py")
    assert result.returncode != 0
    # The shared page contract now reconciles `page_ordinal` against the
    # presentation, so it answers this forgery before the Perlector's own
    # subject-vs-presentation check does. The stage-local refusal is still
    # there and still needed -- it also compares `source_page_id` against the
    # record's subject, which the shared contract cannot see.
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
    _reseal_page_and_references(tree, manifest, page_entry, page)

    result = invoke_stage(root, "disowned-page", "happy", "pipeline/4_perlector/run.py")

    assert result.returncode != 0
    assert "wrong page Testimonium" in result.stderr
    assert "Traceback" not in result.stderr


def test_perlector_names_an_unhashable_page_role_as_a_schema_refusal(tmp_path):
    """A JSON-shaped role must not escape through set membership as TypeError."""
    root = tmp_path / "runs"
    tree = _through_attestatores(root, "unhashable-role")
    manifest = tree.build_manifest(ATTESTATORES)
    page_entry, page = _page_testimonium_on(tree, manifest, 2)
    page["payload"]["page_role"] = ["continuation"]
    _reseal_page_and_references(tree, manifest, page_entry, page)

    result = invoke_stage(root, "unhashable-role", "happy", "pipeline/4_perlector/run.py")

    assert result.returncode != 0
    # The shared page-testimonium validator now closes the record's scope facts
    # before the Perlector's per-page contradiction check can run, so the forged
    # list-valued role is refused there by name — earlier, and still never as a
    # raw TypeError escaping through set membership.
    assert "invalid page scope facts" in result.stderr
    assert "Traceback" not in result.stderr


def test_page_testimony_names_a_reading_the_join_could_not_carry(tmp_path, fixture):
    """F-O7: the page record's closed omission list is the join's exact complement.

    The join drops an act two ways -- a non-reading outcome (F-S1) and a reading
    the join cannot concatenate, because the chair delivered a structured native
    object rather than text. `unjoined_act_attempts` named only the first, so the
    second went behind a successful status: in the shipped `structured-witness`
    scenario, attestator_1's page-1 record reported `read`, carried act a2's text
    alone, and disclosed an empty omission list while act a1 was simply gone.
    Measured on the real run tree before the fix.
    """
    root = tmp_path / "runs"
    _through_attestatores(root, "structured", "structured-witness")
    tree = RunTree(root, "structured")
    act_a1_id = act_identity(fixture, act_by_key(fixture, "a1"))

    act_record = next(
        record
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "testimonium"
        and record["subject_id"] == act_a1_id
        and record.get("payload", {}).get("chair") == "attestator_1"
    )
    assert act_record["outcome"] == "read", (
        "fixture precondition: attestator_1's act-a1 attempt must be a completed reading "
        f"for this test to exercise the silent-omission path; got {act_record['outcome']!r}"
    )
    assert not isinstance(act_record["payload"]["payload"], str), (
        "fixture precondition: that reading must be a structured native object"
    )

    page_payload = next(
        record["payload"]
        for record in _attestatores_artifacts(tree)
        if record.get("kind") == "page-testimonium"
        and record.get("payload", {}).get("chair") == "attestator_1"
        and record.get("payload", {}).get("page_ordinal") == 1
    )
    unjoined = page_payload["unjoined_act_attempts"]
    named = {row["act_id"]: row for row in unjoined}
    assert act_a1_id in named, (
        f"attestator_1's page-1 record joined only {page_payload['payload']!r} and named "
        f"{unjoined!r} as omitted; act a1's structured reading is absent from both, so the "
        "record reports a page it did not fully cover and says nothing about the gap"
    )
    row = named[act_a1_id]
    assert row["outcome"] == "read", (
        "the omission must be disclosed with the attempt outcome that actually happened, "
        f"not relabelled as a failure; got {row['outcome']!r}"
    )
    assert isinstance(row["reason"], str) and row["reason"].strip()
