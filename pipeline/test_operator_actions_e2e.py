"""An operator override sends a reading its own Perlectio holds to export, labelled.

The tree is the live reading seam's (`pipeline/test_live_reading_seam_e2e.py`),
with page 1's second entry citing the first entry's ids as well as its own.
The page accounting then holds both of page 1's entries on their own reading
(`duplicate-region`, a unit hold of each Perlectio) and on their page
(`merged-detection`, a page hold), and the Recensor holds them; p2:1 is
accepted. A person releases both units and finds no missed act on page 1;
the Archetypus establishes both readings exactly as read and the Armarium
delivers them, labelled "released by operator" in every format the package
carries, with who decided, when, and the hold codes overridden.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from test_live_reading_seam_e2e import (  # noqa: F401  (`designated` is a fixture)
    PAGE_ANSWERS,
    TIER,
    PageReaderWorld,
    designated,
    perlector,
    read_by_live_witnesses,
    run_in_process,
)
from test_review_decisions_e2e import (
    RUN_ID,
    TIMESTAMP,
    _after_recensor,
    _bundle,
    _copy,
    _decide,
    _decisions,
    _recense,
    _reviews,
    _run,
)

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ApprovalRefusal, SchemaRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM
from common.contracts.uncertainty import corrected_layer
from common.review_decisions import READING_HELD
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_HELD
from conftest import load_stage, rewitness_stage_boundary

DUPLICATE = "duplicate-region"
MERGED = "merged-detection"
READING_CODES = [DUPLICATE, MERGED]
armarium_export = load_stage("7_armarium", "armarium_export", isolate_path=True)
TEXTS = {
    "p1:1": "SYNTHETIC ACT ONE alpha beta gamma",
    "p1:2": "SYNTHETIC ACT TWO delta epsilon zeta eta",
}


@pytest.fixture(scope="module")
def reading_held(designated, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """The tree through the Recensor's first pass, page 1's entries held by their own readings."""
    work = tmp_path_factory.mktemp("reading-held")
    root = work / "runs"
    shutil.copytree(designated.run_root, root)
    read_by_live_witnesses(designated, root, work / "witnesses")
    answer = json.loads(PAGE_ANSWERS[1])
    answer["entries"][1]["cites"] = ["A1", "B1", "C1", "A2", "B2", "C2"]
    reader = PageReaderWorld(
        designated.catalogue, work / "reader", {1: json.dumps(answer), 2: PAGE_ANSWERS[2]}
    )
    assert (
        run_in_process(
            perlector,
            root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
        == EXIT_COMPLETE
    )
    tree = SimpleNamespace(root=root, catalogue=designated.catalogue)
    assert _run(tree, "pipeline/5_recensor/run.py").returncode == EXIT_HELD
    reviews = _reviews(root)
    for key in TEXTS:
        assert (reviews[key]["outcome"], reviews[key]["payload"]["hold_codes"]) == (
            "held-for-review",
            READING_CODES,
        )
    assert reviews["p2:1"]["outcome"] == "accepted"
    return tree


def _members(root) -> dict[str, bytes]:
    tree = RunTree(root, RUN_ID)
    [export] = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _override(tree) -> dict[str, str]:
    return {
        "p1:1": _decide(tree.root, "p1:1", "release"),
        "p1:2": _decide(tree.root, "p1:2", "release"),
        "p1": _decide(tree.root, "p1", "no-missed-act"),
    }


def test_an_override_exports_a_reading_its_perlectio_held_labelled_in_every_format(
    reading_held, tmp_path
):
    tree = _copy(reading_held, tmp_path)
    paths = _override(tree)

    assert _recense(tree) == EXIT_COMPLETE
    reviews = _reviews(tree.root)
    for key in TEXTS:
        review = reviews[key]
        assert (review["outcome"], review["payload"]["hold_codes"]) == ("accepted", [])
        # The duplicated region holds the entry and its page alike.
        assert review["payload"]["operator_review"]["cleared"] == {
            "unit": [DUPLICATE],
            "page": READING_CODES,
        }
        assert READING_HELD not in review["payload"]["operator_review"]["added"]

    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    for key, text in TEXTS.items():
        act = bundle["acts"][key]
        # The model's reading exactly as read: nothing is invented or corrected.
        assert (act["category"], act["canonical_clean_text"]) == ("delivered", text)
        assert key in bundle["established"]

    members = _members(tree.root)
    sources = json.loads(members["sources.json"])["operator_actions"]
    jsonl = [json.loads(line) for line in members["operator.jsonl"].decode().splitlines()]
    assert jsonl == sources
    assert sorted(row["act_key"] for row in sources) == sorted(TEXTS)
    for row in sources:
        assert row["label"] == "released by operator"
        assert row["reading_hold_codes"] == READING_CODES
        assert row["cleared_codes"] == READING_CODES
        decided = {(d["scope"], d["decision"]): d for d in row["decisions"]}
        assert set(decided) == {("unit", "release"), ("page", "no-missed-act")}
        for decision in row["decisions"]:
            assert (decision["approver"], decision["timestamp"]) == ("project-lead", TIMESTAMP)
        assert (
            decided[("unit", "release")]["approval_ref"]["run_relative_path"]
            == paths[row["act_key"]]
        )
        assert (
            decided[("page", "no-missed-act")]["approval_ref"]["run_relative_path"] == paths["p1"]
        )
    text = "".join(member.decode() for name, member in members.items() if name.startswith("text/"))
    for row in sources:
        assert (
            f"operator_label: released by operator: no-missed-act by project-lead at {TIMESTAMP}"
            f" ({paths['p1']}); release by project-lead at {TIMESTAMP}"
            f" ({paths[row['act_key']]}); cleared {DUPLICATE}, {MERGED}"
        ) in text
    # A person's decision is a reason, never a machine check: the run stays partial.
    assert bundle["manifest"]["aggregate"]["status"] == "partial"


def test_a_release_without_its_page_decision_keeps_the_reading_held(reading_held, tmp_path):
    """The page hold is the page's to clear: a unit release alone overrides nothing."""
    tree = _copy(reading_held, tmp_path)
    _decide(tree.root, "p1:1", "release")

    assert _recense(tree) == EXIT_HELD
    review = _reviews(tree.root)["p1:1"]
    assert review["outcome"] == "held-for-review"
    assert review["payload"]["hold_codes"] == READING_CODES
    assert _decisions(tree.root)["clearances"][0]["cleared"] == [DUPLICATE]


