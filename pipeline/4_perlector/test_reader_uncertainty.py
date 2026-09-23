"""The reader's own doubt report, carried faithfully from the call to the export.

Independent audit of 2026-09-10, finding F2: the live reader returned text and a
stop word and nothing else, the only spans ever minted were the exhausted-cap
projection under a zero cap, and an empty `uncertain_spans` list therefore read
the same whether a reader had assessed its doubts or had no channel to. These
tests drive the real Perlector, Recensor, Archetypus and Armarium over fixture
scenarios that declare a doubt report, and assert the exact spans and the exact
text separately -- never only that producer and validator agree on a boolean.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import annotations
import audit
import pytest
import reader as reader_module
from test_prior_protocol import sampling_approval_records

from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ARCHETYPUS, ARMARIUM, PERLECTOR, RECENSOR
from common.contracts.uncertainty import from_perlectio
from common.runtree.store import RunTree
from operations.operator import review_text
from operations.operator.review import ReadOnlyRun

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"

A1_TEXT = "SYNTHETIC ACT ONE alpha beta gamma"
A1_DOUBT = {"start": 29, "end": 34, "alternatives": ["gamna", "gaMma"], "confidence": "low"}
A1_GAP = {"position": "internal", "start": 23, "end": 23, "witness_evidence": []}


def _perlector():
    spec = importlib.util.spec_from_file_location(
        "reader_uncertainty_perlector", ROOT / "pipeline" / "4_perlector" / "run.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(root: Path, scenario: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            "synthetic-two-page-v0",
            "--scenario",
            scenario,
            "--run-id",
            "r",
            "--run-root",
            str(root),
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _records(tree: RunTree, stage: str, kind: str) -> dict[str, dict]:
    return {
        record["payload"]["act_key"]: record
        for record in (
            tree.read_artifact(stage, kind, entry["artifact_id"])
            for entry in tree.build_manifest(stage)["artifacts"]
            if entry["kind"] == kind
        )
    }


def _export(tree: RunTree) -> tuple[dict, dict, list[dict]]:
    (export,) = [
        tree.read_artifact(ARMARIUM, "export", entry["artifact_id"])
        for entry in tree.build_manifest(ARMARIUM)["artifacts"]
        if entry["kind"] == "export"
    ]
    payload = export["payload"]
    with zipfile.ZipFile(
        io.BytesIO(tree.read_bytes(payload["bundle"]["reference"]["relative_path"]))
    ) as z:
        manifest = json.loads(z.read("EXPORT_MANIFEST.json"))
        rows = [json.loads(line) for line in z.read("acts.jsonl").splitlines()]
    return payload, manifest, rows


# --- The assessment record, anchored to the exact text -----------------------


def test_a_reader_with_no_assessment_key_is_read_as_not_assessed_never_confident():
    perlector = _perlector()
    record = perlector._assessed({"text": "alpha", "stop_reason": "stop"}, text="alpha")
    assert record["state"] == "not-assessed"
    assert record["uncertain_spans"] == [] and record["gaps"] == []
    assert record["problem"] == annotations.NOT_ASSESSED_REASON


def test_unicode_offsets_anchor_by_code_point_and_a_repeated_word_by_position():
    perlector = _perlector()
    text = "né le dix août né à Rouen"  # 'né' twice; accents are one code point each
    second = text.index("né", 1)
    report = {
        "state": "assessed",
        "uncertain_spans": [
            {"start": second, "end": second + 2, "alternatives": ["ne"], "confidence": "medium"}
        ],
        "gaps": [{"position": "internal", "start": 3, "end": 3, "witness_evidence": []}],
        "problem": None,
    }
    record = perlector._assessed({"text": text, "assessment": report}, text=text)
    assert record["state"] == "assessed"
    (span,) = record["uncertain_spans"]
    assert text[span["start"] : span["end"]] == "né" and span["start"] == second
    assert text.encode("utf-8")[span["start"] : span["end"]] != b"n\xc3\xa9", (
        "offsets are code points, so the byte slice at the same numbers is a different string"
    )


# Each case carries the text its report is anchored against, because one of the
# refusals only exists over a text that holds nothing: a `whole-act` gap over
# "alpha beta" is already refused by the gap schema for contradicting the text
# it sits beside, and the rule that a *reader* may never report one -- an empty
# reading is the `no-readable-text` outcome, not a doubt -- would then be a
# branch no case reaches (principle 8).
@pytest.mark.parametrize(
    ("report", "text", "problem_fragment"),
    [
        (
            {
                "state": "assessed",
                "uncertain_spans": [
                    {"start": 0, "end": 99, "alternatives": [], "confidence": "low"}
                ],
                "gaps": [],
                "problem": None,
            },
            "alpha beta",
            "outside text bounds",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [],
                "gaps": [{"position": "internal", "start": 1, "end": 3, "witness_evidence": []}],
                "problem": None,
            },
            "alpha beta",
            "may carry no characters of its own",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [],
                "gaps": [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}],
                "problem": None,
            },
            "",
            "cannot be whole-act",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [],
                "gaps": [
                    {
                        "position": "internal",
                        "start": 1,
                        "end": 1,
                        # Well-formed evidence, so the gap schema accepts it
                        # and the rule under test is the one that refuses it.
                        "witness_evidence": [
                            {
                                "chair": "attestator_1",
                                "testimonium_id": "t-1",
                                "reference": {
                                    "relative_path": "3_attestatores/t.json",
                                    "sha256": "0" * 64,
                                },
                                "variant": "alpha",
                            }
                        ],
                    }
                ],
                "problem": None,
            },
            "alpha beta",
            "carries no witness evidence",
        ),
        (
            {
                "state": "assessed",
                "uncertain_spans": [
                    {"start": 0, "end": 2, "alternatives": [], "confidence": "certain"}
                ],
                "gaps": [],
                "problem": None,
            },
            "alpha beta",
            "confidence",
        ),
        (
            {"state": "sure", "uncertain_spans": [], "gaps": [], "problem": None},
            "alpha beta",
            "unknown state",
        ),
        ({"spans": []}, "alpha beta", "closed record"),
        (
            {"state": "assessed", "uncertain_spans": [], "gaps": [], "problem": "but"},
            "alpha beta",
            "carries no problem",
        ),
        (
            {"state": "not-assessed", "uncertain_spans": [], "gaps": [], "problem": None},
            "alpha beta",
            "must say why",
        ),
        (
            {"state": "malformed", "uncertain_spans": [], "gaps": [], "problem": None},
            "alpha beta",
            "must say why",
        ),
    ],
)
def test_a_report_the_schema_cannot_anchor_becomes_a_visible_malformed_record(
    report, text, problem_fragment
):
    """Refused reports are retained as faults with empty layers, never as confidence."""
    perlector = _perlector()
    record = perlector._assessed({"text": text, "assessment": report}, text=text)
    assert record["state"] == "malformed"
    assert record["uncertain_spans"] == [] and record["gaps"] == []
    assert problem_fragment in record["problem"]
    with pytest.raises(SchemaRefusal):
        annotations.validate_assessment(report, text)


def test_the_canonical_layer_refuses_a_perlectio_sealed_before_the_assessment_existed():
    payload = {
        "text": "alpha",
        "uncertain_spans": [],
        "gaps": [],
        "self_revision": [],
    }
    with pytest.raises(SchemaRefusal, match="carries no uncertainty_assessment"):
        from_perlectio(payload)
    payload["uncertainty_assessment"] = {"state": "assessed", "problem": None}
    assert from_perlectio(payload)["assessment"] == {"state": "assessed", "problem": None}
    payload["uncertainty_assessment"] = {"state": "assessed", "problem": "odd"}
    with pytest.raises(SchemaRefusal, match="exactly when it is not assessed"):
        from_perlectio(payload)


# --- Through the real stages ---------------------------------------------------


def test_a_declared_doubt_arrives_intact_beside_the_same_text_in_review_and_export(tmp_path):
    root = tmp_path / "runs"
    result = _run(root, "reader-doubt")
    assert result.returncode == 3, result.stderr
    assert "delivered with partial text" in result.stdout
    tree = RunTree(root, "r")

    readings = _records(tree, PERLECTOR, "perlectio")
    a1, a2 = readings["a1"]["payload"], readings["a2"]["payload"]
    # Exact spans and exact text, asserted separately.
    assert a1["text"] == A1_TEXT
    assert a1["uncertain_spans"] == [A1_DOUBT]
    assert a1["gaps"] == [A1_GAP]
    assert a1["text"][A1_DOUBT["start"] : A1_DOUBT["end"]] == "gamma"
    assert a1["uncertainty_assessment"] == {"state": "assessed", "problem": None}
    assert a1["audit"]["examination"] == "complete"
    # The act that was assessed and reported nothing: an honest empty list.
    assert a2["uncertain_spans"] == [] and a2["gaps"] == []
    assert a2["uncertainty_assessment"] == {"state": "assessed", "problem": None}

    reviews = _records(tree, RECENSOR, "review")
    assert reviews["a1"]["outcome"] == "accepted", "a doubt is displayed, never a hold"
    # The review carries the same closed object the Perlectio sealed, under the
    # same name: one field name, one shape, wherever a consumer meets it.
    assert reviews["a1"]["payload"]["uncertainty_assessment"] == {
        "state": "assessed",
        "problem": None,
    }
    assert reviews["a2"]["payload"]["uncertainty_assessment"]["state"] == "assessed"

    established = _records(tree, ARCHETYPUS, "archetypus")
    assert established["a1"]["payload"]["text"] == A1_TEXT
    assert established["a1"]["payload"]["text_status"] == "partial", "an unread gap is partial"
    assert established["a1"]["payload"]["uncertainty"]["uncertain_spans"] == [A1_DOUBT]
    assert established["a1"]["payload"]["uncertainty"]["assessment"]["state"] == "assessed"
    assert established["a2"]["payload"]["text_status"] == "established"

    export, manifest, rows = _export(tree)
    assert export["aggregate"]["status"] == "partial"
    by_key = {act["act_key"]: act for act in export["delivered"]}
    assert by_key["a1"]["text"] == A1_TEXT
    assert by_key["a1"]["uncertainty"]["uncertain_spans"] == [A1_DOUBT]
    assert by_key["a1"]["uncertainty"]["gaps"] == [A1_GAP]
    assert by_key["a1"]["uncertainty"]["assessment"] == {"state": "assessed", "problem": None}
    assert by_key["a2"]["uncertainty"]["uncertain_spans"] == []
    # Every literal-text format carries the same layer beside the same text.
    (jsonl_a1,) = [row for row in rows if row["act_key"] == "a1"]
    assert jsonl_a1["canonical_clean_text"] == A1_TEXT
    assert jsonl_a1["uncertainty"]["uncertain_spans"] == [A1_DOUBT]
    assert jsonl_a1["uncertainty"]["assessment"]["state"] == "assessed"
    (instrument,) = [
        entry
        for entry in manifest["claims"]["not_measured"]["entries"]
        if entry["instrument"] == "perlector-uncertain-spans"
    ]
    assert instrument["status"] == "measured"
    assert instrument["detail"]["acts_assessed"] == 2
    assert instrument["detail"]["acts_not_assessed"] == 0
    assert instrument["detail"]["acts_with_uncertain_spans"] == 1

    projection = dataclasses.asdict(ReadOnlyRun(root, "r").projection())
    export_row = next(act for act in projection["acts"] if act["act_key"] == "a1")["row"]
    assert export_row is not None  # the export view carries the export row
    text = "\n".join(review_text.render(projection))
    # An established reading's layer is a union of the reader's report and the
    # audit's projection, and this surface cannot tell which entry is whose, so
    # it does not credit the reader with all of them.
    assert (
        "doubts: assessed by the reader; this view cannot tell which of the span(s) below "
        "are its report and which the audit's; 1 uncertain span(s), 1 gap(s)" in text
    )
    assert "[29, 34) 'gamma' confidence low; alternatives: gamna, gaMma" in text
    assert "gap (internal) at 23" in text


def test_a_chair_without_a_doubt_channel_is_disclosed_as_unproduced_not_confident(tmp_path):
    root = tmp_path / "runs"
    assert _run(root, "happy").returncode == 0
    tree = RunTree(root, "r")
    for reading in _records(tree, PERLECTOR, "perlectio").values():
        assert reading["payload"]["uncertainty_assessment"]["state"] == "not-assessed"
        assert reading["payload"]["uncertain_spans"] == []
    export, manifest, _rows = _export(tree)
    assert export["aggregate"]["status"] == "complete"
    for act in export["delivered"]:
        assert act["uncertainty"]["assessment"]["state"] == "not-assessed"
        assert "no channel" in act["uncertainty"]["assessment"]["problem"]
    (instrument,) = [
        entry
        for entry in manifest["claims"]["not_measured"]["entries"]
        if entry["instrument"] == "perlector-uncertain-spans"
    ]
    assert instrument["status"] == "declared-unproduced"
    assert instrument["detail"] == {
        "sealed_audit_round_cap": 1,
        "acts_delivered": 2,
        "acts_with_uncertain_spans": 0,
        "acts_assessed": 0,
        "acts_not_assessed": 2,
    }

    # And the one person reviewing the run reads the absence, not a silence.
    # A delivered act has no Perlectio row in the console projection, so the
    # state has to be rendered from the export row's own canonical layer.
    text = "\n".join(review_text.render(dataclasses.asdict(ReadOnlyRun(root, "r").projection())))
    assert "doubts: not-assessed — the reader reports no doubt assessment" in text
    assert "doubts: assessed" not in text


def test_a_malformed_report_holds_the_act_with_the_problem_retained_and_no_empty_confidence(
    tmp_path,
):
    root = tmp_path / "runs"
    result = _run(root, "reader-doubt-malformed")
    assert result.returncode == 3, result.stderr
    tree = RunTree(root, "r")
    a1 = _records(tree, PERLECTOR, "perlectio")["a1"]["payload"]
    assert a1["text"] == A1_TEXT, "the reading itself stands"
    assert a1["uncertain_spans"] == [] and a1["gaps"] == []
    assert a1["uncertainty_assessment"]["state"] == "malformed"
    assert "outside text bounds (0..34)" in a1["uncertainty_assessment"]["problem"]
    review = _records(tree, RECENSOR, "review")["a1"]
    assert review["outcome"] == "held-for-review"
    assert review["payload"]["uncertainty_assessment"]["state"] == "malformed"
    assert (
        review["payload"]["uncertainty_assessment"]["problem"]
        == a1["uncertainty_assessment"]["problem"]
    )
    assert "doubt report over this act could not be anchored" in review["payload"]["reason"]
    assert a1["uncertainty_assessment"]["problem"] in review["payload"]["reason"]
    export, manifest, rows = _export(tree)
    assert [act["act_key"] for act in export["delivered"]] == ["a2"]
    (held,) = export["non_delivered"]
    assert held["act_key"] == "a1" and held["category"] == "held-for-review"
    assert (jsonl_a1 := next(row for row in rows if row["act_key"] == "a1"))["uncertainty"] is None
    assert jsonl_a1["canonical_clean_text"] is None
    text = "\n".join(review_text.render(dataclasses.asdict(ReadOnlyRun(root, "r").projection())))
    assert "could not be anchored" in text


def test_a_reproof_that_changes_the_text_publishes_its_own_doubts_never_a_reanchored_copy(
    tmp_path,
):
    """audit-change: Pass B doubts "gamma" at [29,34); the re-proof publishes "gamma!" and
    its own doubt at [29,35). Only the re-proof's report may travel with its text."""
    root = tmp_path / "runs"
    assert _run(root, "audit-change").returncode == 0
    tree = RunTree(root, "r")
    a1 = _records(tree, PERLECTOR, "perlectio")["a1"]["payload"]
    assert a1["text"] == "SYNTHETIC ACT ONE alpha beta gamma!"
    assert a1["uncertain_spans"] == [
        {"start": 29, "end": 35, "alternatives": ["gamma"], "confidence": "medium"}
    ]
    assert a1["text"][29:35] == "gamma!"
    assert a1["uncertainty_assessment"] == {"state": "assessed", "problem": None}
    export, _manifest, rows = _export(tree)
    (row,) = [row for row in rows if row["act_key"] == "a1"]
    assert row["canonical_clean_text"] == a1["text"]
    assert row["uncertainty"]["uncertain_spans"] == a1["uncertain_spans"]


