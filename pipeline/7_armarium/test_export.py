"""End-to-end Armarium product exports over the real sealed fixture pipeline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from armarium_export import verify_delivered_bundle, verify_export_bundle

from common import armarium_formats
from common.contracts.canonical import canonical_bytes, self_hash
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ARMARIUM
from common.runtree import store as runtree_store
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_FATAL, run_stage
from conftest import load_stage
from conftest import rebind_stage_seal_artifact as _rebind_stage_seal

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
ARMARIUM_CLI = ROOT / "pipeline" / "7_armarium" / "run.py"


def _orchestrate(
    run_root: Path,
    run_id: str,
    *,
    formats_config: Path | None = None,
    scenario: str = "page-unbroken",
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        scenario,
        "--run-id",
        run_id,
        "--run-root",
        str(run_root),
    ]
    if formats_config is not None:
        command.extend(("--formats-config", str(formats_config)))
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _run_armarium(
    run_root: Path, run_id: str, scenario: str = "page-unbroken"
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ARMARIUM_CLI),
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


def _export(tree: RunTree) -> dict:
    return tree.read_artifact(
        ARMARIUM,
        "export",
        artifact_id(ARMARIUM, "export", "export", None),
    )


@pytest.fixture(scope="module")
def embedded_run(tmp_path_factory):
    """A fixture run whose sealed format selection embeds page and crop bytes."""
    root = tmp_path_factory.mktemp("embedded")
    formats = root / "formats.toml"
    formats.write_text(
        'schema = "armarium-formats.v1"\n'
        'formats = ["text-bundle", "acts-database", "jsonl", "review-items"]\n'
        "embed_pixels = true\n",
        encoding="utf-8",
    )
    result = _orchestrate(root / "runs", "embedded", formats_config=formats)
    assert result.returncode == 0, result.stderr
    return root / "runs"


def test_run_bound_pixel_embedding_packages_page_and_crop_bytes(tmp_path, embedded_run):
    tree = RunTree(embedded_run, "embedded")
    reference = _export(tree)["payload"]["bundle"]["reference"]
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), tmp_path / "clean")
    assert manifest["formats"]["embed_pixels"] is True
    assert manifest["claims"]["pixels"]["resolution_claim"].startswith("embedded pixels")


def _rerun_armarium_in_process(monkeypatch, run_root: Path, formats: Path) -> int:
    """Run the Armarium again in this process, so a lowered read ceiling reaches it."""
    armarium = load_stage("7_armarium")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            *("--run-root", str(run_root), "--run-id", "embedded"),
            *("--scenario", "page-unbroken", "--formats-config", str(formats)),
        ],
    )
    return run_stage(armarium.main)


def _archive_and_page_ceilings(monkeypatch, tree: RunTree) -> int:
    """Lower the page ceiling to one byte below the archive, which is still above every
    other file in the tree, and return the archive's size."""
    archive = tree.resolve(_export(tree)["payload"]["bundle"]["reference"]["relative_path"])
    size = archive.stat().st_size
    others = [path for path in tree.root.rglob("*") if path.is_file() and path != archive]
    assert max(path.stat().st_size for path in others) < size - 1
    monkeypatch.setattr(runtree_store, "_MAX_TREE_READ_BYTES", size - 1)
    return size


def test_an_archive_above_the_page_ceiling_but_within_its_own_seals_and_publishes(
    tmp_path, embedded_run, monkeypatch
):
    import bundle as bundle_module

    root = tmp_path / "runs"
    shutil.copytree(embedded_run, root, symlinks=True)
    tree = RunTree(root, "embedded")
    size = _archive_and_page_ceilings(monkeypatch, tree)
    monkeypatch.setattr(armarium_formats, "MAX_EXPORT_ARCHIVE_BYTES", size)
    shutil.rmtree(tree.root / "7_armarium")

    assert (
        _rerun_armarium_in_process(monkeypatch, root, embedded_run.parent / "formats.toml")
        == EXIT_COMPLETE
    )
    out = tmp_path / "delivery"
    bundle_module.publish(RunTree(root, "embedded"), out)
    assert (out / "armarium-export.zip").stat().st_size == size


def test_an_archive_over_its_ceiling_is_refused_by_name_before_it_is_stored(
    tmp_path, embedded_run, monkeypatch, capsys
):
    root = tmp_path / "runs"
    shutil.copytree(embedded_run, root, symlinks=True)
    tree = RunTree(root, "embedded")
    size = _archive_and_page_ceilings(monkeypatch, tree)
    monkeypatch.setattr(armarium_formats, "MAX_EXPORT_ARCHIVE_BYTES", size - 1)
    shutil.rmtree(tree.root / "7_armarium")

    assert (
        _rerun_armarium_in_process(monkeypatch, root, embedded_run.parent / "formats.toml")
        == EXIT_FATAL
    )
    assert f"{size - 1}-byte export archive limit" in capsys.readouterr().err
    assert not (tree.root / "7_armarium" / "blobs").exists()