def _drop_jsonl_row(members: dict) -> None:
    rows = members["operator.jsonl"].decode().splitlines()
    members["operator.jsonl"] = ("\n".join(rows[1:]) + "\n").encode()


def _drop_every_label(members: dict) -> None:
    """Remove the layer from every place it is written, leaving the delivered readings."""
    sources = json.loads(members["sources.json"])
    del sources["operator_actions"]
    members["sources.json"] = canonical_bytes(sources)
    del members["operator.jsonl"]
    for name in [name for name in members if name.startswith("text/")]:
        lines = members[name].decode().split("\n")
        kept, skip = [], 0
        for line in lines:
            if line.startswith("operator_label: "):
                skip = 3
            if skip:
                skip -= 1
                continue
            kept.append(line)
        members[name] = "\n".join(kept).encode()


@pytest.mark.parametrize(
    "forge, refusal",
    [
        (_drop_jsonl_row, "operator.jsonl shows other operator rows"),
        (_drop_every_label, "held by their page accounting yet delivered a reading"),
    ],
    ids=["dropped-from-one-format", "dropped-everywhere"],
)
def test_a_dropped_operator_label_is_refused_by_the_verifier(
    reading_held, tmp_path, forge, refusal
):
    """The label is part of the package: one that loses it does not verify."""
    tree = _copy(reading_held, tmp_path)
    _override(tree)
    assert _recense(tree) == EXIT_COMPLETE
    _after_recensor(tree)
    members = _members(tree.root)
    forge(members)
    with pytest.raises(SchemaRefusal, match=refusal):
        armarium_export.verify_export_bundle(_repacked(members), tmp_path / "forged")


def _repacked(members: dict) -> bytes:
    manifest = json.loads(members[armarium_export.EXPORT_MANIFEST_NAME])
    manifest["members"] = [row for row in manifest["members"] if row["path"] in members]
    for row in manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    manifest["self_hash"] = self_hash(
        {key: value for key, value in manifest.items() if key != "self_hash"}
    )
    members[armarium_export.EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)
    return armarium_export._zip_bytes(members)


def test_the_decide_command_records_an_override_the_recensor_applies(
    reading_held, tmp_path, monkeypatch
):
    """`verbatus decide` binds each release to the review the person read, as the stage does."""
    from operations.operator import cli

    tree = _copy(reading_held, tmp_path)
    monkeypatch.setattr(cli, "_typed_decide_confirmation", lambda phrase: phrase)
    for words in (
        ("release", "--unit", "p1:1"),
        ("release", "--unit", "p1:2"),
        ("no-missed-act", "--page", "1"),
    ):
        assert (
            cli.main(
                [
                    "--workspace",
                    str(tmp_path),
                    "--state-dir",
                    str(tmp_path / "state"),
                    "decide",
                    "--run-root",
                    str(tree.root),
                    "--run-id",
                    RUN_ID,
                    *words,
                    "--reason",
                    "the duplicated region is one act read twice",
                ]
            )
            == 0
        )

    assert _recense(tree) == EXIT_COMPLETE
    assert len(_decisions(tree.root)["applied"]) == 3
    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    assert {key: bundle["acts"][key]["canonical_clean_text"] for key in TEXTS} == TEXTS


