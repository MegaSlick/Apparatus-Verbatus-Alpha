"""Synthetic private-reference checks for the per-stage canary alarm."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from common.contracts.canonical import digest_bytes, self_hash
from common.contracts.identities import attempt_id
from operations.corpus import canary
from operations.corpus.local_admission import admit_local_set
from operations.corpus.reference import build_reference_page
from operations.spike_perlector.models import OutputStatus

from .test_local_admission import _two_page_set


def test_healthy_canary_is_silent_and_dai_failure_names_training_page(monkeypatch, tmp_path):
    reference_text = "the synthetic record has enough distinct ink to compare"
    reference = {"acts": [{"physical_act_id": "gold-act", "text": reference_text}]}
    monkeypatch.setattr(canary, "_references", lambda _root: {"a" * 64: reference})
    monkeypatch.setattr(canary, "load_exemplar_page_shas", lambda _tree: {2: "a" * 64})
    monkeypatch.setattr(
        canary, "load_pipeline_proposal_acts", lambda _tree: [{"page_sha256": "a" * 64}]
    )
    monkeypatch.setattr(canary, "sealed_page_bindings", lambda _tree: {})
    monkeypatch.setattr(
        canary,
        "compare_page_geometry",
        lambda *_args: {
            "matched_pairs": [{"pipeline_act_id": "act", "reference_physical_act_id": "gold-act"}]
        },
    )
    monkeypatch.setattr(
        canary,
        "attachment_index",
        lambda *_args, **_kwargs: {
            "act": {
                chair: [{"attachment": {"page_witness": False}, "testimonium": {"chair": chair}}]
                for chair in canary.CHAIRS
            }
        },
    )
    bad_chair = set()
    monkeypatch.setattr(
        canary,
        "witness_reading",
        lambda _attachment, testimony: (
            OutputStatus.COMPLETE,
            "unrelated symbols" if testimony["chair"] in bad_chair else reference_text,
            None,
        ),
    )

    class Tree:
        run_id = "synthetic-canary"

        def read_run(self):
            return {
                "sealed_config_digests": {"canary-ledger": "c" * 64},
                "source_manifest": [{"ordinal": 2, "ledger_sha256": "c" * 64}],
            }

        def build_manifest(self, stage):
            if stage == canary.PERLECTOR:
                return {
                    "artifacts": [
                        {"kind": "stage-seal"},
                        {"kind": "perlectio", "subject_id": "act", "artifact_id": "reading"},
                    ]
                }
            if stage == canary.ARMARIUM:
                return {
                    "artifacts": [
                        {"kind": "stage-seal"},
                        {"kind": "export", "artifact_id": "export"},
                    ]
                }
            return {"artifacts": [{"kind": "stage-seal"}]}

        def read_artifact(self, stage, kind, artifact_id):
            if kind == "perlectio":
                return {
                    "subject_id": "act",
                    "artifact_id": artifact_id,
                    "attempt_id": attempt_id("act", "perlegere", 1),
                    "outcome": "read",
                    "payload": {"text": reference_text, "attempt_ordinal": 1},
                }
            return {
                "payload": {
                    "canary": {"ordinals": [2], "acts": [{"act_id": "act", "page_ordinals": [2]}]},
                    "pages": [{"ordinal": 1}],
                    "delivered": [],
                    "non_delivered": [],
                }
            }

    tree = Tree()
    healthy = canary.check_run(tree, tmp_path)
    assert healthy["dead"] == []
    assert all(healthy["stages"].values())
    assert reference_text not in str(healthy)
    assert healthy["self_hash"] == self_hash(healthy)

    bad_chair.add("attestator_2")
    dead = canary.check_run(tree, tmp_path)
    assert dead["dead"] == [
        {
            "stage": "attestator_2",
            "rule": "DAI failed on a page it was trained on",
        }
    ]
    assert not dead["stages"]["attestator_2"]
    assert reference_text not in str(dead)

    original_manifest = tree.build_manifest
    tree.build_manifest = lambda stage: (
        {"artifacts": []} if stage == canary.DESIGNATOR else original_manifest(stage)
    )
    unsealed = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in unsealed["dead"]} >= {"stage-not-sealed"}
    assert not unsealed["stages"][canary.DESIGNATOR]

    tree.build_manifest = original_manifest
    tree.build_manifest = lambda stage: (
        {"artifacts": []} if stage == canary.ATTESTATORES else original_manifest(stage)
    )
    dead_chairs = canary.check_run(tree, tmp_path)
    assert all(not dead_chairs["stages"][chair] for chair in canary.CHAIRS)
    assert all(
        any(
            row["stage"] == chair and row["rule"] == "stage-not-sealed"
            for row in dead_chairs["dead"]
        )
        for chair in canary.CHAIRS
    )
    tree.build_manifest = original_manifest
    original_artifact = tree.read_artifact

    def leaked(stage, kind, artifact_id):
        record = original_artifact(stage, kind, artifact_id)
        if kind == "export":
            record["payload"]["delivered"] = [{"act_id": "act"}]
        return record

    tree.read_artifact = leaked
    leak = canary.check_run(tree, tmp_path)
    assert not leak["stages"][canary.ARMARIUM]
    assert {row["rule"] for row in leak["dead"]} >= {"canary-reached-real-export"}

    tree.read_artifact = original_artifact
    tree.build_manifest = lambda stage: (
        {
            "artifacts": original_manifest(stage)["artifacts"]
            + [{"kind": "export", "artifact_id": "other"}]
        }
        if stage == canary.ARMARIUM
        else original_manifest(stage)
    )
    ambiguous = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in ambiguous["dead"]} >= {"canary-export-ambiguous"}

    tree.build_manifest = lambda stage: (
        {
            "artifacts": original_manifest(stage)["artifacts"]
            + [{"kind": "perlectio", "subject_id": "act", "artifact_id": "new-reading"}]
        }
        if stage == canary.PERLECTOR
        else original_manifest(stage)
    )

    def newer(stage, kind, artifact_id):
        if artifact_id == "new-reading":
            return {
                "subject_id": "act",
                "artifact_id": artifact_id,
                "attempt_id": attempt_id("act", "perlegere", 2),
                "outcome": "read",
                "payload": {"text": "unrelated symbols", "attempt_ordinal": 2},
            }
        return original_artifact(stage, kind, artifact_id)

    tree.read_artifact = newer
    latest = canary.check_run(tree, tmp_path)
    assert not latest["stages"][canary.PERLECTOR]


def test_check_exception_seals_a_dead_verdict_for_every_chair(monkeypatch, tmp_path):
    class Tree:
        run_id = "synthetic"

        def read_run(self):
            return {
                "sealed_config_digests": {"canary-ledger": "c" * 64},
                "source_manifest": [{"ordinal": 1, "ledger_sha256": "c" * 64}],
            }

    monkeypatch.setattr(canary, "_references", lambda _root: (_ for _ in ()).throw(KeyError("bad")))
    verdict = canary.check_run(Tree(), tmp_path)
    assert {row["rule"] for row in verdict["dead"]} == {"check-raised:KeyError"}
    assert all(not verdict["stages"][chair] for chair in canary.CHAIRS)
    assert verdict["self_hash"] == self_hash(verdict)


def test_build_copies_only_selected_synthetic_reference_pages(tmp_path):
    source = tmp_path / "source"
    (source / "pages").mkdir(parents=True)
    image = source / "pages" / "source.png"
    Image.new("RGB", (20, 20), "white").save(image)
    sha = digest_bytes(image.read_bytes())
    chosen = source / "pages" / f"{sha}.png"
    image.rename(chosen)
    text = "Synthetic reference text"
    reference = build_reference_page(
        page={"sha256": sha, "width": 20, "height": 20},
        source="Synthetic",
        volume="v1",
        designation="p1",
        split="train",
        records=[
            {
                "record_id": "r1",
                "region": {"x": 1, "y": 1, "w": 10, "h": 10},
                "split": "train",
                "text": text,
                "text_sha256": digest_bytes(text.encode()),
            }
        ],
    )
    (source / "reference-pages.json").write_text(json.dumps([reference]))
    output = tmp_path / "private-canary"
    assert canary.build(source, [sha], output) == output
    assert (output / "pages" / chosen.name).read_bytes() == chosen.read_bytes()
    assert json.loads((output / "reference-pages.json").read_text()) == [reference]
    assert (
        json.loads((output / "submission-manifest.json").read_text())["files"][0]["sha256"] == sha
    )
    with pytest.raises(ValueError, match="under private"):
        canary.build(source, [sha], canary.Path(__file__).resolve().parents[2] / "tracked-canary")
    command_output = tmp_path / "command-canary"
    assert (
        canary.main(
            [
                "build",
                "--source-dir",
                str(source),
                "--page-sha",
                sha,
                "--output-root",
                str(command_output),
            ]
        )
        == 0
    )
    assert (command_output / "submission-manifest.json").exists()


def test_build_derives_references_from_a_synthetic_recordgold_set(tmp_path):
    source = _two_page_set(tmp_path / "recordgold")
    admitted = admit_local_set(source, split="val")
    sha = admitted["reference_pages"][0]["page"]["sha256"]
    output = canary.build(source, [sha], tmp_path / "canary", split="val")
    assert len(json.loads((output / "reference-pages.json").read_text())) == 1
    assert len(list((output / "pages").iterdir())) == 1
