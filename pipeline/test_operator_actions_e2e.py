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
from io import BytesIO
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
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ARMARIUM
from common.review_decisions import READING_HELD
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE, EXIT_HELD
from conftest import load_stage

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
    answer["acts"][1]["cites"] = ["A1", "B1", "C1", "A2", "B2", "C2"]
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

