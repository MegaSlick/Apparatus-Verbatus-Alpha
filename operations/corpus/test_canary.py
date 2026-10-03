"""Synthetic private-reference checks for the per-stage canary alarm."""

from __future__ import annotations

import json
import sqlite3
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

import pytest
from PIL import Image

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from operations.corpus import canary
from operations.corpus.local_admission import admit_local_set
from operations.corpus.normalization import MAX_TEXT_LENGTH
from operations.corpus.reference import build_reference_page
from operations.corpus.scoring import OutputStatus

from .test_local_admission import _two_page_set


def _bundle(members=None):
    contents = {"EXPORT_MANIFEST.json": b"{}", "sources.json": b"{}", **(members or {})}
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize(
    "member, contents",
    [
        ("sources.json", canonical_bytes({"act_outcomes": [{"act_id": "act"}]})),
        ("sources.json", canonical_bytes({"regions": [{"source_page_ordinal": 2}]})),
        ("text/_source_root/readings.txt", b"## act (act)\nact-id: act\n"),
        ("review-items.jsonl", canonical_bytes({"act_id": "act"}) + b"\n"),
        ("coniector.jsonl", canonical_bytes({"act_ids": ["other", "act"]}) + b"\n"),
        ("sources.json", canonical_bytes({"joins": [{"head_act_ids": ["act"]}]})),
        ("sources.json", canonical_bytes({"reconstructions": [["other"], ["other", "act"]]})),
        ("other.jsonl", canonical_bytes({"act_id": "act"}) + b"\n"),
        ("text/_source_root/readings.txt", b"## OTHER act (not an act)\nother-id: act\n"),
    ],
)
def test_bundle_inspection_finds_canary_identity_without_reference_text(member, contents):
    assert canary._canary_in_bundle(_bundle({member: contents}), {"act"}, {2})
    assert not canary._canary_in_bundle(_bundle(), {"act"}, {2})


def test_bundle_inspection_passes_act_id_lists_that_name_no_canary_act():
    row = canonical_bytes({"act_ids": ["other"], "act_keys": ["act"]}) + b"\n"
    assert not canary._canary_in_bundle(_bundle({"coniector.jsonl": row}), {"act"}, {2})
    sources = canonical_bytes({"reconstructions": [["other"], ["another"]]})
    assert not canary._canary_in_bundle(_bundle({"sources.json": sources}), {"act"}, {2})


def test_bundle_inspection_reads_database_identity_in_memory():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            "CREATE TABLE acts (act_id TEXT, source_regions_json TEXT); "
            "CREATE TABLE act_search (act_id TEXT)"
        )
        connection.execute("INSERT INTO acts VALUES (?, ?)", ("act", "[]"))
        data = connection.serialize()
    finally:
        connection.close()
    assert canary._canary_in_bundle(_bundle({"acts.sqlite": data}), {"act"}, {2})


