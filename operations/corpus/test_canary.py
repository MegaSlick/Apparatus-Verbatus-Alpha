"""Synthetic private-reference checks for the per-stage canary alarm."""

from __future__ import annotations

from common.contracts.canonical import self_hash
from operations.corpus import canary
from operations.spike_perlector.models import OutputStatus


def test_healthy_canary_is_silent_and_dai_failure_names_training_page(monkeypatch, tmp_path):
    reference_text = "the synthetic record has enough distinct ink to compare"
    reference = {"acts": [{"physical_act_id": "gold-act", "text": reference_text}]}
    monkeypatch.setattr(canary, "_references", lambda _root: {"a" * 64: reference})
    monkeypatch.setattr(canary, "load_exemplar_page_shas", lambda _tree: {2: "a" * 64})
    monkeypatch.setattr(
        canary, "load_pipeline_proposal_acts", lambda _tree: [{"page_sha256": "a" * 64}]
    )
    monkeypatch.setattr(canary, "_sealed_page_bindings", lambda _tree: {})
    monkeypatch.setattr(
        canary,
        "compare_page_geometry",
        lambda *_args: {
            "matched_pairs": [{"pipeline_act_id": "act", "reference_physical_act_id": "gold-act"}]
        },
    )
    monkeypatch.setattr(
        canary,
        "_attachment_index",
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
                return {"outcome": "read", "payload": {"text": reference_text}}
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
            "value": "failed",
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