def test_a_cut_off_reproof_keeps_pass_bs_doubtless_assessment_with_pass_bs_text(tmp_path):
    """The assessment travels with the call whose text is published (F1 meets F2)."""
    root = tmp_path / "runs"
    assert _run(root, "audit-reproof-cutoff").returncode == 3
    a1 = _records(RunTree(root, "r"), PERLECTOR, "perlectio")["a1"]["payload"]
    assert a1["uncertainty_assessment"]["state"] == "not-assessed"
    assert a1["audit"]["examination"] == "incomplete"
    assert a1["uncertain_spans"] == []


# --- The two layers under one state, and the collisions between them ----------


_EXHAUSTED_CAP_CONFIG = (
    'schema = "perlector-audit.v3"\n'
    "default_round_cap = 1\n"
    "absolute_round_cap = 2\n"
    "round_cap = 0\n"
    'approval_ref = ""\n'
)


def test_the_cap_projection_leads_the_readers_own_doubts_and_neither_is_dropped(tmp_path):
    """The one combination the chain check's prefix rule exists for.

    Under a sealed cap of 0 the audit mints its own spans onto every flagged
    act, and `reader-doubt` declares a reader's doubt over the same act. The
    published layer must be the projection exactly, in order, at the head, then
    the reader's own report -- and `validate_chain` must accept that, which is a
    branch every other test runs with an empty projection, where the comparison
    is vacuous (named as a gap by the independent review of 2026-09-11).
    """
    config = tmp_path / "exhausted.toml"
    config.write_text(_EXHAUSTED_CAP_CONFIG)
    root = tmp_path / "runs"
    assert _run(root, "reader-doubt", "--perlector-audit-config", str(config)).returncode == 3
    tree = RunTree(root, "r")
    findings = _records(tree, PERLECTOR, "audit-finding")
    a1 = _records(tree, PERLECTOR, "perlectio")["a1"]
    payload = a1["payload"]

    projected = [
        {"start": span["start"], "end": span["end"], "alternatives": [], "confidence": "low"}
        for span in findings["a1"]["payload"]["uncertain_spans"]
    ]
    assert projected, "a zero cap must leave this act's flags as exhausted-cap spans"
    assert payload["uncertainty_assessment"] == {"state": "assessed", "problem": None}
    assert payload["uncertain_spans"][: len(projected)] == projected
    assert payload["uncertain_spans"][len(projected) :] == [A1_DOUBT]
    assert payload["gaps"] == [A1_GAP]
    # Nothing beyond those two sources, and nothing dropped between them.
    assert payload["uncertain_spans"] == projected + [A1_DOUBT]
    audit.validate_chain(tree, a1, a1["subject_id"])