def test_healthy_canary_is_silent_and_dai_failure_names_training_page(monkeypatch, tmp_path):
    reference_text = "the synthetic record has enough distinct ink to compare"
    reference = {"acts": [{"physical_act_id": "gold-act", "text": reference_text}]}
    monkeypatch.setattr(canary, "_references", lambda _root: {"a" * 64: reference})
    monkeypatch.setattr(canary, "load_exemplar_page_shas", lambda _tree: {2: "a" * 64})
    monkeypatch.setattr(
        canary, "load_pipeline_reading_acts", lambda _tree: [{"page_sha256": "a" * 64}]
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
        "page_witness_index",
        lambda *_args, **_kwargs: {2: {chair: {"chair": chair} for chair in canary.CHAIRS}},
    )
    bad_chair = set()
    monkeypatch.setattr(
        canary,
        "witness_reading",
        lambda testimony: (
            OutputStatus.COMPLETE,
            "unrelated symbols" if testimony["chair"] in bad_chair else reference_text,
            None,
        ),
    )

    class Tree:
        run_id = "synthetic-canary"

        def __init__(self):
            self.bundle_data = _bundle()
            self.export_payload = {
                "canary": {"ordinals": [2], "acts": [{"act_id": "act", "page_ordinals": [2]}]},
                "pages": [{"ordinal": 1}],
                "delivered": [],
                "non_delivered": [],
            }
            # The Perlector's readings: (act id, page ordinal, text).
            self.readings = [("act", 2, reference_text)]

        def read_bytes(self, _path):
            return self.bundle_data

        def read_run(self):
            return {
                "sealed_config_digests": {"canary-ledger": "c" * 64},
                "source_manifest": [{"ordinal": 2, "ledger_sha256": "c" * 64}],
            }

        def build_manifest(self, stage):
            if stage == canary.PERLECTOR:
                return {
                    "artifacts": [{"kind": "stage-seal"}]
                    + [
                        {"kind": "perlectio", "subject_id": act_id, "artifact_id": act_id}
                        for act_id, _ordinal, _text in self.readings
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
                act_id, ordinal, text = next(row for row in self.readings if row[0] == artifact_id)
                return {
                    "subject_id": act_id,
                    "artifact_id": artifact_id,
                    "outcome": "read",
                    "payload": {"text": text, "page_ordinal": ordinal, "n": 1},
                }
            return {
                "payload": {
                    **self.export_payload,
                    "bundle": {
                        "reference": {"relative_path": "bundle.zip"},
                        "sha256": digest_bytes(self.bundle_data),
                    },
                }
            }

    tree = Tree()
    healthy = canary.check_run(tree, tmp_path)
    assert healthy["dead"] == []
    assert all(healthy["stages"].values())
    assert reference_text not in str(healthy)
    assert healthy["self_hash"] == self_hash(healthy)

    # A second reading on the canary page, exported as a real act and missing
    # from the block.
    tree.readings.append(("second", 2, reference_text))
    tree.export_payload["non_delivered"] = [{"act_id": "second"}]
    missing = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in missing["dead"]} >= {
        "canary-missing-from-block",
        "canary-in-real-export",
    }
    tree.readings.pop()
    tree.export_payload["non_delivered"] = []

    tree.readings.append(("real-act", 1, reference_text))
    tree.export_payload["canary"]["acts"].append({"act_id": "real-act", "page_ordinals": [1]})
    wrong_block = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in wrong_block["dead"]} >= {"canary-missing-from-block"}
    tree.export_payload["canary"]["acts"].pop()
    tree.readings.pop()

    block = tree.export_payload.pop("canary")
    absent_block = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in absent_block["dead"]} >= {"canary-missing-from-block"}
    tree.export_payload["canary"] = block

    tree.bundle_data = _bundle({"acts.jsonl": canonical_bytes({"act_id": "act"}) + b"\n"})
    escaped = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in escaped["dead"]} >= {"canary-in-bundle"}
    tree.bundle_data = _bundle()
    assert canary.check_run(tree, tmp_path)["dead"] == []

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

    for layer in ("delivered", "other_readings"):

        def leaked(stage, kind, artifact_id, layer=layer):
            record = original_artifact(stage, kind, artifact_id)
            if kind == "export":
                record["payload"][layer] = [{"act_id": "act"}]
            return record

        tree.read_artifact = leaked
        leak = canary.check_run(tree, tmp_path)
        assert not leak["stages"][canary.ARMARIUM], layer
        assert {row["rule"] for row in leak["dead"]} >= {"canary-in-real-export"}

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

    tree.build_manifest = original_manifest
    tree.readings = [("act", 2, "unrelated symbols")]
    unread = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in unread["dead"]} >= {"canary-reading-shared-too-little-ink"}
    assert not unread["stages"][canary.PERLECTOR]

    tree.readings = [("act", 2, reference_text), ("act", 2, reference_text)]
    doubled = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in doubled["dead"]} >= {"canary-reading-ambiguous"}
    assert doubled["stages"][canary.ARMARIUM], "one ambiguity is the reader's, not the export's"

    tree.readings = [("elsewhere", 1, reference_text)]
    absent = canary.check_run(tree, tmp_path)
    assert {row["rule"] for row in absent["dead"]} >= {"no-canary-reading"}


class _PageRunTree:
    """A sealed page-read run with one canary page: its feed, one held reading, its export."""

    run_id = "synthetic-page-canary"

    def __init__(self, text: str, outcome: str = "held"):
        self.text, self.outcome = text, outcome
        self.bundle_data = _bundle()

    def read_bytes(self, _path):
        return self.bundle_data

    def read_run(self):
        return {
            "sealed_config_digests": {"canary-ledger": "c" * 64},
            "source_manifest": [{"ordinal": 2, "ledger_sha256": "c" * 64}],
        }

    def build_manifest(self, stage):
        artifacts = [{"kind": "stage-seal"}]
        if stage == canary.PERLECTOR:
            artifacts += [{"kind": "perlectio", "subject_id": "act", "artifact_id": "act"}]
            artifacts += [{"kind": "page-feed", "subject_id": "page-2", "artifact_id": "f"}]
        if stage == canary.ARMARIUM:
            artifacts += [{"kind": "export", "artifact_id": "export"}]
        return {"artifacts": artifacts}

    def read_artifact(self, stage, kind, artifact_id):
        if kind == "perlectio":
            return {
                "subject_id": "act",
                "artifact_id": "act",
                "outcome": self.outcome,
                "payload": {"schema": "perlectio.v3", "text": self.text, "page_ordinal": 2, "n": 1},
            }
        return {
            "payload": {
                "canary": {"ordinals": [2], "acts": [{"act_id": "act", "page_ordinals": [2]}]},
                "pages": [{"ordinal": 1}],
                "delivered": [],
                "non_delivered": [],
                "bundle": {
                    "reference": {"relative_path": "bundle.zip"},
                    "sha256": digest_bytes(self.bundle_data),
                },
            }
        }


def _page_testimonium(text: str, *, outcome: str = "read", truncated=False) -> dict:
    return {
        "outcome": outcome,
        "payload": {
            "payload": text,
            "content_health": {"recordable": True, "truncated": truncated},
        },
    }