# --- a person's correction ------------------------------------------------------------

EDITED = "SYNTHETIC ACT ONE alpha beta gamma, as the person reads the ink"
NOTE = "the last word is gamma in the margin"


def _correct(tree) -> dict[str, str]:
    """p1:1 corrected by a person, p1:2 released, and no missed act on page 1."""
    return {
        "p1:1": _decide(tree.root, "p1:1", "edit", text=EDITED, note=NOTE),
        "p1:2": _decide(tree.root, "p1:2", "release"),
        "p1": _decide(tree.root, "p1", "no-missed-act"),
    }


def _archetypus(root, key: str) -> dict:
    tree = RunTree(root, RUN_ID)
    return next(
        record
        for record in (
            tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
            for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
            if entry["kind"] == "archetypus"
        )
        if record["payload"]["act_key"] == key
    )


@pytest.fixture(scope="module")
def corrected(reading_held, tmp_path_factory) -> SimpleNamespace:
    """The tree through the Armarium, p1:1 corrected by a person."""
    tree = _copy(reading_held, tmp_path_factory.mktemp("corrected"))
    paths = _correct(tree)
    assert _recense(tree) == EXIT_COMPLETE
    _after_recensor(tree)
    return SimpleNamespace(root=tree.root, paths=paths, members=_members(tree.root))


def test_an_edit_is_established_as_the_reading_with_the_persons_provenance(corrected):
    record = _archetypus(corrected.root, "p1:1")
    payload = record["payload"]
    assert (payload["status"], payload["text"]) == ("established", EDITED)
    assert payload["uncertainty"] == corrected_layer()
    provenance = payload["provenance"]
    assert provenance["label"] == "corrected by a person"
    assert provenance["note"] == NOTE
    [decision] = provenance["decisions"]
    assert (decision["approver"], decision["timestamp"]) == ("project-lead", TIMESTAMP)
    assert decision["approval_ref"]["relative_path"] == corrected.paths["p1:1"]
    model = provenance["model_reading"]
    assert model["label"] == "model reading (original)"
    # The model's reading is named, not changed: the record points at it as read.
    assert model["perlectio_ref"] == payload["perlectio_ref"]
    assert model["text_sha256"] == digest_bytes(TEXTS["p1:1"].encode("utf-8"))
    assert decision["approval_ref"] in record["inputs"]
    # The released entry is still the model's reading exactly as read.
    assert _archetypus(corrected.root, "p1:2")["payload"]["text"] == TEXTS["p1:2"]


def test_an_edit_exports_the_persons_text_labelled_with_the_original_beside_it(corrected):
    members = corrected.members
    acts = {
        row["act_key"]: row for row in map(json.loads, members["acts.jsonl"].decode().splitlines())
    }
    act = acts["p1:1"]
    assert (act["category"], act["canonical_clean_text"]) == ("delivered", EDITED)
    assert act["uncertainty"] == corrected_layer()
    assert act["provenance"]["label"] == "corrected by a person"
    assert act["provenance"]["note"] == NOTE
    rows = {row["act_key"]: row for row in json.loads(members["sources.json"])["operator_actions"]}
    row = rows["p1:1"]
    assert (row["label"], row["note"]) == ("corrected by a person", NOTE)
    assert row["model_reading"]["label"] == "model reading (original)"
    assert {(d["scope"], d["decision"]) for d in row["decisions"]} == {
        ("unit", "edit"),
        ("page", "no-missed-act"),
    }
    assert rows["p1:2"]["label"] == "released by operator"
    [original] = [
        json.loads(line) for line in members["model_readings.jsonl"].decode().splitlines()
    ]
    # Every package carries it, whatever formats it selects.
    assert json.loads(members["sources.json"])["model_readings"] == [original]
    assert (original["act_key"], original["label"], original["text"]) == (
        "p1:1",
        "model reading (original)",
        TEXTS["p1:1"],
    )
    assert original["uncertainty"]["lectio_kind"] == "page-read"
    text = "".join(member.decode() for name, member in members.items() if name.startswith("text/"))
    assert f"canonical_clean_text:\n{json.dumps(EDITED)}" in text
    assert (
        f"operator_label: corrected by a person: no-missed-act by project-lead at {TIMESTAMP}"
        f" ({corrected.paths['p1']}); edit by project-lead at {TIMESTAMP}"
        f" ({corrected.paths['p1:1']})"
    ) in text
    assert f"operator_note:\n{json.dumps(NOTE)}" in text
    assert (
        f"model_reading_label: model reading (original)\nmodel_reading_text:\n"
        f"{json.dumps(TEXTS['p1:1'])}"
    ) in text