def test_the_union_keeps_every_entry_including_an_exact_repeat():
    """The producer's own union rule, asked directly.

    An exact repeat is kept, not dropped: the two entries are the only record
    that both instruments doubted those characters, because nothing in the run
    holds the reader's report separately. What a pair means is said once, by the
    operator console, which coalesces it into one line naming both instruments.

    A fixture cannot declare a doubt at offsets the audit has not computed yet,
    so this case is unreachable end to end; the rule is asked here rather than
    left as a branch nothing measures (principle 8).
    """
    perlector = _perlector()
    projected = {"start": 3, "end": 7, "alternatives": [], "confidence": "low"}
    repeat = {"start": 3, "end": 7, "alternatives": [], "confidence": "low"}
    overlapping = {"start": 5, "end": 9, "alternatives": ["x"], "confidence": "high"}

    assert perlector._union_with_projection([projected], [repeat, overlapping]) == [
        projected,
        repeat,
        overlapping,
    ]
    # The projection always leads, and is published even when the reader said
    # nothing at all.
    assert perlector._union_with_projection([projected], []) == [projected]
    assert perlector._union_with_projection([], [overlapping]) == [overlapping]


def test_a_declared_doubt_over_an_unreadable_act_is_refused_rather_than_emptied(tmp_path):
    """Pass B: the outcome empties the text, so the report is re-asked against it.

    The reader declared a doubt at offsets its own reading no longer has. The
    two claims cannot both be published, and the one that is dropped must be
    visible: the record seals `malformed` with the refusal retained, and the
    Recensor holds the act rather than delivering it under an `assessed` state
    beside layers that were quietly emptied.
    """
    root = tmp_path / "runs"
    assert _run(root, "reader-doubt-unreadable").returncode == 3
    tree = RunTree(root, "r")
    a1 = _records(tree, PERLECTOR, "perlectio")["a1"]
    assert a1["outcome"] == "no-readable-text"
    assert a1["payload"]["text"] == ""
    assert a1["payload"]["uncertain_spans"] == []
    assert [gap["position"] for gap in a1["payload"]["gaps"]] == ["whole-act"]
    assert a1["payload"]["uncertainty_assessment"]["state"] == "malformed"
    assert "outside text bounds (0..0)" in a1["payload"]["uncertainty_assessment"]["problem"]
    review = _records(tree, RECENSOR, "review")["a1"]
    assert review["outcome"] == "held-for-review"
    assert "could not be anchored" in review["payload"]["reason"]
    # The retained problem is quoted into the reason a person reads, not merely
    # pointed at on an artifact they would have to go and find.
    assert a1["payload"]["uncertainty_assessment"]["problem"] in review["payload"]["reason"]
    assert [act["act_key"] for act in _export(tree)[0]["delivered"]] == ["a2"]


