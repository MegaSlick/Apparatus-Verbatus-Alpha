"""Spec 10, test 5: the annotation layer, and the firewall between it and `text`.

Two levels, because nothing upstream of this stage populates `annotations` yet:
unit tests against `validate_annotations` directly, and end-to-end tests that
tamper a reviewed Perlectio into carrying some and drive the real CLI over it.

Tyrel, 2026-08-05: "many of our records are damaged," so a single act carrying
fifty gaps is ordinary material and anything that behaves acceptably at one gap
and badly at fifty is a defect. Hence the multi-gap cases below.
"""

import importlib.util
import io
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest
import reseal_chain
import stage_driver

from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, RECENSOR
from common.runtree.store import RunTree
from common.stage import EXIT_HELD

ROOT = Path(__file__).resolve().parents[2]


def _load_archetypus():
    path = Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("archetypus_run_under_test_annotations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archetypus = _load_archetypus()

REF_A = {
    "relative_path": "3_attestatores/artifacts/testimonium/art_aaaaaaaaaaaaaaaa.json",
    "sha256": "a" * 64,
}
REF_B = {
    "relative_path": "3_attestatores/artifacts/testimonium/art_bbbbbbbbbbbbbbbb.json",
    "sha256": "b" * 64,
}
# The roster a gap annotation may cite, and what each of those witnesses actually
# reported. A quoted variant must be something its witness really said.
WITNESSES = {
    (REF_A["relative_path"], REF_A["sha256"]): "old Tyrel possibly a name variant-0 v0 x",
    (REF_B["relative_path"], REF_B["sha256"]): "Sohn old variant-1 v1 x",
}


def gap(position: int, *, witness=None, variant: str = "x") -> dict:
    note = {"kind": "illegible", "start": position, "end": position, "witness_evidence": []}
    if witness is not None:
        note["witness_evidence"] = [{"witness_ref": witness, "variant": variant}]
    return note


def uncertain(start: int, end: int, *, certainty="low", alternatives=("Sohn",)) -> dict:
    return {
        "kind": "uncertain",
        "start": start,
        "end": end,
        "certainty": certainty,
        "alternatives": list(alternatives),
    }


# --- validate_annotations: bounds, closed kinds -------------------------------


def test_an_annotation_that_is_not_an_object_is_refused():
    with pytest.raises(SchemaRefusal, match="is not an object"):
        archetypus.validate_annotations(["not a dict"], "some text", WITNESSES, "annotations")


def test_an_unknown_kind_is_refused():
    note = {"kind": "speculative", "start": 0, "end": 1}
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_span_starting_before_zero_is_refused():
    with pytest.raises(SchemaRefusal, match="outside this reading's own text bounds"):
        archetypus.validate_annotations([uncertain(-1, 2)], "some text", WITNESSES, "annotations")


def test_a_span_ending_past_the_text_length_is_refused():
    with pytest.raises(SchemaRefusal, match="outside this reading's own text bounds"):
        archetypus.validate_annotations([uncertain(0, 999)], "some text", WITNESSES, "annotations")


def test_a_non_integer_start_is_refused():
    note = {"kind": "illegible", "start": 1.5, "end": 1.5, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="non-integer"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_an_absurdly_large_end_is_refused_cleanly_rather_than_crashing_on_format():
    """The magnitude check has to fire before the bounds check formats its
    message: CPython refuses to render an int of more than ~4300 digits, so an
    offset this large turns one malformed annotation into an uncaught ValueError
    that takes the whole run down rather than refusing a single act."""
    huge = 10**10000
    note = {
        "kind": "uncertain",
        "start": 0,
        "end": huge,
        "certainty": "high",
        "alternatives": ["x"],
    }
    with pytest.raises(SchemaRefusal, match="far outside any plausible text length"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_an_absurdly_large_negative_start_is_refused_cleanly_rather_than_crashing_on_format():
    huge_negative = -(10**10000)
    note = {
        "kind": "illegible",
        "start": huge_negative,
        "end": huge_negative,
        "witness_evidence": [],
    }
    with pytest.raises(SchemaRefusal, match="far outside any plausible text length"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_boolean_start_is_refused_even_though_it_is_technically_an_int():
    note = {"kind": "illegible", "start": True, "end": True, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="non-integer"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_an_unknown_top_level_field_on_a_gap_is_refused():
    note = gap(1)
    note["note"] = "extra"
    with pytest.raises(SchemaRefusal, match="outside its closed schema"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_certainty_field_on_a_gap_is_refused():
    """A gap read nothing, so it has no certainty about characters to declare."""
    note = gap(1)
    note["certainty"] = "low"
    with pytest.raises(SchemaRefusal, match="outside its closed schema"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


# --- The gap firewall: zero-width, structurally ------------------------------


def test_a_gap_with_start_not_equal_to_end_is_refused():
    note = {"kind": "illegible", "start": 2, "end": 5, "witness_evidence": []}
    with pytest.raises(SchemaRefusal, match="zero-width anchor"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_zero_width_gap_with_no_evidence_is_accepted():
    """Every witness may have found the same damage; that is ordinary."""
    validated = archetypus.validate_annotations([gap(3)], "some text", WITNESSES, "annotations")
    assert validated == [{"kind": "illegible", "start": 3, "end": 3, "witness_evidence": []}]


def test_a_gap_with_the_witness_evidence_field_absent_is_accepted():
    note = {"kind": "illegible", "start": 3, "end": 3}
    validated = archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")
    assert validated[0]["witness_evidence"] == []


def test_gap_evidence_must_cite_one_of_this_acts_own_witnesses():
    stranger = {
        "relative_path": "3_attestatores/artifacts/testimonium/art_cccccccccccccccc.json",
        "sha256": "c" * 64,
    }
    note = gap(2, witness=stranger, variant="Tyrel")
    with pytest.raises(SchemaRefusal, match="not one of this act's own witnesses"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_gap_evidence_requires_a_non_empty_variant():
    note = gap(2, witness=REF_A, variant="")
    with pytest.raises(SchemaRefusal, match="names no variant reading"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_variant_no_witness_ever_reported_is_refused():
    """A quoted variant that is neither the ink nor something its cited witness
    actually said is a reconstruction, and Tyrel ruled on 2026-08-05 that the
    record carries none of those."""
    note = gap(2, witness=REF_A, variant="INVENTED")
    with pytest.raises(SchemaRefusal, match="never reported"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_variant_attributed_to_the_wrong_witness_is_refused():
    """`Sohn` really was reported -- by the other witness. Attribution is checked
    against the witness actually named, not against the roster as a pool."""
    note = gap(2, witness=REF_A, variant="Sohn")
    with pytest.raises(SchemaRefusal, match="never reported"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_variant_from_a_witness_that_reported_nothing_is_refused():
    dead = {
        "relative_path": "3_attestatores/artifacts/testimonium/art_dddddddddddddddd.json",
        "sha256": "d" * 64,
    }
    witnesses = dict(WITNESSES)
    witnesses[(dead["relative_path"], dead["sha256"])] = None
    note = gap(2, witness=dead, variant="anything")
    with pytest.raises(SchemaRefusal, match="never reported"):
        archetypus.validate_annotations([note], "some text", witnesses, "annotations")


def test_gap_evidence_has_a_closed_two_field_schema():
    note = gap(2, witness=REF_A, variant="Tyrel")
    note["witness_evidence"][0]["candidate_text"] = "Tyrel"
    with pytest.raises(SchemaRefusal, match="is not exactly"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_the_same_witness_claim_twice_is_refused():
    note = gap(2, witness=REF_A, variant="Tyrel")
    note["witness_evidence"].append({"witness_ref": REF_A, "variant": "Tyrel"})
    with pytest.raises(SchemaRefusal, match="repeats the same witness claim"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_two_witnesses_disagreeing_at_one_gap_are_both_retained():
    """Nothing here picks between them -- both claims travel, side by side."""
    note = gap(2, witness=REF_A, variant="old")
    note["witness_evidence"].append({"witness_ref": REF_B, "variant": "Sohn"})
    validated = archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")
    assert [item["variant"] for item in validated[0]["witness_evidence"]] == ["old", "Sohn"]


# --- uncertain spans: real characters, at least one, with alternatives -------


def test_an_uncertain_span_with_zero_width_is_refused():
    with pytest.raises(SchemaRefusal, match="must cover at least one"):
        archetypus.validate_annotations([uncertain(4, 4)], "some text", WITNESSES, "annotations")


def test_an_uncertain_span_covering_only_whitespace_is_refused():
    """Width is not a readable character, and the difference is load-bearing.

    On text that is entirely blank, `derive_text_status` finds no gap and returns
    `no_readable_text` — a positive finding that the act held no ink. A span
    accepted over that blankness would sit in the same record asserting the
    reader did read characters there and offering alternatives for them. The two
    silences Tyrel separated would then be one, inside a single sealed record.
    """
    with pytest.raises(SchemaRefusal, match="covering no readable character"):
        archetypus.validate_annotations([uncertain(0, 3)], "   ", WITNESSES, "annotations")


def test_no_readable_text_can_never_carry_an_annotation_at_all():
    """The closure, stated as one property rather than left to be re-derived.

    Only two annotation kinds exist: a gap forces `partial`, and an uncertain
    span now requires a readable character, which forces `established`. So the
    status that claims there was no ink is reachable only with an empty
    annotation list.
    """
    for text in ("", "   ", "\n\t "):
        assert archetypus.derive_text_status(text, []) == "no_readable_text"
        with pytest.raises(SchemaRefusal, match="covering no readable character"):
            archetypus.validate_annotations(
                [uncertain(0, len(text))], text, WITNESSES, "annotations"
            )
        gapped = archetypus.validate_annotations([gap(0)], text, WITNESSES, "annotations")
        assert archetypus.derive_text_status(text, gapped) == "partial"


def test_an_uncertain_span_with_no_alternatives_is_refused():
    with pytest.raises(SchemaRefusal, match="names no alternatives"):
        archetypus.validate_annotations(
            [uncertain(0, 4, alternatives=())], "some text", WITNESSES, "annotations"
        )


def test_an_uncertain_span_with_an_unknown_certainty_is_refused():
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_annotations(
            [uncertain(0, 4, certainty="0.7")], "some text", WITNESSES, "annotations"
        )


def test_an_uncertain_span_with_no_certainty_at_all_is_refused():
    note = uncertain(0, 4)
    del note["certainty"]
    with pytest.raises(SchemaRefusal, match="not one of"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


def test_a_repeated_alternative_reading_is_refused():
    with pytest.raises(SchemaRefusal, match="repeats an alternative"):
        archetypus.validate_annotations(
            [uncertain(0, 4, alternatives=("Sohn", "Sohn"))], "some text", WITNESSES, "annotations"
        )


def test_an_empty_alternative_reading_is_refused():
    with pytest.raises(SchemaRefusal, match="empty or non-string alternative"):
        archetypus.validate_annotations(
            [uncertain(0, 4, alternatives=("",))], "some text", WITNESSES, "annotations"
        )


def test_an_uncertain_span_within_bounds_with_real_alternatives_is_accepted():
    """The reader's own candidate readings for characters it did read -- not a
    witness's, because a witness attaches to a gap, where nothing was read."""
    validated = archetypus.validate_annotations(
        [uncertain(0, 4, certainty="medium", alternatives=("Sohn", "Sahn"))],
        "some text",
        WITNESSES,
        "annotations",
    )
    assert validated[0] == {
        "kind": "uncertain",
        "start": 0,
        "end": 4,
        "certainty": "medium",
        "alternatives": ["Sohn", "Sahn"],
    }


def test_an_uncertain_span_carries_no_witness_reference_field_at_all():
    note = uncertain(0, 4)
    note["witness_ref"] = REF_A
    with pytest.raises(SchemaRefusal, match="outside its closed schema"):
        archetypus.validate_annotations([note], "some text", WITNESSES, "annotations")


# --- Several gaps at once, and a real stress case ----------------------------


def test_several_gaps_at_once_all_validate_and_are_all_carried():
    text = "the ---- man ---- from ---- nowhere"
    notes = [
        gap(0),  # leading
        gap(8, witness=REF_A, variant="old"),
        gap(17),
        gap(len(text)),  # trailing
    ]
    validated = archetypus.validate_annotations(notes, text, WITNESSES, "annotations")
    assert len(validated) == 4
    assert [note["start"] for note in validated] == [0, 8, 17, len(text)]
    assert all(note["start"] == note["end"] for note in validated)


def test_a_whole_act_gap_is_representable_on_empty_text():
    """Leading, internal, trailing -- and the whole-act case, where there is no
    text at all and the ink is nonetheless known to be there."""
    validated = archetypus.validate_annotations([gap(0)], "", WITNESSES, "annotations")
    assert validated == [{"kind": "illegible", "start": 0, "end": 0, "witness_evidence": []}]
    assert archetypus.derive_text_status("", validated) == "partial"


def test_fifty_gaps_at_once_behave_no_differently_than_one():
    """A damaged page yielding a few readable words plus many gaps is a
    successful partial reading, not an occasion to give up -- so the schema
    must not degrade at scale."""
    text = "abcdefghij"
    refs = [REF_A, REF_B]
    notes = [
        gap(
            index % (len(text) + 1),
            witness=refs[index % 2],
            variant=f"variant-{index % 2}",
        )
        for index in range(50)
    ]
    validated = archetypus.validate_annotations(notes, text, WITNESSES, "annotations")
    assert len(validated) == 50
    assert all(note["kind"] == "illegible" and note["start"] == note["end"] for note in validated)
    assert archetypus.derive_text_status(text, validated) == "partial"


# --- End-to-end: a tampered Perlectio carrying annotations, through the real CLI


# The shared subprocess driver (stage_driver.py), for the same reason as the
# shared reseal chain: two private copies of the same argv drift.
def invoke_archetypus(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return stage_driver.invoke(root, run_id, scenario, "pipeline/6_archetypus/run.py")


_run_through_recensor = stage_driver.run_through_recensor


def _tamper_reading_annotations(root: Path, run_id: str, mutate) -> tuple[RunTree, str]:
    """Add `annotations` to the accepted act's reviewed Perlectio, sealed."""
    tree = RunTree(root, run_id)
    review_entry = next(
        entry
        for entry in tree.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review" and entry["outcome"] == "accepted"
    )
    review = json.loads(tree.resolve(review_entry["relative_path"]).read_text(encoding="utf-8"))
    act_id = reseal_chain.reseal_reviewed_reading(tree, review, mutate)
    return tree, act_id


def _witness_refs_of(reading_payload: dict) -> list[dict]:
    return [item["reference"] for item in reading_payload["basis"]["testimonia"]]


def invoke_armarium(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return stage_driver.invoke(root, run_id, scenario, "pipeline/7_armarium/run.py")


def test_a_partial_record_is_exportable_by_the_armarium(tmp_path):
    """The record this stage writes for a damaged act must survive its consumer.

    This is why `status` stays the literal `"established"` and does not mirror
    `text_status`: the Armarium's `verify_established_record` checks that field
    verbatim, so a record whose `status` said `partial` would be refused at
    export -- every damaged act, and damage is the common case.

    `EXIT_HELD` rather than 0 since the export became honest about damage: the
    act is still delivered and its text still leaves whole, which is what this
    test is about; the run reports `partial` beside it because one of its acts
    carries ink nobody could read. Those two are the same sentence, not a
    contradiction, and keeping them apart is the whole reason `status` and
    `text_status` are separate fields.
    """
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")

    def mutate(payload):
        payload["annotations"] = [gap(3)]

    tree, act_id = _tamper_reading_annotations(root, "r", mutate)
    assert invoke_archetypus(root, "r", "happy").returncode == 0
    record = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )
    assert record["payload"]["text_status"] == "partial"

    result = invoke_armarium(root, "r", "happy")
    assert result.returncode == EXIT_HELD, result.stderr

    export = tree.read_artifact(
        "armarium", "export", artifact_id("armarium", "export", "export", None)
    )["payload"]
    delivered = next(item for item in export["delivered"] if item["act_id"] == act_id)
    assert delivered["text"] == record["payload"]["text"]


def test_the_export_carries_a_partial_acts_damage(tmp_path):
    """The honest export shape. Live, and no longer a strict xfail.

    This was the pin on the Stage 6→7 seam: the Armarium read neither
    `text_status` nor `annotations`, so a partial act was delivered as though it
    were whole and the run still aggregated to `complete`. `strict=True` made the
    day it started passing a suite failure naming exactly what to clean up. That
    day is this change — the marker is gone, the assertions below run for real,
    and `pipeline/6_archetypus/HANDOFF.md`'s consumer-obligations section no
    longer says the Armarium ignores these fields.
    """
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")

    def mutate(payload):
        payload["annotations"] = [gap(3)]

    tree, act_id = _tamper_reading_annotations(root, "r", mutate)
    assert invoke_archetypus(root, "r", "happy").returncode == 0
    # EXIT_HELD, not 0: an act delivered with ink nobody could read is a run that
    # did not lose the act and did not read all of it either, and the terminal
    # ledger folds the aggregate's reason into its own.
    assert invoke_armarium(root, "r", "happy").returncode == EXIT_HELD

    export = tree.read_artifact(
        "armarium", "export", artifact_id("armarium", "export", "export", None)
    )["payload"]
    delivered = next(item for item in export["delivered"] if item["act_id"] == act_id)
    assert delivered["text_status"] == "partial"
    assert export["aggregate"]["status"] != "complete"


# --- The red demonstration, kept: one internal gap, honest all the way out -----


def _tamper_every_reviewed_reading(root: Path, run_id: str, mutate) -> tuple[RunTree, list[str]]:
    """Reseal every accepted act's reviewed Perlectio, not only the first.

    The audit's demonstration injected its gap into *each* Perlectio of a `happy`
    run, and that matters: a single damaged act among whole ones is the case a
    reader excuses, while a run where every act carries known-unread ink and still
    reports `complete` is the one that cannot be argued with.
    """
    tree = RunTree(root, run_id)
    act_ids: list[str] = []
    for entry in tree.build_manifest(RECENSOR)["artifacts"]:
        if entry["kind"] != "review" or entry["outcome"] != "accepted":
            continue
        review = json.loads(tree.resolve(entry["relative_path"]).read_text(encoding="utf-8"))
        act_ids.append(reseal_chain.reseal_reviewed_reading(tree, review, mutate))
    assert act_ids, "the fixture accepted no act, so this demonstration would prove nothing"
    return tree, sorted(act_ids)


def _internal_gap(payload: dict) -> None:
    """One schema-legal internal gap: ink the reader knows it did not read."""
    middle = len(payload["text"]) // 2
    assert 0 < middle < len(payload["text"]), "the fixture reading is too short to gap internally"
    payload["gaps"] = [
        {"position": "internal", "start": middle, "end": middle, "witness_evidence": []}
    ]


def _bundle_members(tree: RunTree) -> dict[str, bytes]:
    export = tree.read_artifact(
        "armarium", "export", artifact_id("armarium", "export", "export", None)
    )["payload"]
    data = tree.read_bytes(export["bundle"]["reference"]["relative_path"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _jsonl_rows(members: dict[str, bytes]) -> dict[str, dict]:
    rows = [json.loads(line) for line in members["acts.jsonl"].decode("utf-8").splitlines() if line]
    return {row["act_id"]: row for row in rows}


def _database_rows(members: dict[str, bytes], tmp_path: Path) -> dict[str, tuple]:
    """Read the packaged acts database the way a recipient with only the ZIP would.

    Stage 7's own reader is off-limits here: a stage's test file may not import a
    module owned by another stage (`pipeline/test_stage_import_boundaries.py`), so
    this opens the member with stdlib sqlite3 and asks the product itself.
    """
    path = tmp_path / "acts.sqlite"
    path.write_bytes(members["acts.sqlite"])
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT act_id, text_status, transcription_annotations_json, "
            "semantic_annotations_json, semantic_annotation_status FROM acts"
        ).fetchall()
    return {row[0]: row[1:] for row in rows}


def test_an_internal_gap_in_every_reading_leaves_the_run_visibly_partial(tmp_path):
    """The audit's red demonstration, kept as a test rather than as a memory.

    Before this change: one schema-legal internal gap injected into each Perlectio
    of a `happy` run produced `export outcome: delivered`, an aggregate of
    `{"by_category": {"delivered": 2}, "reasons": [], "status": "complete"}`, and
    a text bundle that said nothing about the damage at all. The Archetypus knew
    (`text_status: partial` on every record); nothing downstream read the field.

    After it: the status reaches the projection, every selected literal format and
    the run aggregate, which names each act and reports `partial`.

    What this deliberately does not assert is anything about the `display:`
    rendering. Whether a gap is *shown* inside a rendered reading is Tyrel's
    choice of convention (spec 11), and the manifest says so on its own face.
    Counting the damage is this seam's business; showing it is not.
    """
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")
    tree, act_ids = _tamper_every_reviewed_reading(root, "r", _internal_gap)

    assert invoke_archetypus(root, "r", "happy").returncode == 0
    for act_id in act_ids:
        record = tree.read_artifact(
            ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
        )
        assert record["payload"]["text_status"] == "partial"

    result = invoke_armarium(root, "r", "happy")
    assert result.returncode == EXIT_HELD, result.stderr

    export = tree.read_artifact(
        "armarium", "export", artifact_id("armarium", "export", "export", None)
    )["payload"]
    aggregate = export["aggregate"]
    assert aggregate["status"] == "partial"
    assert aggregate["by_category"] == {"delivered": len(act_ids)}
    # Named, not merely counted: every damaged act appears in `reasons` by key.
    keys = {item["act_key"] for item in export["delivered"]}
    assert keys, "the run delivered nothing, so there is no damaged delivery to check"
    for act_key in keys:
        assert any(
            reason.startswith(f"act {act_key} was delivered with partial text")
            for reason in aggregate["reasons"]
        ), aggregate["reasons"]
    assert all(item["text_status"] == "partial" for item in export["delivered"])
    assert export["bundle"]["claims_status"] == "partial"

    members = _bundle_members(tree)
    readable = "\n".join(
        content.decode("utf-8")
        for name, content in sorted(members.items())
        if name.startswith("text/")
    )
    assert readable.count("text_status: partial") == len(keys)
    rows = _jsonl_rows(members)
    database = _database_rows(members, tmp_path)
    for act_id in act_ids:
        assert rows[act_id]["text_status"] == "partial"
        assert rows[act_id]["uncertainty"]["gaps"], "the layer the status was derived from"
        assert database[act_id][0] == "partial"


def test_a_sealed_annotation_is_carried_out_rather_than_replaced_by_not_produced(tmp_path):
    """Sol-S4's second field failure: the layer was not dropped, it was overwritten.

    Every exported row carried `annotations: []` and `annotation_status:
    "not-produced"` — a true statement about the *semantic* annotation layer
    `annotation_boundary.py` has never built, written over an act whose Archetypus
    record sealed a real `illegible` mark. Two different things wore one word and
    the sealed one lost, so the export made a positive claim that no annotations
    were produced for an act that carried one.

    Both layers now travel under their own names, and both are asserted here: the
    transcription layer arrives intact, and the semantic claim stays exactly the
    true statement it always was about the layer it actually describes.
    """
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")

    def mutate(payload):
        payload["annotations"] = [gap(3)]

    tree, act_id = _tamper_reading_annotations(root, "r", mutate)
    assert invoke_archetypus(root, "r", "happy").returncode == 0
    record = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )
    sealed = record["payload"]["annotations"]
    assert sealed == [gap(3)]

    assert invoke_armarium(root, "r", "happy").returncode == EXIT_HELD

    export = tree.read_artifact(
        "armarium", "export", artifact_id("armarium", "export", "export", None)
    )["payload"]
    delivered = next(item for item in export["delivered"] if item["act_id"] == act_id)
    assert delivered["transcription_annotations"] == sealed

    members = _bundle_members(tree)
    row = _jsonl_rows(members)[act_id]
    assert row["transcription_annotations"] == sealed
    assert row["semantic_annotations"] == []
    assert row["semantic_annotation_status"] == "not-produced-pending-architecture-approval"
    database = _database_rows(members, tmp_path)
    _status, transcription_json, semantic_json, semantic_status = database[act_id]
    assert json.loads(transcription_json) == sealed
    assert json.loads(semantic_json) == []
    assert semantic_status == "not-produced-pending-architecture-approval"
    readable = "\n".join(
        content.decode("utf-8")
        for name, content in sorted(members.items())
        if name.startswith("text/")
    )
    assert json.dumps(sealed, ensure_ascii=False, sort_keys=True) in readable


def _reported_by(tree: RunTree, reference: dict) -> str:
    """What that witness actually said, so a test variant can be a real quotation."""
    record = json.loads(tree.resolve(reference["relative_path"]).read_text(encoding="utf-8"))
    return record["payload"]["reported"]


def test_a_damaged_act_establishes_as_partial_with_gaps_carried_whole(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")
    tree = RunTree(root, "r")

    def mutate(payload):
        witnesses = _witness_refs_of(payload)
        text = payload["text"]
        quoted = _reported_by(tree, witnesses[0])[:6]
        payload["annotations"] = [
            gap(0),
            gap(len(text) // 2, witness=witnesses[0], variant=quoted),
            gap(len(text)),
            uncertain(0, 9, certainty="low", alternatives=("SYNTHETIK",)),
        ]

    tree, act_id = _tamper_reading_annotations(root, "r", mutate)
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 0, result.stderr

    record = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )
    payload = record["payload"]
    assert payload["text_status"] == "partial"
    # The Armarium's record-level literal is untouched by a partial reading.
    assert payload["status"] == "established"
    assert payload["evidence_ref"] is None
    assert len(payload["annotations"]) == 4
    assert {note["kind"] for note in payload["annotations"]} == {"illegible", "uncertain"}
    gap_starts = sorted(
        note["start"] for note in payload["annotations"] if note["kind"] == "illegible"
    )
    assert gap_starts[0] == 0
    assert gap_starts[-1] == len(payload["text"])


def test_gap_evidence_never_leaks_into_established_text(tmp_path):
    """The firewall, proven end to end: a real witness quotation reaches
    `annotations` and never touches `text`."""
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")
    tree = RunTree(root, "r")
    original_text = None
    quoted = None

    def mutate(payload):
        nonlocal original_text, quoted
        original_text = payload["text"]
        # A witness whose words are its own -- the fixture's second chair reads
        # `gamna` where the reading has `gamma`. A quotation the reading does not
        # already contain makes its presence in `text` unambiguous evidence of a
        # leak rather than a coincidence.
        chosen = None
        for reference in _witness_refs_of(payload):
            reported = _reported_by(tree, reference)
            for length in range(5, min(len(reported), 20) + 1):
                candidate = reported[-length:]
                if candidate not in original_text:
                    chosen, quoted = reference, candidate
                    break
            if chosen is not None:
                break
        assert chosen is not None, "no witness in this fixture departs from the reading"
        payload["annotations"] = [gap(2, witness=chosen, variant=quoted)]

    tree, act_id = _tamper_reading_annotations(root, "r", mutate)
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 0, result.stderr

    record = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )
    payload = record["payload"]
    assert payload["text"] == original_text
    assert quoted not in payload["text"]
    assert quoted in json.dumps(payload["annotations"])


def test_fifty_gaps_establish_cleanly_through_the_real_cli(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")
    tree = RunTree(root, "r")

    def mutate(payload):
        witnesses = _witness_refs_of(payload)
        length = len(payload["text"])
        payload["annotations"] = [
            gap(
                index % (length + 1),
                witness=witnesses[index % len(witnesses)],
                variant=_reported_by(tree, witnesses[index % len(witnesses)])[:5],
            )
            for index in range(50)
        ]

    tree, act_id = _tamper_reading_annotations(root, "r", mutate)
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 0, result.stderr

    record = tree.read_artifact(
        ARCHETYPUS, "archetypus", artifact_id(ARCHETYPUS, "archetypus", act_id)
    )
    payload = record["payload"]
    assert payload["text_status"] == "partial"
    assert len(payload["annotations"]) == 50


def test_a_non_zero_width_gap_is_refused_through_the_real_cli(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")

    def mutate(payload):
        payload["annotations"] = [
            {"kind": "illegible", "start": 1, "end": 5, "witness_evidence": []}
        ]

    _tamper_reading_annotations(root, "r", mutate)
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "zero-width anchor" in result.stderr


def test_evidence_citing_a_stranger_witness_is_refused_through_the_real_cli(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")
    stranger = {
        "relative_path": "3_attestatores/artifacts/testimonium/art_eeeeeeeeeeeeeeee.json",
        "sha256": "e" * 64,
    }

    def mutate(payload):
        payload["annotations"] = [gap(1, witness=stranger, variant="x")]

    _tamper_reading_annotations(root, "r", mutate)
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "not one of this act's own witnesses" in result.stderr


def test_an_invented_variant_is_refused_through_the_real_cli(tmp_path):
    root = tmp_path / "runs"
    _run_through_recensor(root, "r")

    def mutate(payload):
        witnesses = _witness_refs_of(payload)
        payload["annotations"] = [gap(1, witness=witnesses[0], variant="NOBODY_EVER_SAID_THIS")]

    _tamper_reading_annotations(root, "r", mutate)
    result = invoke_archetypus(root, "r", "happy")
    assert result.returncode == 2, result.stderr
    assert "Traceback" not in result.stderr
    assert "never reported" in result.stderr


# --- Render -> strip -> hash round-trip: a schema-sufficiency demonstration --
#
# Spec 10 test 4's second half. No stage builds real display rendering yet --
# that is the Armarium's future business at export time -- so this is a
# test-only helper proving the schema this stage writes is *sufficient* to
# support that round-trip once built, not a shipped rendering feature.


def _demo_render(text: str, annotations: list[dict]) -> str:
    """A minimal Leiden-style bracket rendering, for this test only."""
    rendered = []
    cursor = 0
    for note in sorted(annotations, key=lambda item: item["start"]):
        if note["kind"] != "illegible":
            continue
        rendered.append(text[cursor : note["start"]])
        rendered.append("⟨illegible⟩")
        cursor = note["start"]
    rendered.append(text[cursor:])
    return "".join(rendered)


def _demo_strip(rendered: str) -> str:
    return rendered.replace("⟨illegible⟩", "")


def test_render_strip_hash_round_trip_reproduces_the_canonical_text_hash():
    from common.contracts.canonical import digest_of

    text = "the man from nowhere"
    annotations = [gap(0), gap(7), gap(len(text))]
    validated = archetypus.validate_annotations(annotations, text, {}, "annotations")
    rendered = _demo_render(text, validated)
    assert rendered != text  # the display really does differ from the clean text
    stripped = _demo_strip(rendered)
    assert stripped == text
    # Against a pinned digest, not digest_of(text): once stripped == text
    # holds, comparing two calls of the same function proves nothing. The
    # constant is what a sealed record's text_hash would hold for this text,
    # so this is the recomputation a real consumer performs.
    assert digest_of(stripped) == "67173165481aa850b657885cbee282a56bcc4ff006b49aee5e266b94b4eaa035"