def test_an_edit_is_delivered_and_counted_and_is_no_reason(corrected, reading_held, tmp_path):
    """The edited entry counts as delivered; only the release and the page decision are reasons."""
    manifest = armarium_export.verify_delivered_bundle(
        _repacked(dict(corrected.members)), tmp_path / "clean"
    )
    aggregate = manifest["aggregate"]
    reasons = aggregate["reasons"]
    assert not any("p1:1" in reason for reason in reasons), reasons
    assert any("act p1:2" in reason and "(release)" in reason for reason in reasons)
    assert any(reason.startswith("page 1 was cleared") for reason in reasons)
    sources = json.loads(corrected.members["sources.json"])
    assert sources["aggregate_basis"]["review_decisions"]["corrections"] == ["p1:1"]


def test_the_reconstruction_stays_beneath_the_model_reading_and_says_so(corrected):
    members = corrected.members
    rows = {
        tuple(row["act_keys"]): row
        for row in map(json.loads, members["coniector.jsonl"].decode().splitlines())
    }
    assert rows[("p1:1",)]["made_from"] == "model reading (original)"
    assert "made_from" not in rows.get(("p1:2",), {})


def _empty_originals(members: dict) -> None:
    members["model_readings.jsonl"] = b""


def _sources(members: dict, change) -> None:
    sources = json.loads(members["sources.json"])
    change(sources)
    members["sources.json"] = canonical_bytes(sources)


def _drop_sources_original(members: dict) -> None:
    _sources(members, lambda sources: sources.pop("model_readings"))


def _alter_sources_original(members: dict) -> None:
    def alter(sources):
        [model] = sources["model_readings"]
        model["text_status"] = "partial"

    _sources(members, alter)


def _forge_note(members: dict) -> None:
    """Change the person's note wherever the package says it, but not the decision."""
    old, new = json.dumps(NOTE), json.dumps("a note nobody wrote")
    for name in list(members):
        if name.endswith((".json", ".jsonl")) or name.startswith("text/"):
            members[name] = members[name].replace(old.encode(), new.encode())
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "acts.sqlite"
        path.write_bytes(members["acts.sqlite"])
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE acts SET provenance_json = replace(provenance_json, ?, ?)", (old, new)
        )
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        members["acts.sqlite"] = path.read_bytes()


@pytest.mark.parametrize(
    "forge, refusal",
    [
        (_empty_originals, "jsonl format does not show the model reading \\(original\\)"),
        (_drop_sources_original, "sources.json does not show the model reading \\(original\\)"),
        (_alter_sources_original, "does not show p1:1's model reading \\(original\\)"),
        (
            _forge_note,
            "delivers a text for p1:1 that the edit its provenance names does not record",
        ),
    ],
    ids=["dropped-original", "dropped-sources-original", "altered-sources-original", "forged-note"],
)
def test_a_dropped_original_or_forged_edit_is_refused_by_the_verifier(
    corrected, tmp_path, forge, refusal
):
    members = dict(corrected.members)
    forge(members)
    with pytest.raises(SchemaRefusal, match=refusal):
        armarium_export.verify_export_bundle(_repacked(members), tmp_path / "forged")


def _drop_provenance_label(members: dict) -> None:
    def drop(sources):
        for citation in sources["act_citations"]:
            if citation["act_key"] == "p1:1":
                citation["provenance"] = citation["provenance"]["model_reading"]["provenance"]

    _sources(members, drop)


def _drop_basis_correction(members: dict) -> None:
    def drop(sources):
        sources["aggregate_basis"]["review_decisions"]["corrections"] = []

    _sources(members, drop)


def _text_no_edit_names(members: dict) -> None:
    """The delivered text must be the one the edit records: the decision is rebuilt from it."""
    rows = [json.loads(line) for line in members["acts.jsonl"].decode().splitlines()]
    for row in rows:
        if row["act_key"] == "p1:1":
            row["canonical_clean_text"] = EDITED + " and more"
            row["canonical_text_sha256"] = armarium_export.canonical_text_sha256(
                row["canonical_clean_text"]
            )
    members["acts.jsonl"] = "".join(json.dumps(row) + "\n" for row in rows).encode()