def test_an_emptied_reading_re_asks_the_report_instead_of_emptying_it_under_assessed():
    """The one rubric both emptying paths use, asked where it lives.

    Pass B and the Pass-C re-proof both publish an empty text with its whole-act
    gap when the outcome turns `no-readable-text`, and both go through
    `_published_doubt`. Before the independent review of 2026-09-11 the re-proof
    path emptied the two layers and left the state alone, sealing "the reader
    assessed this act and reported nothing" over a report it had just thrown
    away.

    Asked here rather than through a scenario because no scenario in this
    fixture produces a flag whose location covers the act: `audit.validate_finding`
    refuses a re-proof that changes text outside a flagged location, and the
    flags these scenarios mint are narrow testimony diffs, so no re-proof here
    can empty a text Pass B read. Whether some flag class could cover a whole
    act and make the path reachable is not settled either way; the end-to-end
    re-proof path is named as untested. The Pass-B half of the same rubric IS
    driven end to end, by `reader-doubt-unreadable` above.
    """
    perlector = _perlector()
    whole_act = [{"position": "whole-act", "start": 0, "end": 0, "witness_evidence": []}]
    report = {
        "state": "assessed",
        "uncertain_spans": [{"start": 0, "end": 5, "alternatives": ["x"], "confidence": "low"}],
        "gaps": [],
        "problem": None,
    }

    sealed, spans, gaps = perlector._published_doubt(
        {"text": "alpha beta", "assessment": report},
        text="",
        outcome="no-readable-text",
        whole_act_gaps=whole_act,
    )
    assert sealed["state"] == "malformed"
    assert "outside text bounds (0..0)" in sealed["problem"]
    assert spans == [] and gaps == whole_act

    # And where the reading stands, the report is published as the reader gave it.
    sealed, spans, gaps = perlector._published_doubt(
        {"text": "alpha beta", "assessment": report},
        text="alpha beta",
        outcome="read",
        whole_act_gaps=whole_act,
    )
    assert sealed == {"state": "assessed", "problem": None}
    assert spans == report["uncertain_spans"] and gaps == []

    # A gap the re-ask CAN anchor to an empty text -- a zero-width `leading` or
    # `trailing` gap at offset 0 -- would otherwise come back `assessed` and
    # then be discarded in favour of the outcome's whole-act gap, under a state
    # saying the reader was asked and answered. Over an empty text the two are
    # the same claim, but "nothing is dropped in silence" has to be literally
    # true, so the report is set aside as `malformed` and says why.
    degenerate = {
        "state": "assessed",
        "uncertain_spans": [],
        "gaps": [{"position": "leading", "start": 0, "end": 0, "witness_evidence": []}],
        "problem": None,
    }
    sealed, spans, gaps = perlector._published_doubt(
        {"text": "   ", "assessment": degenerate},
        text="",
        outcome="no-readable-text",
        whole_act_gaps=whole_act,
    )
    assert sealed["state"] == "malformed"
    assert "the whole-act gap is the only annotation the outcome allows" in sealed["problem"]
    assert spans == [] and gaps == whole_act


