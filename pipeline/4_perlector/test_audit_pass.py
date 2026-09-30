"""The sealed Pass-C audit policy, and the audit record validators the Recensor reads.

The policy is loaded by `audit.py` and recorded on every `page-reading` as not
run; the validators are `common/perlector_audit.py`'s.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import audit
import protocol
import pytest
import reader as reader_module

from common import perlector_audit, truncation
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.perlector_audit import (
    LEGACY_SCHEMA,
    REPROOF_PASS_KIND,
    neutral_prompt,
    render_reproof_instruction,
    truncation_classification,
    validate_audit_request,
    validate_truncation_record,
)
from common.runtree.store import RunTree
from conftest import load_stage

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def test_witness_derived_location_classes_remain_the_one_open_class():
    """Location provenance does not make page-boundary evidence a text flag.

    Do not widen this set for boundary disagreement: that is Recensor page
    evidence, not a witness-derived text location.
    """
    assert perlector_audit.WITNESS_DERIVED_LOCATION_CLASSES == frozenset({"testimony-diff"})


def test_audit_draft_requires_location_basis_exactly_when_testimony_located_a_flag():
    policy = {"schema": perlector_audit.SCHEMA, "sha256": "a" * 64, "approval_ref": "approved"}
    draft = {
        "act_key": "a1",
        "attempt_ordinal": 1,
        "semi_final_text": "alpha beta",
        "page_ids": ["page-1"],
        "round_cap": 1,
        "policy": policy,
        "flags": [{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
        "flag_location_basis": [],
    }
    with pytest.raises(SchemaRefusal, match="flags and witness-derived location basis disagree"):
        perlector_audit.validate_draft(draft)

    draft["flags"] = []
    draft["flag_location_basis"] = [
        {
            "class": "testimony-diff",
            "chair": "witness-1",
            "derivation": "own-report",
            "location": {"start": 6, "end": 10},
        }
    ]
    with pytest.raises(SchemaRefusal, match="flags and witness-derived location basis disagree"):
        perlector_audit.validate_draft(draft)

    # The case equal lengths could never catch: one flag, one basis row, and
    # the row accounting for a span no flag names. Read by count alone this
    # draft was well formed, and the record said a chair located a flag it
    # did not.
    draft["flags"] = [{"class": "testimony-diff", "location": {"start": 0, "end": 5}}]
    with pytest.raises(SchemaRefusal, match="flags and witness-derived location basis disagree"):
        perlector_audit.validate_draft(draft)

    draft["flags"] = [
        {"class": "testimony-diff", "location": {"start": 0, "end": 5}},
        {"class": "testimony-diff", "location": {"start": 6, "end": 10}},
    ]
    draft["flag_location_basis"] *= 2
    with pytest.raises(SchemaRefusal, match="repeats a witness-derived flag-location basis"):
        perlector_audit.validate_draft(draft)


def _run(root: Path, *extra: str, scenario: str = "happy"):
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


def _records(tree: RunTree, kind: str, stage: str = PERLECTOR) -> list[dict]:
    return [
        tree.read_artifact(stage, kind, entry["artifact_id"])
        for entry in tree.build_manifest(stage)["artifacts"]
        if entry["kind"] == kind
    ]


# The fixture's own page area, and the sealed `[truncation]` table the
# instrument judges under, read the way the stage reads it. The hand-built
# records below sit on the 200x260 fixture page, under the sealed legible size,
# so their length is not judged: these tests are about the audit's
# reconciliation of a record, not about the length signal.
_TRUNCATION_POLICY = protocol.load(ROOT / "config" / "perlector_protocol.toml")[0]["truncation"]
_TEST_REGION_PIXELS = 18612
_TEST_PAGE_PIXELS = 200 * 260
# The sealed floor every measure below was judged under. It travels on the
# record since 2026-09-14, so a fixture that omits it is not a closed record.
_FLOOR = _TRUNCATION_POLICY[protocol.LENGTH_FLOOR_FIELD]
_GATE = _TRUNCATION_POLICY[protocol.LEGIBLE_PAGE_FIELD]


def _gate_terms(page_pixels):
    """The measure terms that say whether a page of this size has its length judged."""
    return {
        "smallest_page_pixels": page_pixels,
        "legible_page_pixels": _GATE,
        "length_judged": page_pixels >= _GATE,
    }


# The truncation instrument's record of a call that ran to completion over a
# clean text; what a well-formed v2 finding carries for a completed re-proof.
_COMPLETE_TRUNCATION = {
    "classification": "complete",
    "signals": {
        "stop_reason_declared": "stop",
        "unclosed_structure": False,
        "length_suspicious": None,
        "ends_abruptly": False,
    },
    "measure": {
        "region_pixels": _TEST_REGION_PIXELS,
        "page_pixels": _TEST_PAGE_PIXELS,
        "characters": len("alpha beta gamma"),
        "length_floor_characters_per_page": _FLOOR,
        **_gate_terms(_TEST_PAGE_PIXELS),
    },
}
# The same instrument over a re-proof its engine cut off: the text signals are
# clean (it returned the frozen text), and the engine's own word overrules them.
# Measured over fixture act a1 as the Perlector measures it: its 34-character
# reading over the padded 188x99 crop (18,612 px) of the 200x260 page.
_FIXTURE_A1_MEASURE = {
    "region_pixels": 18612,
    "page_pixels": 52_000,
    "characters": 34,
    "length_floor_characters_per_page": _FLOOR,
    **_gate_terms(52_000),
}
_CUT_OFF_TRUNCATION = {
    "classification": "truncated",
    "signals": {**_COMPLETE_TRUNCATION["signals"], "stop_reason_declared": "length"},
    "measure": _FIXTURE_A1_MEASURE,
}


def test_the_reader_refuses_a_reproof_pass_whose_instrument_never_arrived():
    """The seam's own half of the repair, independent of any run.

    `run.py` building the request correctly is one claim; a reader that would
    have carried on without one is the other, and it is the half that made the
    defect invisible. A reader may not condition generation on `pass_kind`, so
    a re-proof pass with no request is not a call it can complete honestly --
    there is no span to re-examine and no delivered task to answer.
    """
    request = perlector_audit.audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/artifacts/draft.json", "sha256": "a" * 64},
        semi_final_text="alpha beta gamma",
        flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
    )

    with pytest.raises(ContractError, match="no audit request"):
        reader_module.validate_audit_delivery(
            {"act_key": "a1"}, pass_kind=REPROOF_PASS_KIND, audit_request=None
        )
    # The mirror: a span-scoped task delivered to a read of the whole act.
    with pytest.raises(ContractError, match="belongs to the pass that seals it"):
        reader_module.validate_audit_delivery(
            {"act_key": "a1"}, pass_kind="perlectio", audit_request=request
        )
    # And one act's frozen locations beside another act's pixels.
    with pytest.raises(ContractError, match="delivered beside the dossier"):
        reader_module.validate_audit_delivery(
            {"act_key": "a2"}, pass_kind=REPROOF_PASS_KIND, audit_request=request
        )
    assert (
        reader_module.validate_audit_delivery(
            {"act_key": "a1"}, pass_kind=REPROOF_PASS_KIND, audit_request=request
        )
        == request
    )


def test_a_directional_or_empty_audit_request_is_refused_at_the_delivery_boundary():
    """Neutrality is screened where the instrument is handed over, not only where
    it is stored. `payload.audit.reproofs` was already held to `neutral_prompt`
    exactly; the request now goes through the same screen, so a prompt telling
    the reader which way to argue cannot reach a reader by travelling on the
    delivered copy instead of the sealed one."""
    request = perlector_audit.audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/artifacts/draft.json", "sha256": "b" * 64},
        semi_final_text="alpha beta gamma",
        flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
    )

    directional = copy.deepcopy(request)
    directional["reproofs"][0]["prompt"] = "The reading is wrong; replace it with gamma."
    with pytest.raises(SchemaRefusal, match="neutral location-only"):
        validate_audit_request(directional)

    moved = copy.deepcopy(request)
    moved["reproofs"][0]["location"] = {"start": 6, "end": 99}
    with pytest.raises(SchemaRefusal, match="lies outside the delivered text"):
        validate_audit_request(moved)

    empty = copy.deepcopy(request)
    empty["reproofs"] = []
    with pytest.raises(SchemaRefusal, match="delivers no re-proof location"):
        validate_audit_request(empty)

    # `validate_input_refs` reads two keys and ignores the rest, so without the
    # nested closure an extra field here would reach the reader, enter the
    # digest, and survive `validate_chain`'s rebuild -- directional text riding
    # past the neutrality screen on the reference.
    widened = copy.deepcopy(request)
    widened["draft_ref"]["note"] = "the reading is probably gamma"
    with pytest.raises(SchemaRefusal, match="draft reference is not its closed shape"):
        validate_audit_request(widened)


def test_unhashable_audit_classes_are_named_schema_refusals():
    """Resealed JSON arrays cannot escape class allowlists as raw TypeError."""
    policy = {"schema": perlector_audit.SCHEMA, "sha256": "0" * 64, "approval_ref": ""}
    draft = {
        "act_key": "a1",
        "attempt_ordinal": 1,
        "semi_final_text": "x",
        "page_ids": ["p1"],
        "round_cap": 1,
        "policy": policy,
        "flags": [{"class": [], "location": {"start": 0, "end": 1}}],
        "flag_location_basis": [],
    }
    with pytest.raises(SchemaRefusal, match="unknown class"):
        perlector_audit.validate_draft(draft)

    finding = {
        key: value
        for key, value in draft.items()
        if key not in ("semi_final_text", "flag_location_basis")
    }
    finding.update(
        {
            "flags": [{"class": "testimony-diff", "location": {"start": 0, "end": 1}}],
            "change_record": [
                {"start": 0, "end": 1, "triggering_flag_class": []},
            ],
            "uncertain_spans": [],
            "unresolved": False,
            "examination": "complete",
            "reproof_truncation": _COMPLETE_TRUNCATION,
            "reproof_call": None,
            "reproof_edits": [
                {
                    "class": "testimony-diff",
                    "location": {"start": 0, "end": 1},
                    "original": "x",
                    "replacement": "y",
                }
            ],
        }
    )
    with pytest.raises(SchemaRefusal, match="unknown triggering flag class"):
        perlector_audit.validate_finding(finding, text="y", flag_text="x")

    reference = {"relative_path": "4_perlector/audit.json", "sha256": "0" * 64}
    perlectio_audit = {
        "draft_ref": reference,
        "finding_ref": reference,
        "finding_digest": "0" * 64,
        "unresolved": False,
        "examination": "complete",
        "request_digest": "0" * 64,
        "reproofs": [
            {
                "class": [],
                "location": {"start": 0, "end": 1},
                "prompt": neutral_prompt(start=0, end=1, text_length=1),
            }
        ],
    }
    with pytest.raises(SchemaRefusal, match="unknown class or prompt"):
        perlector_audit.validate_perlectio_audit(perlectio_audit, text_length=1)


def test_an_audit_round_cap_above_one_is_refused_because_no_second_round_exists(tmp_path):
    """A sealed cap of 2 with an approval reference would be recorded but never run."""
    approved = tmp_path / "approved.toml"
    approved.write_text(
        'schema = "perlector-audit.v3"\n'
        "default_round_cap = 1\n"
        "absolute_round_cap = 2\n"
        "round_cap = 2\n"
        'approval_ref = "project-lead-raised-audit-cap"\n'
    )
    with pytest.raises(ContractError, match="runs no second audit round"):
        audit.load(approved)


def test_a_legacy_v2_declaration_cannot_start_a_new_exact_edit_execution(tmp_path):
    legacy = tmp_path / "legacy.toml"
    legacy.write_text(
        f'schema = "{LEGACY_SCHEMA}"\n'
        "default_round_cap = 1\n"
        "absolute_round_cap = 1\n"
        "round_cap = 1\n"
        'approval_ref = ""\n'
    )
    with pytest.raises(ContractError, match="sealed under perlector-audit.v2"):
        audit.load(legacy)


def test_raised_cap_needs_the_project_leads_reference_and_exhaustion_routes_review(tmp_path):
    raised = tmp_path / "raised.toml"
    raised.write_text(
        'schema = "perlector-audit.v3"\ndefault_round_cap = 1\nabsolute_round_cap = 2\nround_cap = 2\napproval_ref = ""\n'
    )
    with pytest.raises(ContractError, match="the project lead's approval reference"):
        audit.load(raised)

    exhausted = tmp_path / "exhausted.toml"
    exhausted.write_text(
        'schema = "perlector-audit.v3"\ndefault_round_cap = 1\nabsolute_round_cap = 2\nround_cap = 0\napproval_ref = ""\n'
    )
    result = _run(tmp_path / "exhausted-runs", "--perlector-audit-config", str(exhausted))
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "exhausted-runs", "r")
    findings = _records(tree, "audit-finding")
    assert all(record["payload"]["unresolved"] for record in findings)
    finals = {record["subject_id"]: record for record in _records(tree, "perlectio")}
    for finding in findings:
        spans = finding["payload"]["uncertain_spans"]
        assert all(span["reason"] == "audit-round-cap-exhausted" for span in spans)
        assert finals[finding["subject_id"]]["payload"]["uncertain_spans"] == [
            {
                "start": span["start"],
                "end": span["end"],
                "alternatives": [],
                "confidence": "low",
            }
            for span in spans
        ]


def test_a_zero_width_exhausted_flag_stays_unresolved_without_inventing_a_span():
    """A point insertion is a real flag, but cannot become a non-empty text span."""

    finding = perlector_audit.validate_finding(
        {
            "act_key": "a1",
            "attempt_ordinal": 1,
            "page_ids": ["p1"],
            "round_cap": 0,
            "policy": {
                "schema": "perlector-audit.v3",
                "sha256": "0" * 64,
                "approval_ref": "",
            },
            "flags": [{"class": "testimony-diff", "location": {"start": 3, "end": 3}}],
            "change_record": [],
            "uncertain_spans": [],
            "unresolved": True,
            "examination": "cap-exhausted",
            "reproof_truncation": None,
            "reproof_call": None,
            "reproof_edits": None,
        },
        text="abc",
        flag_text="abc",
    )

    assert finding["flags"] and finding["uncertain_spans"] == []
    # And the contract itself refuses an invented zero-width span, so a
    # producer regression cannot smuggle one past this test's empty list.
    with pytest.raises(SchemaRefusal, match="no exhausted-cap reason or width"):
        perlector_audit.validate_finding(
            {
                "act_key": "a1",
                "attempt_ordinal": 1,
                "page_ids": ["p1"],
                "round_cap": 0,
                "policy": {
                    "schema": "perlector-audit.v3",
                    "sha256": "0" * 64,
                    "approval_ref": "",
                },
                "flags": [{"class": "testimony-diff", "location": {"start": 3, "end": 3}}],
                "change_record": [],
                "uncertain_spans": [{"start": 3, "end": 3, "reason": "audit-round-cap-exhausted"}],
                "unresolved": True,
                "examination": "cap-exhausted",
                "reproof_truncation": None,
                "reproof_call": None,
                "reproof_edits": None,
            },
            text="abc",
            flag_text="abc",
        )
    outcome, reason = load_stage("5_recensor").review_route_from_findings(
        testimony_shortfall=False,
        audit_unresolved=finding["unresolved"],
        under_witnessed=False,
        unreconciled=False,
    )
    assert outcome == "held-for-review"
    assert "audit re-proof cap" in reason


def test_a_not_run_perlectio_has_no_audit_chain_and_is_not_a_traceback():
    """The absent-chair Perlectio the Recensor is built to hold, not crash on.

    `pipeline/4_perlector/run.py` publishes `not-run` for a Designator-held act
    and for an explicitly absent Perlector chair (`state = "absent"` on the
    `perlector` chair in `config/models.toml`), and that record carries no
    `text` at all. Recensor `main` calls `audit_state` before it classifies the
    outcome, so demanding a Pass-C chain of every reading turned the absent
    chair's explicit hold — the shape the neighbouring `basis_regions` comment
    refuses to index for exactly this reason — into a `SchemaRefusal` about
    missing final text. Held acts are `continue`d earlier; the absent chair is
    not, so this is the path that reached it.
    """
    not_run = {
        "outcome": "not-run",
        "payload": {
            "act_key": "a1",
            "attempt_ordinal": 1,
            "reason": "the Perlector chair is explicitly absent: withdrawn between runs",
            "basis": {"regions": [], "testimonia": []},
            "dissent": [],
            "provenance": {"chair_state": "absent"},
        },
        "inputs": [],
    }
    # `tree=None` is the assertion: no artifact is read for a reading that never
    # produced one, so the refusal cannot come from a lookup that half-ran.
    # `None`, never `False`: no audit exists, which is a different recorded
    # fact from "audited, resolved".
    assert (
        load_stage("5_recensor").audit_state(SimpleNamespace(tree=None), not_run, "act-1") is None
    )


# The three-character reading every `_finding` unit test validates against, and
# the geometry its re-proof termination is measured over. Since 2026-09-14 the
# shared validator binds `measure.characters` to the reading the record was
# measured over and re-derives `length_suspicious` from the block, so a fixture
# pairing a 34-character measurement with a three-character text would be
# refused for exactly the reason the binding exists (independent audit of
# 2026-09-14). A small region on a whole page keeps the length signal clean at
# these lengths, so each test still exercises the property it is about.
_FINDING_TEXT = "abc"
_FINDING_MEASURE = {
    "region_pixels": 1_000,
    "page_pixels": 52_000,
    "characters": len(_FINDING_TEXT),
    "length_floor_characters_per_page": _FLOOR,
    **_gate_terms(52_000),
}


def _termination_over(record: dict | None, text: str) -> dict | None:
    """The same termination record, re-measured over `text`."""
    if record is None:
        return None
    return {**record, "measure": {**_FINDING_MEASURE, "characters": len(text)}}


def _finding(**overrides) -> dict:
    base = {
        "act_key": "a1",
        "attempt_ordinal": 1,
        "page_ids": ["p1"],
        "round_cap": 1,
        "policy": {"schema": "perlector-audit.v3", "sha256": "0" * 64, "approval_ref": ""},
        "flags": [{"class": "testimony-diff", "location": {"start": 0, "end": 3}}],
        "change_record": [],
        "uncertain_spans": [],
        "unresolved": True,
        "examination": "incomplete",
        "reproof_truncation": _CUT_OFF_TRUNCATION,
        "reproof_call": None,
        "reproof_edits": [
            {
                "class": "testimony-diff",
                "location": {"start": 0, "end": 3},
                "original": "abc",
                "replacement": "abc",
            }
        ],
    }
    finding = {**base, **overrides}
    if finding["reproof_truncation"] is None and "reproof_edits" not in overrides:
        finding["reproof_edits"] = None
    finding["reproof_truncation"] = _termination_over(finding["reproof_truncation"], _FINDING_TEXT)
    return finding


def test_an_audit_finding_cannot_call_a_cut_off_reproof_complete():
    """The shared validator re-derives the examination; the producer cannot choose it."""
    # The honest record of a cut-off re-proof validates.
    assert (
        perlector_audit.validate_finding(_finding(), text="abc", flag_text="abc")["unresolved"]
        is True
    )

    with pytest.raises(SchemaRefusal, match="make it 'incomplete'"):
        perlector_audit.validate_finding(
            _finding(examination="complete", unresolved=False), text="abc", flag_text="abc"
        )
    with pytest.raises(SchemaRefusal, match="unresolved state contradicts its examination"):
        perlector_audit.validate_finding(_finding(unresolved=False), text="abc", flag_text="abc")
    with pytest.raises(SchemaRefusal, match="cap was not exhausted"):
        perlector_audit.validate_finding(
            _finding(
                uncertain_spans=[{"start": 0, "end": 3, "reason": "audit-round-cap-exhausted"}]
            ),
            text="abc",
            flag_text="abc",
        )
    # A completed re-proof is resolved -- and only then.
    assert (
        perlector_audit.validate_finding(
            _finding(
                examination="complete", unresolved=False, reproof_truncation=_COMPLETE_TRUNCATION
            ),
            text="abc",
            flag_text="abc",
        )["examination"]
        == "complete"
    )
    # The three shapes that cannot exist: a termination where nothing was due,
    # a termination the exhausted cap could not have delivered, and a due
    # re-proof with no termination at all.
    with pytest.raises(SchemaRefusal, match="raised no flag"):
        perlector_audit.validate_finding(
            _finding(flags=[], examination="not-due", unresolved=False, reproof_edits=[]),
            text="abc",
            flag_text="abc",
        )
    with pytest.raises(SchemaRefusal, match="left no round to deliver"):
        perlector_audit.validate_finding(
            _finding(round_cap=0, examination="cap-exhausted"), text="abc", flag_text="abc"
        )
    with pytest.raises(SchemaRefusal, match="records no termination"):
        perlector_audit.validate_finding(
            _finding(reproof_truncation=None), text="abc", flag_text="abc"
        )
    with pytest.raises(SchemaRefusal, match="unknown truncation classification"):
        perlector_audit.validate_finding(
            _finding(reproof_truncation={**_CUT_OFF_TRUNCATION, "classification": "fine"}),
            text="abc",
            flag_text="abc",
        )

    reference = {"relative_path": "4_perlector/audit.json", "sha256": "0" * 64}
    with pytest.raises(SchemaRefusal, match="contradicts its examination state"):
        perlector_audit.validate_perlectio_audit(
            {
                "draft_ref": reference,
                "finding_ref": reference,
                "finding_digest": "0" * 64,
                "unresolved": False,
                "examination": "incomplete",
                "reproofs": [],
                "request_digest": None,
            },
            text_length=3,
        )


def test_a_v2_audit_finding_is_refused_by_name():
    finding = _finding(policy={"schema": LEGACY_SCHEMA, "sha256": "0" * 64, "approval_ref": ""})
    with pytest.raises(SchemaRefusal, match="sealed under perlector-audit.v2"):
        perlector_audit.validate_finding(finding, text="abc", flag_text="abc")


def test_a_v1_audit_record_is_refused_by_name_and_never_read_forward():
    """The old schema could not carry F1's fact; its silence is not evidence."""
    v1_policy = {"schema": "perlector-audit.v1", "sha256": "0" * 64, "approval_ref": ""}
    with pytest.raises(SchemaRefusal, match="sealed under perlector-audit.v1") as refused:
        perlector_audit.validate_finding(_finding(policy=v1_policy), text="abc", flag_text="abc")
    assert "could not record whether a delivered re-proof completed" in str(refused.value)
    assert "stay as written" in str(refused.value)
    with pytest.raises(SchemaRefusal, match="sealed under perlector-audit.v1"):
        perlector_audit.validate_draft(
            {
                "act_key": "a1",
                "attempt_ordinal": 1,
                "semi_final_text": "abc",
                "page_ids": ["p1"],
                "round_cap": 1,
                "policy": v1_policy,
                "flags": [],
                "flag_location_basis": [],
            }
        )
    assert perlector_audit.RETIRED_SCHEMAS == frozenset({"perlector-audit.v1"})
    assert perlector_audit.SCHEMA == "perlector-audit.v3"