@pytest.mark.parametrize(
    "forge, refusal",
    [
        # Changed in sources.json alone, these disagree first with the acts database
        # and the sealed aggregate; `_verify_corrections` is held to them directly below.
        (_drop_provenance_label, "does not retain exact delivered provenance"),
        (_drop_basis_correction, "aggregate basis disagrees with its source accounting"),
        (_text_no_edit_names, "that the edit its provenance names does not record"),
    ],
    ids=["dropped-label", "dropped-from-basis", "text-no-edit-names"],
)
def test_a_correction_the_package_misstates_is_refused_by_the_verifier(
    corrected, tmp_path, forge, refusal
):
    members = dict(corrected.members)
    forge(members)
    with pytest.raises(SchemaRefusal, match=refusal):
        armarium_export.verify_export_bundle(_repacked(members), tmp_path / "forged")


def _verify_corrections_directly(corrected, tmp_path, forge) -> None:
    """`_verify_corrections` over a verified package whose sources.json `forge` changes.

    A forgery in sources.json alone is caught by the full verifier's earlier
    cross-format checks (above); this holds the correction check itself to it.
    """
    clean = tmp_path / "clean"
    manifest = armarium_export.verify_export_bundle(_repacked(dict(corrected.members)), clean)
    path = clean / "sources.json"
    members = {"sources.json": path.read_bytes()}
    forge(members)
    path.write_bytes(members["sources.json"])
    sources = armarium_export._load_sources(clean)
    armarium_export._verify_corrections(
        clean,
        armarium_export._manifest_formats(manifest),
        sources,
        armarium_export._operator_rows(sources, manifest),
    )


@pytest.mark.parametrize(
    "forge, refusal",
    [
        (_drop_provenance_label, "disagree about whether a person corrected it"),
        (_drop_basis_correction, "does not name exactly the readings"),
    ],
    ids=["dropped-label", "dropped-from-basis"],
)
def test_the_correction_check_itself_refuses_a_misstated_correction(
    corrected, tmp_path, forge, refusal
):
    with pytest.raises(SchemaRefusal, match=refusal):
        _verify_corrections_directly(corrected, tmp_path, forge)


def test_the_decide_command_records_an_edit_bound_to_its_text(reading_held, tmp_path, monkeypatch):
    """`verbatus decide edit` reads the text from a file and names its digest, and the note's
    or "no note", in the confirmation."""
    from operations.operator import cli

    tree = _copy(reading_held, tmp_path)
    text_file = tmp_path / "p1-1.txt"
    text_file.write_text(EDITED, encoding="utf-8")
    phrases = []

    def confirm(phrase):
        phrases.append(phrase)
        return phrase

    monkeypatch.setattr(cli, "_typed_decide_confirmation", confirm)
    common = ["--workspace", str(tmp_path), "--state-dir", str(tmp_path / "state"), "decide"]
    where = ["--run-root", str(tree.root), "--run-id", RUN_ID]
    reason = ["--reason", "the ink reads gamma"]
    assert (
        cli.main(
            [
                *common,
                *where,
                "edit",
                "--unit",
                "p1:1",
                "--text-file",
                str(text_file),
                "--note",
                NOTE,
                *reason,
            ]
        )
        == 0
    )
    assert digest_bytes(EDITED.encode("utf-8")) in phrases[0]
    # The note is confirmed by its digest, and an edit with none says so.
    assert f"and note {digest_bytes(NOTE.encode('utf-8'))}" in phrases[0]
    unchanged = tmp_path / "p1-2.txt"
    unchanged.write_text(TEXTS["p1:2"], encoding="utf-8")
    edit = ("edit", "--unit", "p1:2", "--text-file", str(unchanged))
    assert cli.main([*common, *where, *edit, *reason]) == 0
    assert "and no note" in phrases[1]
    assert cli.main([*common, *where, "no-missed-act", "--page", "1", *reason]) == 0

    assert _recense(tree) == EXIT_COMPLETE
    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    assert bundle["acts"]["p1:1"]["canonical_clean_text"] == EDITED
    assert bundle["acts"]["p1:2"]["canonical_clean_text"] == TEXTS["p1:2"]