def test_the_instrument_records_carry_the_doubt_report_too(tmp_path):
    """A doubt reported on an instrument call is a measurement, not a discard.

    Both instruments are sampled, and their default rate is zero, so the run
    below raises each to 1000 per mille with its approval reference exactly as
    `test_prior_protocol.py` does. Named because the first version of this test
    ran the plain `happy` scenario and looped over two empty collections: two
    assertions that never executed and read as a pass (principle 8; the
    independent review of 2026-09-11).
    """
    root = tmp_path / "runs"
    perlector = _perlector()
    sampled = (
        "--nuda-per-mille",
        "1000",
        "--nuda-approval-ref",
        perlector.NUDA_APPROVAL_SUBJECT,
        "--perlector-instrument-per-mille",
        "1000",
        "--perlector-instrument-approval-ref",
        perlector.PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT,
    )
    # A sampled instrument is Tyrel's decision and the Perlector resolves the
    # approval record it was told to; the run refuses without one on disk. The
    # records are rebuilt from the flags by the sibling suite's own deterministic
    # helper rather than copied, so a change to the binding cannot leave this
    # test placing a record the stage no longer asks for.
    placed = RunTree(root, "r")
    for record in sampling_approval_records("happy", *sampled).values():
        placed.write_approval_record(record)
    assert _run(root, "happy", *sampled).returncode == 0

    tree = RunTree(root, "r")
    expected = {"state": "not-assessed", "problem": annotations.NOT_ASSESSED_REASON}

    for kind in ("lectio-prior", "lectio-nuda", "primed-without-prior"):
        records = _records(tree, PERLECTOR, kind)
        assert records, f"this run must publish at least one {kind} record"
        for record in records.values():
            assert record["payload"]["uncertainty_assessment"] == expected
            # And the layers stay empty under that state, which is what
            # `_validate_sealed_doubt` refuses to publish otherwise.
            assert record["payload"]["uncertain_spans"] == []


