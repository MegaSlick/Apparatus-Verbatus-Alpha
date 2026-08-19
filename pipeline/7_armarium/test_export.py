"""End-to-end Armarium product exports over the real sealed fixture pipeline."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from armarium_export import verify_export_bundle, verify_projection_identity

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import FatalAccounting
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
ARMARIUM_CLI = ROOT / "pipeline" / "7_armarium" / "run.py"


def _orchestrate(
    run_root: Path, run_id: str, *, formats_config: Path | None = None
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(ORCHESTRATOR),
        "--fixture",
        "synthetic-two-page-v0",
        "--scenario",
        "happy",
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


def _run_armarium(run_root: Path, run_id: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ARMARIUM_CLI),
            "--run-root",
            str(run_root),
            "--run-id",
            run_id,
            "--scenario",
            "happy",
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


def test_armarium_seals_a_self_verifying_product_bundle(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, "bundle")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "bundle")
    export = _export(tree)
    bundle = export["payload"]["bundle"]
    reference = bundle["reference"]
    assert export["inputs"] == [reference]
    assert bundle["sha256"] == reference["sha256"]

    clean = tmp_path / "clean"
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), clean)
    # The happy fixture loses nothing, so the ledger says so. A status that reads
    # `partial` on every run whatever happened could not report the run that did.
    assert manifest["claims"]["status"] == "complete"
    assert manifest["claims"]["partial_reasons"] == []
    ledger = manifest["claims"]["terminal_ledger"]
    assert ledger["by_unit_type"] == {"source": 2, "page": 2, "act": 2}
    assert ledger["by_unit_type"]["source"] == manifest["claims"]["page_census"]["counted"]
    assert sum(ledger["by_category"].values()) == ledger["unit_count"] == 6
    assert (
        manifest["claims"]["submission_inventory"]["status"]
        == "reconciled-at-source-page-ordinal-granularity"
    )
    assert verify_projection_identity(
        tree.read_bytes(reference["relative_path"]), tmp_path / "identity"
    )


def test_run_bound_pixel_embedding_packages_page_and_crop_bytes(tmp_path):
    formats = tmp_path / "formats.toml"
    formats.write_text(
        'schema = "armarium-formats.v1"\n'
        'formats = ["text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"]\n'
        "embed_pixels = true\n",
        encoding="utf-8",
    )
    root = tmp_path / "runs"
    result = _orchestrate(root, "embedded", formats_config=formats)
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "embedded")
    reference = _export(tree)["payload"]["bundle"]["reference"]
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), tmp_path / "clean")
    assert manifest["formats"]["embed_pixels"] is True
    assert manifest["claims"]["pixels"]["resolution_claim"].startswith("embedded pixels")


def test_product_keeps_non_text_provenance_and_every_continuation_citation(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, "provenance")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "provenance")
    export = _export(tree)
    delivered = next(entry for entry in export["payload"]["delivered"] if entry["act_key"] == "a2")
    reference = export["payload"]["bundle"]["reference"]
    bundle_bytes = tree.read_bytes(reference["relative_path"])
    with ZipFile(BytesIO(bundle_bytes)) as archive:
        rows = [json.loads(line) for line in archive.read("acts.jsonl").decode().splitlines()]
        row = next(item for item in rows if item["act_id"] == delivered["act_id"])
        text = archive.read(
            "text/_source_folder/fixtures/synthetic-two-page-v0/readings.txt"
        ).decode()

    assert delivered["witnesses"], "the delivered act carried no witnesses to compare"
    assert len(row["witnesses"]) == len(delivered["witnesses"])
    assert row["perlectio_ref"] == {
        "availability": "requires-retained-run-access",
        "run_relative_path": delivered["perlectio_ref"]["relative_path"],
        "sha256": delivered["perlectio_ref"]["sha256"],
    }
    assert row["witnesses"][0]["testimonium_ref"]["availability"] == (
        "requires-retained-run-access"
    )
    assert "relative_path" not in row["perlectio_ref"]
    assert delivered["source_regions"], "the delivered act carried no source citations"
    for region in delivered["source_regions"]:
        assert f"source-page: {region['declared_path']}" in text
        assert f"source-sha256: {region['declared_sha256']}" in text


def test_happy_run_marks_absent_salvage_inventory_as_not_produced(tmp_path):
    root = tmp_path / "runs"
    result = _orchestrate(root, "happy-no-salvage")
    assert result.returncode == 0, result.stderr

    tree = RunTree(root, "happy-no-salvage")
    assert not any(
        entry["kind"] == "salvage-inventory" for entry in tree.build_manifest(RECENSOR)["artifacts"]
    )
    reference = _export(tree)["payload"]["bundle"]["reference"]
    manifest = verify_export_bundle(tree.read_bytes(reference["relative_path"]), tmp_path / "clean")
    assert manifest["claims"]["salvage"]["status"] == ("not-produced-no-sealed-salvage-inventory")
    assert manifest["claims"]["salvage"]["count"] is None


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
    # for: the transport envelope remains valid, but its established reading has
    # no exportable provenance.  Remove old Armarium output so the new terminal
    # record has no immutable-identity collision with the prior happy export.
    shutil.rmtree(tree.root / "7_armarium")
    altered = json.loads(json.dumps(original))
    altered["payload"].pop(missing_field)
    altered["payload"]["self_hash"] = self_hash(altered["payload"])
    altered["self_hash"] = self_hash(altered)
    artifact_path = tree.resolve(
        tree.artifact_path(ARCHETYPUS, "archetypus", altered["artifact_id"])
    )
    artifact_path.write_bytes(canonical_bytes(altered))

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
    # Found by CodeRabbit.
    rows = [
        json.loads(line) for line in (clean / "acts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    row = next(item for item in rows if item["act_id"] == refused_act_id)
    assert row["category"] == "refused-with-reason"
    assert row["canonical_clean_text"] is None
    assert verify_projection_identity(
        tree.read_bytes(reference["relative_path"]), tmp_path / f"id-{missing_field}"
    )


def test_a_provenance_that_fails_deeper_validation_is_also_downgraded_to_refused(tmp_path):
    """The `except SchemaRefusal` branch in run.py's main(), not just the narrower
    field-presence check `missing_export_provenance` performs before it.

    Provenance and regions are both structurally present here -- unlike the
    parametrized test above -- so `missing_export_provenance` finds nothing wrong.
    Tampering only the Perlectio's provenance would just trip
    `verify_established_record`'s exact-preservation check (it would no longer
    match the Archetypus's copy) -- the shallower, already-covered branch. Reaching
    `validate_serving_provenance` needs the Archetypus's own provenance tampered
    identically, which in turn means every digest-checked reference between the
    three sealed records -- Perlectio, the Recensor review that accepted it, and
    the Archetypus -- has to be updated to match the new bytes: exactly the
    chain-of-custody `verify_established_record` exists to enforce, so a corrupted
    provenance cannot simply carry its own falsified referrers along with it.
    """
    root = tmp_path / "runs"
    result = _orchestrate(root, "deeper-refusal")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "deeper-refusal")
    original = next(
        tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
    )
    refused_act_id = original["subject_id"]

    perlectio_ref = original["payload"]["perlectio_ref"]
    perlectio_original = tree.read_artifact_reference(
        perlectio_ref, stage=PERLECTOR, kind="perlectio", subject_id=refused_act_id
    )
    recensor_ref = original["payload"]["recensor_ref"]
    review_original = tree.read_artifact_reference(
        recensor_ref, stage=RECENSOR, kind="review", subject_id=refused_act_id
    )

    shutil.rmtree(tree.root / "7_armarium")
    endpoint = "https://example.invalid/served"

    def _with_updated_ref(inputs, old_ref, new_ref):
        return [new_ref if entry == old_ref else entry for entry in inputs]

    altered_perlectio = json.loads(json.dumps(perlectio_original))
    altered_perlectio["payload"]["provenance"]["endpoint"] = endpoint
    altered_perlectio["payload"]["self_hash"] = self_hash(altered_perlectio["payload"])
    altered_perlectio["self_hash"] = self_hash(altered_perlectio)
    perlectio_path = tree.resolve(
        tree.artifact_path(PERLECTOR, "perlectio", altered_perlectio["artifact_id"])
    )
    perlectio_path.write_bytes(canonical_bytes(altered_perlectio))
    new_perlectio_ref = {
        "relative_path": perlectio_ref["relative_path"],
        "sha256": digest_bytes(canonical_bytes(altered_perlectio)),
    }

    altered_review = json.loads(json.dumps(review_original))
    altered_review["payload"]["perlectio_ref"] = new_perlectio_ref
    altered_review["inputs"] = _with_updated_ref(
        altered_review["inputs"], perlectio_ref, new_perlectio_ref
    )
    altered_review["self_hash"] = self_hash(altered_review)
    review_path = tree.resolve(
        tree.artifact_path(RECENSOR, "review", altered_review["artifact_id"])
    )
    review_path.write_bytes(canonical_bytes(altered_review))
    new_recensor_ref = {
        "relative_path": recensor_ref["relative_path"],
        "sha256": digest_bytes(canonical_bytes(altered_review)),
    }

    altered = json.loads(json.dumps(original))
    altered["payload"]["provenance"]["endpoint"] = endpoint
    altered["payload"]["perlectio_ref"] = new_perlectio_ref
    altered["payload"]["dissent_ref"] = new_perlectio_ref
    altered["payload"]["recensor_ref"] = new_recensor_ref
    altered["inputs"] = _with_updated_ref(
        _with_updated_ref(altered["inputs"], perlectio_ref, new_perlectio_ref),
        recensor_ref,
        new_recensor_ref,
    )
    altered["payload"]["self_hash"] = self_hash(altered["payload"])
    altered["self_hash"] = self_hash(altered)
    artifact_path = tree.resolve(
        tree.artifact_path(ARCHETYPUS, "archetypus", altered["artifact_id"])
    )
    artifact_path.write_bytes(canonical_bytes(altered))

    result = _run_armarium(root, "deeper-refusal")
    assert result.returncode == 3, result.stderr
    export = _export(tree)
    assert export["payload"]["aggregate"]["status"] == "partial"
    refused = [
        entry for entry in export["payload"]["non_delivered"] if entry["act_id"] == refused_act_id
    ]
    assert len(refused) == 1
    assert refused[0]["category"] == "refused-with-reason"
    assert "provenance was refused" in refused[0]["reason"]
    assert "leaks serving-only field" in refused[0]["reason"]
    assert not [
        entry for entry in export["payload"]["delivered"] if entry["act_id"] == refused_act_id
    ]


def test_a_digest_damaged_testimonium_hard_stops_instead_of_exporting_partial(tmp_path):
    """Broken witness custody is damage, not an act-level provenance refusal."""
    root = tmp_path / "runs"
    result = _orchestrate(root, "damaged-testimonium")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "damaged-testimonium")
    first_act = next(
        item for item in _export(tree)["payload"]["delivered"] if item["act_key"] == "a1"
    )
    reading = tree.read_artifact_reference(
        first_act["perlectio_ref"],
        stage=PERLECTOR,
        kind="perlectio",
        subject_id=first_act["act_id"],
    )
    testimony_ref = reading["payload"]["basis"]["testimonia"][0]["reference"]
    testimony_path = tree.resolve(testimony_ref["relative_path"])

    shutil.rmtree(tree.root / "7_armarium")
    # Whitespace keeps the Testimonium readable JSON while changing the bytes
    # under the Perlectio's sealed digest reference.
    testimony_path.write_bytes(testimony_path.read_bytes() + b"\n")

    result = _run_armarium(root, "damaged-testimonium")

    assert result.returncode == 2
    assert "bytes changed under a sealed reference" in result.stderr
    assert not tree.has_artifact(
        ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
    )
    armarium_root = tree.root / "7_armarium"
    assert not armarium_root.exists() or not any(
        path.is_file() for path in armarium_root.rglob("*")
    )


def test_a_damaged_witness_receipt_hard_stops_rather_than_refusing_only_its_act(tmp_path):
    """The narrowed `except SchemaRefusal` scope, driven where it is the only guard.

    The test above damages a Testimonium, and that never reaches the narrow catch at
    all: `build_manifest(PERLECTOR)` revalidates every Perlectio's sealed inputs and
    raises first, so the run hard-stops with the catch widened or narrow. A serving
    receipt is not an artifact input, so nothing revalidates it before
    `export_witnesses` reads it -- and with the catch widened back over that read, this
    run exports a *partial* product at exit 3 with the act merely refused, over
    witness custody the stage could not verify. Damaged evidence is fatal contract
    damage, not one act's provenance refusal.
    """
    root = tmp_path / "runs"
    result = _orchestrate(root, "damaged-receipt")
    assert result.returncode == 0, result.stderr
    tree = RunTree(root, "damaged-receipt")
    first_act = next(
        item for item in _export(tree)["payload"]["delivered"] if item["act_key"] == "a1"
    )
    receipt_ref = first_act["witnesses"][0]["provenance"]["receipt_ref"]
    receipt_path = tree.resolve(receipt_ref["relative_path"])

    shutil.rmtree(tree.root / "7_armarium")
    receipt_path.write_bytes(receipt_path.read_bytes() + b"\n")

    result = _run_armarium(root, "damaged-receipt")

    assert result.returncode == 2, result.stdout
    assert "run receipt" in result.stderr
    assert not tree.has_artifact(
        ARMARIUM, "export", artifact_id(ARMARIUM, "export", "export", None)
    )


# --- The act-attachment view is required at export, not merely checked ----------
#
# Opus audit-and-repair seat 3, R0. F-O2: `export_witnesses` rechecked R0's
# act-attachment dossier view only `if attachment is not None`, so an established
# reading that had dropped the field exported with its page-witness custody never
# rechecked here. The retained witness basis beside it was already required.


def _armarium_module():
    """Load the stage program under a unique name.

    Never a bare ``import run``: several stage directories define a module by that
    name, and the import cache would decide which one this test got.
    """
    spec = importlib.util.spec_from_file_location("armarium_run_under_test_export", ARMARIUM_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _established_uncertainty_case(armarium, monkeypatch):
    act = {"act_id": "act-1", "act_key": "a1", "page_id": "page-1"}
    reading_ref = {"relative_path": "perlectio.json", "sha256": "a" * 64}
    review_ref = {"relative_path": "review.json", "sha256": "b" * 64}
    provenance = {"chair": "perlector"}
    layer = {"uncertain_spans": [], "gaps": [], "self_revisions": []}
    reading = {
        "artifact_id": "reading-1",
        "payload": {"text": "Maria", "provenance": provenance},
    }
    checked_review = {
        "artifact_id": "review-1",
        "outcome": "accepted",
        "payload": {"perlectio_ref": reading_ref},
        "inputs": [reading_ref],
    }
    payload = {
        **act,
        "status": "established",
        "text": "Maria",
        "regions": [],
        "provenance": provenance,
        "dissent_ref": reading_ref,
        "perlectio_ref": reading_ref,
        "recensor_ref": review_ref,
        "uncertainty": layer,
    }
    payload["self_hash"] = self_hash(payload)
    established = {"payload": payload, "inputs": [review_ref, reading_ref]}
    review = {"artifact_id": "review-1"}

    def read_artifact_reference(_reference, *, stage, **_kwargs):
        return checked_review if stage == RECENSOR else reading

    context = SimpleNamespace(
        artifact_ref=lambda *_args: review_ref,
        tree=SimpleNamespace(read_artifact_reference=read_artifact_reference),
    )
    monkeypatch.setattr(
        armarium,
        "artifacts_for",
        lambda _context, stage, *_args: [] if stage == armarium.DESIGNATOR else [reading],
    )
    monkeypatch.setattr(armarium, "latest_attempt", lambda *_args, **_kwargs: reading)
    monkeypatch.setattr(armarium, "recovery_region_count", lambda *_args: 0)
    monkeypatch.setattr(armarium, "reading_basis_regions", lambda *_args: [])
    return context, act, review, established, layer


def test_malformed_perlectio_is_attributed_to_the_perlectio(monkeypatch):
    armarium = _armarium_module()
    context, act, review, established, _layer = _established_uncertainty_case(armarium, monkeypatch)

    def refuse_perlectio(_payload):
        raise armarium.SchemaRefusal("malformed producer layer")

    monkeypatch.setattr(armarium, "from_perlectio", refuse_perlectio)

    with pytest.raises(FatalAccounting, match="accepted Perlectio is malformed"):
        armarium.verify_established_record(context, act, review, established, {})


def test_malformed_archetypus_uncertainty_is_attributed_to_the_archetypus(monkeypatch):
    armarium = _armarium_module()
    context, act, review, established, layer = _established_uncertainty_case(armarium, monkeypatch)
    monkeypatch.setattr(armarium, "from_perlectio", lambda _payload: layer)

    def refuse_archetypus(_layer, _text):
        raise armarium.SchemaRefusal("malformed established layer")

    monkeypatch.setattr(armarium, "validate_uncertainty", refuse_archetypus)

    with pytest.raises(FatalAccounting, match="Archetypus uncertainty layer is malformed"):
        armarium.verify_established_record(context, act, review, established, {})


def test_an_established_reading_without_its_act_attachment_view_is_refused_at_export():
    """R0's exit criterion says the attachment is consumed, not consumed-if-present."""
    armarium = _armarium_module()
    reading = {
        "inputs": [],
        "payload": {
            "basis": {"testimonia": [{"chair": "attestator_1"}]},
            "dossier": {"act_key": "a1", "dossier_digest": "d" * 64},
        },
    }
    with pytest.raises(FatalAccounting, match="no act-attachment evidence"):
        armarium.export_witnesses(None, reading, "act_0000000000000001")