@pytest.fixture(scope="module")
def unreadable(designated, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """The tree through the Recensor's first pass, p1:2 read with no text at all."""
    work = tmp_path_factory.mktemp("no-readable-text")
    root = work / "runs"
    shutil.copytree(designated.run_root, root)
    read_by_live_witnesses(designated, root, work / "witnesses")
    answer = json.loads(PAGE_ANSWERS[1])
    answer["entries"][1]["text"] = ""
    reader = PageReaderWorld(
        designated.catalogue, work / "reader", {1: json.dumps(answer), 2: PAGE_ANSWERS[2]}
    )
    assert (
        run_in_process(
            perlector,
            root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
        == EXIT_COMPLETE
    )
    tree = SimpleNamespace(root=root, catalogue=designated.catalogue)
    assert _run(tree, "pipeline/5_recensor/run.py").returncode == EXIT_HELD
    review = _reviews(root)["p1:2"]
    assert review["outcome"] == "held-for-review"
    assert "entry-no-readable-text" in review["payload"]["hold_codes"]
    return tree


def test_a_reading_with_no_text_cannot_be_released_but_can_be_corrected(unreadable, tmp_path):
    """The export cannot carry an empty model reading, but it can carry a person's text."""
    from operations.operator import decide

    tree = _copy(unreadable, tmp_path)
    with pytest.raises(ApprovalRefusal, match="releasing p1:2 cannot send it to export"):
        decide.prepare_decision(
            RunTree(tree.root, RUN_ID), decision="release", unit="p1:2", reason="looks fine"
        )
    prepared = decide.prepare_decision(
        RunTree(tree.root, RUN_ID),
        decision="edit",
        unit="p1:2",
        reason="the ink is faint but legible",
        text=EDITED,
        timestamp=TIMESTAMP,
    )
    decide.record_decision(RunTree(tree.root, RUN_ID), prepared)
    # The page's own hold is the page's to clear.
    assert _reviews(tree.root)["p1:2"]["payload"]["hold_codes"] == [
        "entry-no-readable-text",
        "witness-text-not-read",
    ]
    _decide(tree.root, "p1", "no-missed-act")

    assert _recense(tree) == EXIT_COMPLETE
    review = _reviews(tree.root)["p1:2"]
    assert (review["outcome"], review["payload"]["hold_codes"]) == ("accepted", [])
    assert _decisions(tree.root)["corrections"][0]["cleared"] == ["entry-no-readable-text"]
    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    act = bundle["acts"]["p1:2"]
    assert (act["category"], act["canonical_clean_text"]) == ("delivered", EDITED)
    [original] = [
        json.loads(line)
        for line in _members(tree.root)["model_readings.jsonl"].decode().splitlines()
    ]
    assert (original["act_key"], original["text"]) == ("p1:2", "")


def test_an_edit_its_page_still_holds_is_no_correction_and_an_advance_exports(
    reading_held, tmp_path
):
    """The page hold stays, so the edit corrects nothing yet; an advance exports with it held."""
    tree = _copy(reading_held, tmp_path)
    _decide(tree.root, "p1:1", "edit", text=EDITED, note=NOTE)
    assert _recense(tree) == EXIT_HELD
    assert _decisions(tree.root)["corrections"] == []
    _after_recensor(tree)
    members = _members(tree.root)
    sources = json.loads(members["sources.json"])
    assert sources["aggregate_basis"]["review_decisions"]["corrections"] == []
    acts = {
        row["act_key"]: row for row in map(json.loads, members["acts.jsonl"].decode().splitlines())
    }
    assert acts["p1:1"]["category"] != "delivered"
    armarium_export.verify_export_bundle(_repacked(dict(members)), tmp_path / "clean")


# --- a resealed Archetypus that is not the person's correction -------------------------


@pytest.fixture(scope="module")
def corrected_established(reading_held, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """p1:1 corrected and established by the Archetypus; nothing after it has run."""
    tree = _copy(reading_held, tmp_path_factory.mktemp("corrected-established"))
    _correct(tree)
    assert _recense(tree) == EXIT_COMPLETE
    assert _run(tree, "pipeline/6_archetypus/run.py").returncode == EXIT_COMPLETE
    return tree


def _forge_archetypus(root, change) -> None:
    """Rewrite p1:1's record, reseal it and rewitness the Archetypus, as an honest
    producer of the forged record would."""
    tree = RunTree(root, RUN_ID)
    [entry] = [
        entry
        for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]
        if entry["kind"] == "archetypus"
        and tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])["payload"]["act_key"]
        == "p1:1"
    ]
    path = tree.resolve(entry["relative_path"])
    record = json.loads(path.read_text(encoding="utf-8"))
    change(record)
    record["payload"]["self_hash"] = self_hash(record["payload"])
    record["self_hash"] = self_hash(record)
    path.write_bytes(canonical_bytes(record))
    rewitness_stage_boundary(tree, ARCHETYPUS)