# --- The fixture's own refusals, and the producer's pre-publication check ------


_DOUBT = {
    "scenario": "happy",
    "act_key": "a1",
    "start": 0,
    "end": 1,
    "alternatives": [],
    "confidence": "low",
}


def _fixture(**tables) -> dict:
    base = {
        "act": [{"key": "a1", "text": "alpha beta"}],
        "page": [],
        "scenario": [{"name": "happy"}],
    }
    base.update(tables)
    return base


def _read(fixture: dict, scenario: str = "happy"):
    return reader_module.FixtureReader(fixture, scenario).read(
        {"act_id": "act_0000000000000000", "act_key": "a1", "regions": [], "page_renders": []},
        pass_kind="perlectio",
    )


@pytest.mark.parametrize(
    ("tables", "expected"),
    [
        ({"reader_doubt": [{**_DOUBT, "scenario": "nowhere"}]}, "undeclared scenario"),
        ({"reader_doubt": [{**_DOUBT, "act_key": "a9"}]}, "undeclared act"),
        (
            {"reader_doubt": [{**_DOUBT, "pass_kind": "not-a-pass"}]},
            "unknown pass kind",
        ),
        (
            {
                "reader_assessment": [
                    {"scenario": "happy", "act_key": "a1", "state": "assessed", "problem": ""},
                    {"scenario": "happy", "act_key": "a1", "state": "malformed", "problem": "x"},
                ]
            },
            "twice",
        ),
        # The two refusals the correction added: a detail row that would have
        # been discarded in silence, and a state whose whole content is missing.
        ({"reader_doubt": [dict(_DOUBT)]}, "cannot also report one"),
        (
            {
                "reader_assessment": [
                    {"scenario": "happy", "act_key": "a1", "state": "malformed", "problem": ""}
                ]
            },
            "with no problem",
        ),
        # A row naming no pass covers every pass, so a pass-scoped row beside it
        # is a second answer to the same call, not a second key.
        (
            {
                "reader_assessment": [
                    {"scenario": "happy", "act_key": "a1", "state": "assessed", "problem": ""},
                    {
                        "scenario": "happy",
                        "act_key": "a1",
                        "state": "malformed",
                        "problem": "x",
                        "pass_kind": "perlectio",
                    },
                ]
            },
            "with 2 rows",
        ),
    ],
)
def test_the_fixture_refuses_a_doubt_row_it_cannot_honestly_serve(tables, expected):
    with pytest.raises(KeyError, match=expected):
        _read(_fixture(**tables))