@pytest.mark.parametrize(
    ("missing_field", "expected_reason"),
    [
        ("provenance", "model identity provenance"),
        ("regions", "source-region provenance"),
    ],
)
def test_provenance_less_established_reading_becomes_a_visible_refusal(
    tmp_path, missing_field, expected_reason
):
    root = tmp_path / "runs"
    result = _orchestrate(root, "refusal")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "refusal")
    original = next(
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    refused_act_id = original["subject_id"]

    # An immutable stage artifact cannot be altered through its normal writer.
    # This synthetic reseal is the precise counterfactual Armarium must account
    # for: the transport envelope remains valid, but its established reading
    # has no exportable provenance. Clear old Armarium output first, or the
    # new terminal record would collide with the prior happy export's identity.
    shutil.rmtree(tree.root / "7_armarium")
    altered = json.loads(json.dumps(original))
    altered["payload"].pop(missing_field)
    altered["payload"]["self_hash"] = self_hash(altered["payload"])
    altered["self_hash"] = self_hash(altered)
    artifact_path = tree.resolve(
        tree.artifact_path(ARCHETYPUS, "archetypus", altered["artifact_id"])
    )
    artifact_path.write_bytes(canonical_bytes(altered))
    _rebind_stage_seal(tree, ARCHETYPUS)

    result = _run_armarium(root, "refusal")
    assert result.returncode == 3, result.stderr
    export = _export(tree)
    assert export["payload"]["aggregate"]["status"] == "partial"
    refused = [
        entry for entry in export["payload"]["non_delivered"] if entry["act_id"] == refused_act_id
    ]
    assert len(refused) == 1
    assert refused[0]["category"] == "refused-with-reason"
    assert expected_reason in refused[0]["reason"]
    assert not [
        entry for entry in export["payload"]["delivered"] if entry["act_id"] == refused_act_id
    ]

    reference = export["payload"]["bundle"]["reference"]
    clean = tmp_path / f"clean-{missing_field}"
    verify_export_bundle(tree.read_bytes(reference["relative_path"]), clean)
    # `encoding="utf-8"` explicitly: `read_text()` without it decodes under the
    # locale, and the bundle is written as UTF-8 by `_jsonl_bytes`. A machine
    # whose locale is not UTF-8 would decode a published product's own bytes
    # differently from the machine that wrote them — the same environment
    # dependence this branch already carries in its sealed bundle identity.
    rows = [
        json.loads(line) for line in (clean / "acts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    row = next(item for item in rows if item["act_id"] == refused_act_id)
    assert row["category"] == "refused-with-reason"
    assert row["canonical_clean_text"] is None
    assert verify_delivered_bundle(
        tree.read_bytes(reference["relative_path"]), tmp_path / f"id-{missing_field}"
    )


def _page_record_case():
    """An accepted page reading, its review and its Archetypus, all mutually consistent."""
    reading_ref = {"relative_path": "4_perlector/artifacts/perlectio/r.json", "sha256": "a" * 64}
    review_ref = {"relative_path": "5_recensor/artifacts/review/v.json", "sha256": "b" * 64}
    region_ref = {"relative_path": "4_perlector/artifacts/act-region/g.json", "sha256": "c" * 64}
    crop_ref = {"relative_path": "4_perlector/blobs/crop", "sha256": "d" * 64}
    region = {"image_path": crop_ref["relative_path"], "image_sha256": crop_ref["sha256"]}
    row = {
        "act_id": "act_0000000000000001",
        "act_key": "p1-e1",
        "page_id": "pg_0000000000000001",
        "kind": "act",
        "perlectio_ref": reading_ref,
        "region_ref": region_ref,
    }
    reading_payload = {
        "schema": "perlectio.v3",
        "kind": "act",
        "text": "Maria",
        "provenance": {"chair": "perlector"},
        "act_region_ref": region_ref,
        "holds": [],
        "page_holds": [],
        "uncertain_spans": [],
        "gaps": [],
        "uncertainty_assessment": {
            "state": "assessed",
            "problem": None,
            "uncertain_spans": [],
            "gaps": [],
        },
    }
    reading = {"outcome": "read", "payload": reading_payload}
    review = {
        "artifact_id": "review-1",
        "outcome": "accepted",
        "payload": {"perlectio_ref": reading_ref},
        "inputs": [reading_ref],
    }
    records = {"perlectio": reading, "act-region": {"payload": {}}}
    context = SimpleNamespace(
        artifact_ref=lambda *_args: review_ref,
        input_ref=lambda _path: crop_ref,
        run={},
        tree=SimpleNamespace(
            read_artifact_reference=lambda _ref, *, kind, **_kwargs: records[kind]
        ),
    )
    return (
        context,
        row,
        review,
        reading_payload,
        region,
        [review_ref, reading_ref, region_ref, crop_ref],
    )


def _sealed_page_record(armarium, row, reading_payload, region, **changes):
    payload = {
        "schema": "archetypus-record.v2",
        "act_id": row["act_id"],
        "act_key": row["act_key"],
        "page_id": row["page_id"],
        "kind": row["kind"],
        "status": "established",
        "text": reading_payload["text"],
        "regions": [region],
        "provenance": reading_payload["provenance"],
        "uncertainty": armarium.from_page_perlectio(reading_payload),
        "text_status": "established",
        "recensor_ref": {"relative_path": "5_recensor/artifacts/review/v.json", "sha256": "b" * 64},
        "perlectio_ref": row["perlectio_ref"],
        "dissent_ref": row["perlectio_ref"],
        **changes,
    }
    payload["self_hash"] = self_hash(payload)
    return payload


def test_an_archetypus_claiming_whole_text_over_its_reading_s_gap_is_refused_at_export(
    monkeypatch,
):
    """`text_status` is recomputed from the reading here, never read out of the record."""
    armarium = load_stage("7_armarium")
    context, row, review, reading_payload, region, inputs = _page_record_case()
    monkeypatch.setattr(armarium, "verify_reading_region_lineage", lambda *_args: region)

    whole = _sealed_page_record(armarium, row, reading_payload, region)
    payload, _reading = armarium.verify_established_page_record(
        context, row, review, {"payload": whole, "inputs": inputs}
    )
    assert payload["text_status"] == "established"

    gap = {"position": "internal", "start": 2, "end": 2, "witness_evidence": []}
    reading_payload["gaps"] = [gap]
    reading_payload["uncertainty_assessment"]["gaps"] = [gap]
    # The record copies the reading's gap and still claims `established`.
    forged = _sealed_page_record(
        armarium,
        row,
        reading_payload,
        region,
        uncertainty=armarium.from_page_perlectio(reading_payload),
    )
    with pytest.raises(FatalAccounting, match="does not exactly preserve"):
        armarium.verify_established_page_record(
            context, row, review, {"payload": forged, "inputs": inputs}
        )


def _forge_self_hash(case):
    case["payload"]["text"] = "Marie"


def _forge_kind(case):
    case["payload"] = _reseal(case["payload"], kind="other")


def _forge_review(case):
    case["review"]["outcome"] = "held-for-review"


def _forge_region_ref(case):
    case["reading"]["act_region_ref"] = {"relative_path": "elsewhere.json", "sha256": "e" * 64}


def _forge_holds(case):
    case["reading"]["holds"] = ["page-unread"]


def _forge_lineage(case):
    def stale(*_args):
        raise ContractError("the crop bytes do not match the sealed page region")

    case["monkeypatch"].setattr(case["armarium"], "verify_reading_region_lineage", stale)


def _forge_gaps(case):
    case["reading"]["gaps"] = "not a list"


def _forge_inputs(case):
    case["inputs"].pop()


def _reseal(payload, **changes):
    resealed = {key: value for key, value in payload.items() if key != "self_hash"}
    resealed.update(changes)
    resealed["self_hash"] = self_hash(resealed)
    return resealed


@pytest.mark.parametrize(
    ("forge", "refusal"),
    [
        (_forge_self_hash, "fails its own self-hash before export"),
        (_forge_kind, "does not describe the reading being exported"),
        (_forge_review, "is not bound to the accepted review"),
        (_forge_region_ref, "names another act-region"),
        (_forge_holds, "is held or is not the row's own page reading"),
        (_forge_lineage, "does not trace to the Exemplar"),
        (_forge_gaps, "cannot be reconciled with its reading"),
        (_forge_inputs, "does not input exactly its review, reading"),
    ],
)
def test_each_way_an_archetypus_can_disagree_with_its_page_reading_is_refused_at_export(
    monkeypatch, forge, refusal
):
    """The export re-proves every established record against its row, review and reading."""
    armarium = load_stage("7_armarium")
    context, row, review, reading_payload, region, inputs = _page_record_case()
    monkeypatch.setattr(armarium, "verify_reading_region_lineage", lambda *_args: region)
    case = {
        "armarium": armarium,
        "monkeypatch": monkeypatch,
        "payload": _sealed_page_record(armarium, row, reading_payload, region),
        "review": review,
        "reading": reading_payload,
        "inputs": inputs,
    }
    forge(case)
    with pytest.raises(FatalAccounting, match=refusal):
        armarium.verify_established_page_record(
            context, row, case["review"], {"payload": case["payload"], "inputs": case["inputs"]}
        )