def _other_text(record):
    record["payload"]["text"] = EDITED + " and more"


def _model_layer(record):
    record["payload"]["uncertainty"] = {**corrected_layer(), "lectio_kind": "page-read"}


def _other_note(record):
    record["payload"]["provenance"]["note"] = "a note nobody wrote"


def _no_approval_input(record):
    record["inputs"] = [
        ref for ref in record["inputs"] if not ref["relative_path"].startswith("receipts/")
    ]


@pytest.mark.parametrize(
    "forge",
    [_other_text, _model_layer, _other_note, _no_approval_input],
    ids=["text", "layer", "provenance", "inputs"],
)
def test_a_resealed_archetypus_other_than_the_stored_edit_is_refused_by_the_armarium(
    corrected_established, tmp_path, forge
):
    tree = _copy(corrected_established, tmp_path)
    _forge_archetypus(tree.root, forge)
    assert _run(tree, "pipeline/4b_coniector/run.py").returncode in (EXIT_COMPLETE, EXIT_HELD)
    result = _run(tree, "pipeline/7_armarium/run.py")
    assert result.returncode == 2, result.stderr
    assert "the Archetypus of p1:1" in result.stderr
    assert "person's correction" in result.stderr or "stored edit" in result.stderr


# --- an edit alone completes a run ---------------------------------------------------------


