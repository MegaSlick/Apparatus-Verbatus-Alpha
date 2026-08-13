"""Spec 10, test 5: the annotation layer, and the firewall between it and `text`.

Two levels, because nothing upstream of this stage populates `annotations` yet:
unit tests against `validate_annotations` directly, and end-to-end tests that
tamper a reviewed Perlectio into carrying some and drive the real CLI over it.

Tyrel, 2026-08-05: "many of our records are damaged," so a single act carrying
fifty gaps is ordinary material and anything that behaves acceptably at one gap
and badly at fifty is a defect. Hence the multi-gap cases below.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import reseal_chain

from common.contracts.errors import SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.stages import ARCHETYPUS, RECENSOR
from common.runtree.store import RunTree

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


def invoke_archetypus(root: Path, run_id: str, scenario: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/6_archetypus/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _run_through_recensor(root: Path, run_id: str, scenario: str = "happy") -> None:
    for program in (
        "pipeline/1_exemplar/door.py",
        "pipeline/1_exemplar/run.py",
        "pipeline/2_designator/run.py",
        "pipeline/3_attestatores/run.py",
        "pipeline/4_perlector/run.py",
        "pipeline/5_recensor/run.py",
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                run_id,
                "--scenario",
                scenario,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"


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
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline/7_armarium/run.py"),
            "--run-root",
            str(root),
            "--run-id",
            run_id,
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_a_partial_record_is_exportable_by_the_frozen_armarium(tmp_path):
    """The record this stage writes for a damaged act must survive its consumer.

    This is why `status` stays the literal `"established"` and does not mirror
    `text_status`: the Armarium's `verify_established_record` checks that field
    verbatim, so a record whose `status` said `partial` would be refused at
    export -- every damaged act, and damage is the common case.

    **And a gap this test names rather than fixes.** The delivered entry carries
    no trace of the damage today, and the run still aggregates to `complete`: the
    Armarium does not read `text_status` or `annotations` at all. Making the
    export honest about a partial reading is spec 11's work, not this stage's.
    The export-side half of this test therefore ends in an xfail rather than
    assertions: the suite records the dishonest export as a known defect, so
    spec 11's fix will surface as an xpass to clean up, never as a regression.
    Stage 7 was off-limits this round.
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
    assert result.returncode == 0, result.stderr

    export = tree.read_artifact(
        "armarium", "export", artifact_id("armarium", "export", "export", None)
    )["payload"]
    delivered = next(item for item in export["delivered"] if item["act_id"] == act_id)
    assert delivered["text"] == record["payload"]["text"]
    pytest.xfail(
        "spec 11: the export drops text_status and annotations, so a partial act "
        "is delivered as if it were whole and the run still aggregates to complete"
    )


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
    assert digest_of(stripped) == digest_of(text)