@pytest.fixture
def page_canary(monkeypatch):
    """The canary's sealed-tree readers, answered for one page-run canary page."""
    reference_text = "the synthetic record has enough distinct ink to compare"
    reference = {"acts": [{"physical_act_id": "gold-act", "text": reference_text}]}
    monkeypatch.setattr(canary, "_references", lambda _root: {"a" * 64: reference})
    monkeypatch.setattr(canary, "load_exemplar_page_shas", lambda _tree: {2: "a" * 64})
    monkeypatch.setattr(
        canary, "load_pipeline_reading_acts", lambda _tree: [{"page_sha256": "a" * 64}]
    )
    monkeypatch.setattr(
        canary,
        "compare_page_geometry",
        lambda *_args: {
            "matched_pairs": [{"pipeline_act_id": "act", "reference_physical_act_id": "gold-act"}]
        },
    )

    testimonia = {(chair, 2): _page_testimonium(reference_text) for chair in canary.CHAIRS}
    monkeypatch.setattr(canary, "sealed_page_bindings", lambda _tree: {})
    monkeypatch.setattr(
        canary,
        "page_witness_index",
        lambda _tree, **_kwargs: {
            2: {chair: record for (chair, _ordinal), record in testimonia.items()}
        },
    )
    return reference_text, testimonia


def test_a_page_run_canary_reads_held_entries_and_page_witnesses_without_a_false_alarm(
    page_canary, tmp_path
):
    reference_text, _testimonia = page_canary

    verdict = canary.check_run(_PageRunTree(reference_text), tmp_path)

    assert verdict["dead"] == []
    assert all(verdict["stages"].values())


def test_a_page_run_canary_still_names_a_reader_that_read_nothing(page_canary, tmp_path):
    verdict = canary.check_run(_PageRunTree("unrelated symbols"), tmp_path)

    assert {row["rule"] for row in verdict["dead"]} == {"canary-reading-shared-too-little-ink"}
    assert not verdict["stages"][canary.PERLECTOR]


def test_a_page_run_canary_names_a_reader_text_beyond_the_scoring_bounds(page_canary, tmp_path):
    verdict = canary.check_run(_PageRunTree("a" * (MAX_TEXT_LENGTH + 1)), tmp_path)

    assert verdict["dead"] == [
        {"stage": canary.PERLECTOR, "rule": "canary-reading-text-out-of-bounds"}
    ]
    assert all(verdict["stages"][chair] for chair in canary.CHAIRS)


def test_a_page_run_canary_names_a_failed_page_witness(page_canary, tmp_path):
    reference_text, testimonia = page_canary
    testimonia[("attestator_2", 2)] = _page_testimonium("unrelated symbols")
    testimonia[("attestator_1", 2)] = _page_testimonium(reference_text, truncated=True)
    del testimonia[("attestator_3", 2)]

    verdict = canary.check_run(_PageRunTree(reference_text), tmp_path)

    assert sorted((row["stage"], row["rule"]) for row in verdict["dead"]) == [
        ("attestator_1", "canary-witness-reading-failed"),
        ("attestator_2", "DAI failed on a page it was trained on"),
        ("attestator_3", "missing-canary-testimonium"),
    ]
    assert verdict["stages"][canary.PERLECTOR]


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


def test_build_copies_only_selected_synthetic_reference_pages(tmp_path, monkeypatch):
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
    original_read = Path.read_bytes
    reads = 0

    def read_once(path):
        nonlocal reads
        if path == chosen:
            reads += 1
            if reads > 1:
                raise AssertionError("selected page was read more than once")
        return original_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", read_once)
        assert canary.build(source, [sha], output) == output
    assert reads == 1
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
    chosen.rename(source / "pages" / "original.png")
    chosen.symlink_to("original.png")
    with pytest.raises(ValueError, match="symlink"):
        canary.build(source, [sha], tmp_path / "symlink-canary")


def test_build_derives_references_from_a_synthetic_recordgold_set(tmp_path):
    source = _two_page_set(tmp_path / "recordgold")
    admitted = admit_local_set(source, split="val")
    sha = admitted["reference_pages"][0]["page"]["sha256"]
    output = canary.build(source, [sha], tmp_path / "canary", split="val")
    assert len(json.loads((output / "reference-pages.json").read_text())) == 1
    assert len(list((output / "pages").iterdir())) == 1


def test_page_testimonia_are_each_chairs_current_page_reading(tmp_path):
    from common.runtree.store import RunTree

    from .test_evaluate import _orchestrate

    completed = _orchestrate(tmp_path, "page-unbroken")
    assert completed.returncode == 0, completed.stderr
    tree = RunTree(tmp_path, "r")

    testimonia = canary.page_witness_index(tree)[1]

    assert sorted(testimonia) == sorted(canary.CHAIRS)
    for record in testimonia.values():
        assert record["payload"]["page_ordinal"] == 1
        assert isinstance(record["payload"]["payload"], str)