@pytest.fixture(scope="module")
def unit_held(designated, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """The tree through the Recensor's first pass, p1:2 held on doubt marks the reader
    could not parse. Its characters count as unread, which takes page 1 over the
    page doubt limit too."""
    return _held_on_p1_2(
        designated, tmp_path_factory, "SYNTHETIC ACT TWO delta [[]] epsilon zeta eta"
    )


@pytest.fixture(scope="module")
def doubt_unit_held(designated, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """p1:2 held only on its own reading: "zeta eta" doubtful is 7 of 34 characters, over
    the act limit, while page 1 stays under the page limit."""
    return _held_on_p1_2(
        designated, tmp_path_factory, "SYNTHETIC ACT TWO delta epsilon [[zeta eta]]"
    )


def _held_on_p1_2(designated, tmp_path_factory, text: str) -> SimpleNamespace:  # noqa: F811
    """The tree through the Recensor's first pass with p1:2 read as `text`."""
    work = tmp_path_factory.mktemp("unit-held")
    root = work / "runs"
    shutil.copytree(designated.run_root, root)
    read_by_live_witnesses(designated, root, work / "witnesses")
    # No act runs across the page break, so nothing on page 2 keeps the run partial.
    answer = json.loads(PAGE_ANSWERS[1])
    answer["entries"][1]["text"] = text
    answer["entries"][1]["continues_to_next_page"] = False
    second = json.loads(PAGE_ANSWERS[2])
    for act in second["entries"]:
        act["continues_from_previous_page"] = False
    reader = PageReaderWorld(
        designated.catalogue,
        work / "reader",
        {1: json.dumps(answer), 2: json.dumps(second)},
    )
    assert (
        run_in_process(
            perlector,
            root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
        == EXIT_COMPLETE
    )
    tree = SimpleNamespace(root=root, catalogue=designated.catalogue)
    assert _run(tree, "pipeline/5_recensor/run.py").returncode == EXIT_HELD
    assert _reviews(root)["p1:2"]["outcome"] == "held-for-review"
    return tree


def test_an_edit_of_a_reading_with_malformed_doubt_marks_is_delivered(unit_held, tmp_path):
    """Its marks cannot be released, but a person's text carries none."""
    from operations.operator import decide

    tree = _copy(unit_held, tmp_path)
    with pytest.raises(ApprovalRefusal, match="releasing p1:2 cannot send it to export"):
        decide.prepare_decision(
            RunTree(tree.root, RUN_ID), decision="release", unit="p1:2", reason="fine"
        )
    codes = _reviews(tree.root)["p1:2"]["payload"]["hold_codes"]
    assert {"doubt-marks-malformed", "page-doubt-share-high"} <= set(codes)
    _decide(tree.root, "p1:2", "edit", text=TEXTS["p1:2"])
    _decide(tree.root, "p1", "no-missed-act")
    assert _recense(tree) == EXIT_COMPLETE
    _after_recensor(tree)
    bundle = _bundle(tree.root, tmp_path / "clean")
    assert bundle["acts"]["p1:2"]["canonical_clean_text"] == TEXTS["p1:2"]
    assert bundle["acts"]["p1:2"]["uncertainty"] == corrected_layer()


def test_a_run_whose_only_decision_is_an_edit_of_a_unit_held_reading_is_complete(
    doubt_unit_held, tmp_path
):
    """A correction is no reason: with nothing else held, the aggregate is complete."""
    tree = _copy(doubt_unit_held, tmp_path)
    reviews = _reviews(tree.root)
    held = [key for key, review in reviews.items() if review["outcome"] == "held-for-review"]
    assert held == ["p1:2"], "only the edited unit is held, and only on its own reading"
    assert reviews["p1:2"]["payload"]["hold_codes"] == ["doubt-share-high"]
    _decide(tree.root, "p1:2", "edit", text=TEXTS["p1:2"])
    assert _recense(tree) == EXIT_COMPLETE
    _after_recensor(tree)
    sources = json.loads(_members(tree.root)["sources.json"])
    assert sources["aggregate_basis"]["review_decisions"]["corrections"] == ["p1:2"]
    manifest = armarium_export.verify_delivered_bundle(
        _repacked(dict(_members(tree.root))), tmp_path / "clean"
    )
    assert manifest["aggregate"]["status"] == "complete", manifest["aggregate"]["reasons"]


def test_a_persons_text_with_doubt_mark_brackets_is_delivered_as_written(reading_held, tmp_path):
    """Brackets in a person's text are the person's characters: never parsed as doubt marks,
    never refused as malformed, and delivered exactly."""
    tree = _copy(reading_held, tmp_path)
    written = "SYNTHETIC ACT ONE alpha [[beta|beda]] gamma [[?]] and ]] [["
    _decide(tree.root, "p1:1", "edit", text=written)
    _decide(tree.root, "p1:2", "release")
    _decide(tree.root, "p1", "no-missed-act")
    assert _recense(tree) == EXIT_COMPLETE
    _after_recensor(tree)
    payload = _archetypus(tree.root, "p1:1")["payload"]
    assert (payload["text"], payload["uncertainty"]) == (written, corrected_layer())
    bundle = _bundle(tree.root, tmp_path / "clean")
    assert bundle["acts"]["p1:1"]["canonical_clean_text"] == written
    assert bundle["acts"]["p1:1"]["uncertainty"] == corrected_layer()


@pytest.fixture(scope="module")
def doubt_held(designated, tmp_path_factory) -> SimpleNamespace:  # noqa: F811
    """The tree through the Recensor's first pass, page 2's one entry read wholly doubtful."""
    work = tmp_path_factory.mktemp("doubt-held")
    root = work / "runs"
    shutil.copytree(designated.run_root, root)
    read_by_live_witnesses(designated, root, work / "witnesses")
    answer = json.loads(PAGE_ANSWERS[2])
    answer["entries"][0]["text"] = "[[SYNTHETIC ACT TWO delta epsilon zeta eta]]"
    reader = PageReaderWorld(
        designated.catalogue, work / "reader", {1: PAGE_ANSWERS[1], 2: json.dumps(answer)}
    )
    assert (
        run_in_process(
            perlector,
            root,
            designated.catalogue,
            placement_tier=TIER,
            serving_factory=reader.factory,
        )
        == EXIT_COMPLETE
    )
    tree = SimpleNamespace(root=root, catalogue=designated.catalogue)
    assert _run(tree, "pipeline/5_recensor/run.py").returncode == EXIT_HELD
    return tree


def test_a_doubt_held_reading_is_delivered_only_through_a_persons_release(doubt_held, tmp_path):
    """Held by the Perlector and the Recensor on its own doubt and its page's, released by a
    person, the reading exports with its doubt layer and passes the Armarium's recount."""
    tree = _copy(doubt_held, tmp_path)
    review = _reviews(tree.root)["p2:1"]
    assert review["outcome"] == "held-for-review"
    assert {"doubt-share-high", "page-doubt-share-high"} <= set(review["payload"]["hold_codes"])

    _decide(tree.root, "p2:1", "release")
    _decide(tree.root, "p2", "no-missed-act")
    assert _recense(tree) == EXIT_COMPLETE
    assert _reviews(tree.root)["p2:1"]["outcome"] == "accepted"
    _after_recensor(tree)

    bundle = _bundle(tree.root, tmp_path / "clean")
    act = bundle["acts"]["p2:1"]
    assert (act["category"], act["canonical_clean_text"]) == ("delivered", TEXTS["p1:2"])
    assert act["uncertainty"]["uncertain_spans"]
    [counted] = [
        row
        for row in bundle["manifest"]["claims"]["doubt_share"]["acts"]
        if row["act_key"] == "p2:1"
    ]
    assert counted["doubtful_or_unread"] == counted["out_of"] == 34