# --- The independent review of candidate 0934c057: forged terminations and delivery facts


def test_a_sealed_termination_whose_verdict_contradicts_its_signals_is_refused():
    """Blocking finding 1: the classification is a function of the four sealed signals."""
    forged_complete = {**_CUT_OFF_TRUNCATION, "classification": "complete"}
    with pytest.raises(SchemaRefusal, match="own signals make it 'truncated'"):
        perlector_audit.validate_finding(
            _finding(examination="complete", unresolved=False, reproof_truncation=forged_complete),
            text="abc",
            flag_text="abc",
        )
    silent = {
        "classification": "complete",
        "signals": {**_COMPLETE_TRUNCATION["signals"], "stop_reason_declared": None},
        "measure": dict(_COMPLETE_TRUNCATION["measure"]),
    }
    with pytest.raises(SchemaRefusal, match="own signals make it 'unknown'"):
        validate_truncation_record(silent, label="a test record")
    with pytest.raises(SchemaRefusal, match="declares stop reason 'banana'"):
        validate_truncation_record(
            {
                "classification": "complete",
                "signals": {**_COMPLETE_TRUNCATION["signals"], "stop_reason_declared": "banana"},
                "measure": dict(_COMPLETE_TRUNCATION["measure"]),
            },
            label="a test record",
        )
    # Three suspicious computed signals under a clean stop are `truncated`; one is `unknown`.
    three = {
        "classification": "truncated",
        "signals": {
            "stop_reason_declared": "stop",
            "unclosed_structure": True,
            "length_suspicious": True,
            "ends_abruptly": True,
        },
        # A whole 300-DPI leaf returning sixteen characters really is
        # length-suspicious under the sealed floor; the validator re-derives that
        # signal from this block, so the record has to mean what its signals say.
        "measure": {
            **_COMPLETE_TRUNCATION["measure"],
            "region_pixels": 2550 * 3300,
            "page_pixels": 2550 * 3300,
            **_gate_terms(2550 * 3300),
        },
    }
    assert validate_truncation_record(three, label="x")["classification"] == "truncated"
    one = {
        "classification": "unknown",
        "signals": {**_COMPLETE_TRUNCATION["signals"], "ends_abruptly": True},
        "measure": dict(_COMPLETE_TRUNCATION["measure"]),
    }
    assert validate_truncation_record(one, label="x")["classification"] == "unknown"
    # The instrument decides with the same shared rule.
    measured = truncation.classify(
        "alpha beta-",
        region_pixels=_TEST_REGION_PIXELS,
        page_pixels=_TEST_PAGE_PIXELS,
        truncation_policy=_TRUNCATION_POLICY,
        stop_reason="stop",
    )
    assert measured["classification"] == truncation_classification(measured["signals"])