def test_a_declared_not_assessed_row_reports_this_modules_own_reason():
    """TOML has no null, so an empty problem is the absence, under that state alone."""
    result = _read(
        _fixture(
            reader_assessment=[
                {"scenario": "happy", "act_key": "a1", "state": "not-assessed", "problem": ""}
            ]
        )
    )

    assert result["assessment"] == annotations.not_assessed()


def test_a_declared_malformed_row_keeps_the_problem_it_stands_in_for():
    result = _read(
        _fixture(
            reader_assessment=[
                {
                    "scenario": "happy",
                    "act_key": "a1",
                    "state": "malformed",
                    "problem": "the engine returned a doubt this fixture cannot anchor",
                }
            ]
        )
    )

    assert result["assessment"]["state"] == "malformed"
    assert result["assessment"]["uncertain_spans"] == [] and result["assessment"]["gaps"] == []
    assert "cannot anchor" in result["assessment"]["problem"]


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        ("not an object", "closed {state, problem}"),
        ({"state": "assessed"}, "closed {state, problem}"),
        ({"state": ["assessed"], "problem": None}, "unknown doubt state"),
        ({"state": "confident", "problem": None}, "unknown doubt state"),
        ({"state": "malformed", "problem": ""}, "neither null nor a non-empty string"),
        ({"state": "not-assessed", "problem": None}, "exactly when it is not assessed"),
        ({"state": "assessed", "problem": "why"}, "exactly when it is not assessed"),
    ],
)
def test_the_producer_refuses_a_sealed_doubt_report_it_would_never_have_written(
    assessment, expected
):
    """Where it was introduced, not one stage later (the review of 2026-09-11).

    Before this, a malformed record published here, reached the Recensor as
    state `None` -- no hold -- and failed at the Archetypus.
    """
    perlector = _perlector()
    payload = {"uncertainty_assessment": assessment, "uncertain_spans": [], "gaps": []}

    with pytest.raises(SchemaRefusal, match=re.escape(expected)):
        perlector._validate_sealed_doubt(payload, fields=perlector._LECTIO_NUDA_FIELDS)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "uncertainty_assessment": {"state": "not-assessed", "problem": "no channel"},
                "uncertain_spans": [
                    {"start": 0, "end": 1, "alternatives": [], "confidence": "low"}
                ],
                "gaps": [],
            },
            "publishes an uncertain span of its own",
        ),
        (
            {
                "uncertainty_assessment": {"state": "malformed", "problem": "refused"},
                "uncertain_spans": [],
                "gaps": [{"position": "internal", "start": 1, "end": 1, "witness_evidence": []}],
            },
            "publishes a gap of its own",
        ),
    ],
)
def test_an_instrument_record_may_not_publish_a_layer_its_state_denies(payload, expected):
    """Asked only where there is no audit behind the record, because there every
    span is the reader's own -- the established Perlectio's layer is a union
    only `validate_chain` can take apart."""
    perlector = _perlector()

    # The real closed field sets, not invented ones: if a record kind ever
    # gained or lost `audit`, this test moves with it.
    assert "audit" not in perlector._LECTIO_NUDA_FIELDS
    assert "audit" in perlector._PERLECTIO_FIELDS
    with pytest.raises(SchemaRefusal, match=expected):
        perlector._validate_sealed_doubt(payload, fields=perlector._LECTIO_NUDA_FIELDS)
    # The same payload under a record that does carry an audit is this check's
    # business no longer, and passes here: `validate_chain` applies the rule
    # there, over the finding that says which span is whose.
    perlector._validate_sealed_doubt(payload, fields=perlector._PERLECTIO_FIELDS)