def test_a_perlectio_audit_record_must_agree_with_its_own_delivery_facts():
    """Finding 6: a standalone reader refuses a completed re-proof that was never requested."""
    reference = {"relative_path": "4_perlector/audit.json", "sha256": "0" * 64}
    plan = [
        {
            "class": "testimony-diff",
            "location": {"start": 0, "end": 1},
            "prompt": neutral_prompt(start=0, end=1, text_length=1),
        }
    ]

    def record(**overrides):
        base = {
            "draft_ref": reference,
            "finding_ref": reference,
            "finding_digest": "0" * 64,
            "unresolved": False,
            "examination": "complete",
            "reproofs": plan,
            "request_digest": "1" * 64,
        }
        return {**base, **overrides}

    assert (
        perlector_audit.validate_perlectio_audit(record(), text_length=1)["examination"]
        == "complete"
    )
    with pytest.raises(SchemaRefusal, match="contradicts its delivery"):
        perlector_audit.validate_perlectio_audit(record(request_digest=None), text_length=1)
    with pytest.raises(SchemaRefusal, match="contradicts its re-proof plan"):
        perlector_audit.validate_perlectio_audit(record(reproofs=[]), text_length=1)
    with pytest.raises(SchemaRefusal, match="contradicts its re-proof plan"):
        perlector_audit.validate_perlectio_audit(
            record(examination="not-due", request_digest=None), text_length=1
        )
    assert (
        perlector_audit.validate_perlectio_audit(
            record(examination="not-due", reproofs=[], request_digest=None), text_length=1
        )["examination"]
        == "not-due"
    )
    with pytest.raises(SchemaRefusal, match="perlector-audit.v1 field set"):
        perlector_audit.validate_perlectio_audit(
            {key: value for key, value in record().items() if key != "examination"},
            text_length=1,
        )


def test_the_recensor_routes_on_the_examination_and_refuses_a_contradicting_boolean():
    """Finding 4: the boolean is derived from the examination; where both arrive they agree."""
    recensor = load_stage("5_recensor")
    with pytest.raises(ContractError, match="derives True"):
        recensor.review_route_from_findings(
            testimony_shortfall=False,
            audit_unresolved=False,
            audit_examination="incomplete",
            under_witnessed=False,
        )
    outcome, reason = recensor.review_route_from_findings(
        testimony_shortfall=False,
        audit_unresolved=None,
        audit_examination="incomplete",
        audit_reproof_truncation=_CUT_OFF_TRUNCATION,
        under_witnessed=False,
    )
    assert outcome == "held-for-review"
    # Finding 9: the reason names the instrument's verdict and its signals, not an
    # engine statement the instrument may never have received.
    assert "classified the re-proof call 'truncated'" in reason
    assert "engine stop word 'length'" in reason
    assert "computed signals raised: none" in reason
    assert "audit re-proof cap" not in reason
    assert (
        recensor.review_route_from_findings(
            testimony_shortfall=False,
            audit_unresolved=None,
            audit_examination="complete",
            under_witnessed=False,
        )
        is None
    )


def test_a_sealed_reproof_call_must_name_the_digest_of_the_response_it_retains():
    """The two digests are one fact stated twice."""
    reference = {"relative_path": "4_perlector/blobs/sha256/" + "a" * 64, "sha256": "a" * 64}
    request = perlector_audit.audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/audit-draft/a1.json", "sha256": "a" * 64},
        semi_final_text="abc",
        flags=[{"class": "testimony-diff", "location": {"start": 0, "end": 3}}],
    )
    base_text = "Read the ink"
    request_sha256 = "d" * 64
    prompt = perlector_audit.audit_prompt_evidence(
        base_prompt={"rendered_sha256": digest_bytes(base_text.encode("utf-8"))},
        base_text=base_text,
        request=request,
        request_sha256=request_sha256,
        rendered_text=base_text + "\n" + render_reproof_instruction(request),
    )
    call = {
        "call_record_ref": {
            "relative_path": "4_perlector/blobs/sha256/" + "b" * 64,
            "sha256": "b" * 64,
        },
        "raw_response_ref": reference,
        "response_sha256": "a" * 64,
        "finish_reason": "length",
        "served_model_id": "perlector-under-test",
        "request_sha256": request_sha256,
        "audit_prompt": prompt,
    }
    finding = _finding(reproof_call=call)
    assert (
        perlector_audit.validate_finding(finding, text="abc", flag_text="abc")["reproof_call"]
        == call
    )
    # The retained call's finish reason and the sealed verdict's stop word are
    # one fact: a `stop` call beside a `length` termination is refused.
    with pytest.raises(SchemaRefusal, match="finished 'stop' but its sealed termination"):
        perlector_audit.validate_finding(
            _finding(reproof_call={**call, "finish_reason": "stop"}), text="abc", flag_text="abc"
        )
    with pytest.raises(SchemaRefusal, match="no reader maps to a stop word"):
        perlector_audit.validate_finding(
            _finding(reproof_call={**call, "finish_reason": "eos"}), text="abc", flag_text="abc"
        )
    assert (
        perlector_audit.validate_finding(
            _finding(
                examination="complete",
                unresolved=False,
                reproof_truncation=_COMPLETE_TRUNCATION,
                reproof_call={**call, "finish_reason": "stop"},
            ),
            text="abc",
            flag_text="abc",
        )["examination"]
        == "complete"
    )
    with pytest.raises(SchemaRefusal, match="names response digest"):
        perlector_audit.validate_finding(
            _finding(reproof_call={**call, "response_sha256": "c" * 64}),
            text="abc",
            flag_text="abc",
        )
    with pytest.raises(SchemaRefusal, match="although no re-proof was delivered"):
        perlector_audit.validate_finding(
            _finding(
                flags=[],
                examination="not-due",
                unresolved=False,
                reproof_truncation=None,
                reproof_call=call,
                reproof_edits=None,
            ),
            text="abc",
            flag_text="abc",
        )


@pytest.mark.parametrize("bad", [["complete"], {"state": "complete"}, 7])
def test_an_unhashable_or_non_string_vocabulary_value_is_refused_by_name_not_typeerror(bad):
    """A list or object at a frozenset check must be a refusal, not a TypeError."""
    with pytest.raises(SchemaRefusal, match="unknown truncation classification"):
        validate_truncation_record(
            {**_COMPLETE_TRUNCATION, "classification": bad}, label="a test record"
        )
    with pytest.raises(SchemaRefusal, match="malformed sealed policy reference"):
        perlector_audit.validate_finding(
            _finding(policy={"schema": bad, "sha256": "0" * 64, "approval_ref": ""}),
            text="abc",
            flag_text="abc",
        )
    reference = {"relative_path": "4_perlector/audit.json", "sha256": "0" * 64}
    with pytest.raises(SchemaRefusal, match="unknown examination state"):
        perlector_audit.validate_perlectio_audit(
            {
                "draft_ref": reference,
                "finding_ref": reference,
                "finding_digest": "0" * 64,
                "unresolved": False,
                "examination": bad,
                "reproofs": [],
                "request_digest": None,
            },
            text_length=1,
        )
    with pytest.raises(SchemaRefusal, match="is not an audit examination state"):
        perlector_audit.unresolved_state(bad)


def test_the_validator_that_refused_the_unmeasured_emptying_still_does():
    """The behavioural half: the binding the branch above has to satisfy."""
    from common import truncation

    region, page = 160 * 80, 200 * 260
    policy = protocol.load(ROOT / "config" / "perlector_protocol.toml")[0][
        protocol.TRUNCATION_TABLE
    ]
    whitespace = truncation.classify(
        "   ", region_pixels=region, page_pixels=page, truncation_policy=policy, stop_reason="stop"
    )
    emptied = truncation.classify(
        "", region_pixels=region, page_pixels=page, truncation_policy=policy, stop_reason="stop"
    )
    assert whitespace["measure"]["characters"] == 3
    assert emptied["measure"]["characters"] == 0

    with pytest.raises(SchemaRefusal, match="characters but the text"):
        validate_truncation_record(
            whitespace, label="an audit finding's re-proof termination", text=""
        )
    assert (
        validate_truncation_record(
            emptied, label="an audit finding's re-proof termination", text=""
        )
        == emptied
    )
