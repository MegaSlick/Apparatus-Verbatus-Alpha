"""R5b Pass-C proof: one frozen page pass, neutral re-proof, and review routing."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import audit
import pytest

from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def _recensor():
    spec = importlib.util.spec_from_file_location(
        "r5b_recensor_consumer", ROOT / "pipeline" / "5_recensor" / "run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _perlector():
    spec = importlib.util.spec_from_file_location(
        "r5b_perlector_schema", ROOT / "pipeline" / "4_perlector" / "run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _records(tree: RunTree, kind: str) -> list[dict]:
    return [
        tree.read_artifact(PERLECTOR, kind, entry["artifact_id"])
        for entry in tree.build_manifest(PERLECTOR)["artifacts"]
        if entry["kind"] == kind
    ]


def test_fixture_produces_each_audit_kind_and_records_unchanged_reproof(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    drafts = _records(tree, "audit-draft")
    findings = _records(tree, "audit-finding")
    finals = _records(tree, "perlectio")
    assert len(drafts) == len(findings) == len(finals) == 2
    assert all(record["payload"]["flags"] for record in drafts)
    assert all(record["payload"]["change_record"] == [] for record in findings)
    assert all(record["payload"]["unresolved"] is False for record in findings)
    for final in finals:
        for reproof in final["payload"]["audit"]["reproofs"]:
            prompt = reproof["prompt"].lower()
            assert "wrong" not in prompt
            assert "expected" not in prompt
            assert "confirmed unchanged" in prompt


def test_fixture_exercises_a_changed_reproof_with_its_triggering_flag_class(tmp_path):
    result = _run(tmp_path / "runs", scenario="audit-change")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    changed = next(
        record for record in _records(tree, "audit-finding") if record["payload"]["change_record"]
    )
    assert changed["payload"]["change_record"][0]["triggering_flag_class"] == "testimony-diff"
    final = next(
        record
        for record in _records(tree, "perlectio")
        if record["subject_id"] == changed["subject_id"]
    )
    prior = next(
        record
        for record in _records(tree, "lectio-prior")
        if record["subject_id"] == changed["subject_id"]
    )
    draft = tree.read_artifact_reference(
        final["payload"]["audit"]["draft_ref"],
        stage=PERLECTOR,
        kind="audit-draft",
        subject_id=changed["subject_id"],
    )
    assert final["payload"]["text"] != draft["payload"]["semi_final_text"]
    # The exact declared result, not merely "something changed": any wrong
    # changed text would otherwise pass this end-to-end pin.
    assert final["payload"]["text"] == "SYNTHETIC ACT ONE alpha beta gamma!"
    perlector = _perlector()
    assert final["payload"]["self_revision"] == perlector.departures(
        final["payload"]["text"], prior["payload"]["text"]
    )
    assert final["payload"]["self_revision"] != perlector.departures(
        draft["payload"]["semi_final_text"], prior["payload"]["text"]
    )
    agreeing_witness = next(
        row for row in final["payload"]["dissent"] if row["chair"] == "attestator_1"
    )
    assert [departure["reading_span"] for departure in agreeing_witness["departures"]] == [
        {
            "start": len(draft["payload"]["semi_final_text"]),
            "end": len(final["payload"]["text"]),
        }
    ]


def test_perlectio_schema_refuses_a_directional_reproof_prompt(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    final = _records(RunTree(tmp_path / "runs", "r"), "perlectio")[0]
    payload = copy.deepcopy(final["payload"])
    payload["audit"]["reproofs"][0]["prompt"] = "The reading is wrong; replace it with gamma."
    perlector = _perlector()
    # Thread the sealed protocol: since R5a, a payload carrying a protocol
    # record refuses an unthreaded validation call before this test's own
    # boundary is reached.
    protocol_config, protocol_sha256 = perlector.protocol.load(
        ROOT / "config" / "perlector_protocol.toml"
    )
    with pytest.raises(SchemaRefusal, match="neutral location-only"):
        perlector.validate_reading_payload(
            payload,
            outcome="read",
            fields=perlector._PERLECTIO_FIELDS,
            protocol_config=protocol_config,
            protocol_sha256=protocol_sha256,
        )


def test_flags_are_frozen_once_per_page_and_never_cascade_from_a_reproof():
    frozen = [
        {
            "act_id": "a1",
            "page_id": "p1",
            "order": 0,
            "geometry_order": 0,
            "text": "No 2 1689 alpha",
            "testimonia": ["No 2 1689 beta"],
            "within_crop": True,
        },
        {
            "act_id": "a2",
            "page_id": "p1",
            "order": 1,
            "geometry_order": 1,
            "text": "No 1 1688 gamma",
            "testimonia": ["No 1 1688 gamma"],
            "within_crop": True,
        },
    ]
    flags = audit.flags_once_per_page(frozen)
    assert {flag["class"] for flag in flags["a2"]} == {"date-sequence", "numbering"}
    # A hypothetical re-proof result modifies a1 only.  The already-frozen a2
    # locations are the audit plan; no call recomputes them over changed text.
    changed = copy.deepcopy(frozen)
    changed[0]["text"] = "No 2 1600 beta"
    assert flags["a2"] == audit.flags_once_per_page(frozen)["a2"]
    assert audit.flags_once_per_page(changed)["a2"] != flags["a2"]


def test_recovery_flag_pass_merges_sibling_rows_before_the_page_calculation(monkeypatch):
    """The merge rule alone: a one-act recovery still evaluates cross-act flags
    over its whole page. The sibling rows are supplied here rather than read --
    `test_recovery_sibling_context_is_sealed_and_never_republished` below is
    what proves they come from sealed artifacts, so deleting that test is what
    would lose the sealing coverage, not this one."""
    perlector = _perlector()
    recovered = [
        {
            "act_id": "a2",
            "page_id": "p1",
            "order": 1,
            "geometry_order": (20, 0),
            "text": "No 1 1688 recovered",
            "testimonia": ["No 1 1688 recovered"],
            "within_crop": True,
        }
    ]
    sibling = {
        "act_id": "a1",
        "page_id": "p1",
        "order": 0,
        "geometry_order": (10, 0),
        "text": "No 2 1689 sibling",
        "testimonia": ["No 2 1689 sibling"],
        "within_crop": True,
    }
    monkeypatch.setattr(
        perlector,
        "_sealed_sibling_semi_finals",
        lambda *_args, **_kwargs: [sibling],
    )

    flags = perlector._page_flags(
        object(),
        recovered,
        expected=[],
        recovery_act_id="a2",
    )

    assert {flag["class"] for flag in flags["a2"]} == {"date-sequence", "numbering"}


def test_recovery_sibling_context_is_sealed_and_never_republished(tmp_path):
    result = _run(tmp_path / "runs", scenario="review")
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    perlector = _perlector()
    readings = _records(tree, "perlectio")
    recovered = next(record for record in readings if record["payload"]["attempt_ordinal"] == 2)
    sibling = next(
        record
        for record in readings
        if record["subject_id"] != recovered["subject_id"]
        and record["payload"]["attempt_ordinal"] == 1
    )
    page_id = recovered["payload"]["basis"]["regions"][0]["source_page_id"]
    expected = [
        {"act_id": recovered["subject_id"], "page_id": page_id},
        {"act_id": sibling["subject_id"], "page_id": page_id},
    ]
    context = SimpleNamespace(tree=tree, config_digest=tree.read_run()["config_digest"])
    before = [entry for entry in tree.build_manifest(PERLECTOR)["artifacts"]]

    protocol_config, protocol_sha256 = perlector.protocol.load(
        ROOT / "config" / "perlector_protocol.toml"
    )
    merged = perlector._sealed_sibling_semi_finals(
        context,
        [{"act_id": recovered["subject_id"], "page_id": page_id}],
        expected=expected,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )

    assert [row["act_id"] for row in merged] == [sibling["subject_id"]]
    assert merged[0]["text"] == sibling["payload"]["text"]
    assert tree.build_manifest(PERLECTOR)["artifacts"] == before

    sibling_path = tree.resolve(tree.artifact_path(PERLECTOR, "perlectio", sibling["artifact_id"]))
    corrupted = json.loads(sibling_path.read_text())
    corrupted["payload"]["text"] += " corrupted"
    sibling_path.write_text(json.dumps(corrupted))
    with pytest.raises(SchemaRefusal, match="fails its self-hash"):
        perlector._sealed_sibling_semi_finals(
            context,
            [{"act_id": recovered["subject_id"], "page_id": page_id}],
            expected=expected,
        )


def test_a_degenerate_digit_run_flags_instead_of_ending_the_stage():
    """`int()` on the numbering capture was a `ValueError` waiting on the reader.

    `(\\d+)` is unbounded and the text is whatever the reader emitted, so a run
    of more than 4300 digits hit CPython's integer string-conversion limit and
    ended the Perlector mid-page with an unnamed traceback -- for every act on
    that page, not just the one carrying the run. The comparison is the same
    ordering it always was; only the representation changed.
    """
    degenerate = "No " + "9" * 5000 + " alpha"
    frozen = [
        {
            "act_id": "a1",
            "page_id": "p1",
            "order": 0,
            "geometry_order": 0,
            "text": degenerate,
            "testimonia": [degenerate],
            "within_crop": True,
        },
        {
            "act_id": "a2",
            "page_id": "p1",
            "order": 1,
            "geometry_order": 1,
            "text": "No 7 beta",
            "testimonia": ["No 7 beta"],
            "within_crop": True,
        },
    ]
    flags = audit.flags_once_per_page(frozen)
    assert [flag["class"] for flag in flags["a2"]] == ["numbering"]
    assert flags["a1"] == []

    # Ordering is unchanged for every number a register actually carries,
    # leading zeros included.
    assert audit._numeric_key("007") < audit._numeric_key("8")
    assert audit._numeric_key("9") < audit._numeric_key("10")
    assert audit._numeric_key("0") == audit._numeric_key("000")
    assert audit._numeric_key("1688") < audit._numeric_key("1689")


def test_change_record_refuses_a_change_extending_past_the_flag_end():
    flags = [{"class": "testimony-diff", "location": {"start": 1, "end": 2}}]
    with pytest.raises(SchemaRefusal, match="outside every flagged location"):
        audit.change_record("abcd", "aXYZ", flags)


def test_change_record_names_the_narrowest_flag_that_located_the_change():
    """The triggering class is the soft-picker measurement, not decoration.

    Every cross-act flag class spans the whole act from offset 0, so any act
    that carries one contains every narrower flag too. Attributing by list
    order made the widest flag win and recorded a correction that sits squarely
    inside a `testimony-diff` span as `date-sequence` — the one change the
    "moved toward the witness" measurement is looking for, filed under a class
    that has nothing to do with testimony.
    """
    text = "No 1 1688 alpha beta gamma"
    flags = [
        # Exactly the order `flags_once_per_page` emits: sorted by (start, class).
        {"class": "date-sequence", "location": {"start": 0, "end": len(text)}},
        {"class": "testimony-diff", "location": {"start": 21, "end": 26}},
    ]
    changes = audit.change_record(text, "No 1 1688 alpha beta gamna", flags)
    assert changes == [{"start": 24, "end": 25, "triggering_flag_class": "testimony-diff"}]

    # A change the narrow flag does not cover still belongs to the wide one.
    whole_act = audit.change_record(text, "No 1 1687 alpha beta gamma", flags)
    assert whole_act == [{"start": 8, "end": 9, "triggering_flag_class": "date-sequence"}]


def test_an_audit_round_cap_above_one_is_refused_because_no_second_round_exists(tmp_path):
    """A sealed cap of 2 with Tyrel's reference would be recorded but never run."""
    approved = tmp_path / "approved.toml"
    approved.write_text(
        'schema = "perlector-audit.v1"\n'
        "default_round_cap = 1\n"
        "absolute_round_cap = 2\n"
        "round_cap = 2\n"
        'approval_ref = "tyrel-2026-08-16-raised-audit-cap"\n'
    )
    with pytest.raises(ContractError, match="exactly one span-scoped audit re-proof"):
        audit.load(approved)


def test_an_audit_changed_text_is_re_measured_by_the_truncation_instrument():
    """The last derived field that still described the pre-audit reading.

    `self_revision` and `dissent` are recomputed when Pass C changes the text
    (H6); `truncation` was not, and `outcome` is derived from it — so an
    audit-changed reading published the three text-computed signals of a text
    nobody established, and the re-proof's own engine stop reason was dropped
    on the floor.
    """
    perlector = _perlector()
    complete = {
        "classification": "complete",
        "signals": {
            "stop_reason_declared": "stop",
            "unclosed_structure": False,
            "length_suspicious": False,
            "ends_abruptly": False,
        },
    }

    # The re-proof's own generation ran out of budget: its text is what gets
    # published, so the record it is published under is the re-proof's.
    cut_off = perlector._audited_truncation(
        pass_b=complete,
        declared_failure=None,
        text="alpha beta gamma",
        region_pixels=18612,
        stop_reason="length",
    )
    assert cut_off["classification"] == "truncated"
    assert cut_off["signals"]["stop_reason_declared"] == "length"
    assert (
        perlector._resolve_outcome(
            declared_failure=None, truncation_record=cut_off, text="alpha beta gamma"
        )
        == "truncated"
    )

    # A clean engine, but the change left the text ending mid-token: the
    # computed signals are measured over the published text, not the draft.
    abrupt = perlector._audited_truncation(
        pass_b=complete,
        declared_failure=None,
        text="alpha beta gamma-",
        region_pixels=18612,
        stop_reason="stop",
    )
    assert abrupt["signals"]["ends_abruptly"] is True
    assert abrupt["classification"] == "unknown"

    # Pass C is span-scoped, so a clean re-proof can never clear a Pass-B
    # truncation: the signals are re-measured, the verdict is not improved.
    was_truncated = {**complete, "classification": "truncated"}
    kept = perlector._audited_truncation(
        pass_b=was_truncated,
        declared_failure=None,
        text="alpha beta gamma",
        region_pixels=18612,
        stop_reason="stop",
    )
    assert kept["classification"] == "truncated"
    assert kept["signals"]["ends_abruptly"] is False


def test_raised_cap_needs_tyrels_reference_and_exhaustion_routes_review(tmp_path):
    raised = tmp_path / "raised.toml"
    raised.write_text(
        'schema = "perlector-audit.v1"\ndefault_round_cap = 1\nabsolute_round_cap = 2\nround_cap = 2\napproval_ref = ""\n'
    )
    with pytest.raises(ContractError, match="Tyrel's approval reference"):
        audit.load(raised)

    exhausted = tmp_path / "exhausted.toml"
    exhausted.write_text(
        'schema = "perlector-audit.v1"\ndefault_round_cap = 1\nabsolute_round_cap = 2\nround_cap = 0\napproval_ref = ""\n'
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


def test_recensor_refuses_a_forged_audit_reference(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = _records(tree, "perlectio")[0]
    forged = copy.deepcopy(final)
    forged["payload"]["audit"]["finding_ref"] = forged["payload"]["audit"]["draft_ref"]

    with pytest.raises(SchemaRefusal, match="not required 'perlector'/'audit-finding'"):
        _recensor().audit_state(SimpleNamespace(tree=tree), forged, final["subject_id"])


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
    assert _recensor().audit_state(SimpleNamespace(tree=None), not_run, "act-1") is False


def test_the_order_flag_fires_from_real_crop_geometry_not_the_declared_order():
    """H1's repair, red-proved: `geometry_order` must be an independent fact.

    While the production wiring set `geometry_order = row["order"]`, declared and
    geometric order agreed by construction and the `order` class could never
    fire — a check that reports agreement it manufactured itself (R0 freeze note
    #2, the derived-record pattern). Nothing in the suite noticed the difference,
    so the repair is pinned here: two acts declared in the seal's order but cut
    the other way up the page must both raise `order`.
    """
    perlector = _perlector()

    def row(act_id, *, order, y):
        return perlector._audit_semi_final(
            act_id=act_id,
            page_id="p1",
            order=order,
            text=f"text of {act_id}",
            regions=[
                {"source_page_id": "p1", "transform": {"bounds": {"x": 12, "y": y, "w": 9, "h": 9}}}
            ],
            dossier={"testimonia": []},
        )

    # Declared first, but cut lower down the page than the act declared second.
    first = row("a1", order=0, y=200)
    second = row("a2", order=1, y=10)
    assert (first["geometry_order"], second["geometry_order"]) == ((200, 12), (10, 12))

    flags = audit.flags_once_per_page([first, second])
    assert [flag["class"] for flag in flags["a1"]] == ["order"]
    assert [flag["class"] for flag in flags["a2"]] == ["order"]

    # Same acts, cut in their declared order: the class stays silent.
    agreeing = audit.flags_once_per_page([row("a1", order=0, y=10), row("a2", order=1, y=200)])
    assert agreeing == {"a1": [], "a2": []}


def test_the_chain_refuses_an_audit_pair_belonging_to_another_act(tmp_path):
    """`act_key` is a restatement; `subject_id` is the binding that holds.

    Two acts on one page can carry the same `act_key` text — the fixture's keys
    are distinct, but nothing in the seal makes them globally unique — so the
    draft/finding restatement equality `validate_chain` checks cannot tell one
    act's audit pair from another's. What can is the envelope subject the
    reference resolves to, and that is the check pinned here.
    """
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    finals = _records(tree, "perlectio")
    mine, theirs = finals[0], finals[1]
    assert mine["subject_id"] != theirs["subject_id"]

    forged = copy.deepcopy(mine)
    forged["payload"]["audit"] = copy.deepcopy(theirs["payload"]["audit"])
    forged["inputs"] = theirs["inputs"]
    with pytest.raises(SchemaRefusal, match="not required '" + mine["subject_id"] + "'"):
        audit.validate_chain(tree, forged, mine["subject_id"])


def test_the_chain_refuses_a_second_attempt_that_reuses_the_first_attempts_audit(tmp_path):
    """One audit draft and finding per attempt, bound to that attempt's ordinal.

    A recovery reread reads a new crop, so its Pass-C flags are located in a new
    semi-final. Letting attempt 2's Perlectio point back at attempt 1's pair
    would republish the superseded audit as the current one — and the pair is
    the only evidence the Recensor routes on.
    """
    result = _run(tmp_path / "runs", scenario="review")
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    by_attempt = {}
    for record in _records(tree, "perlectio"):
        by_attempt.setdefault(record["subject_id"], {})[record["payload"]["attempt_ordinal"]] = (
            record
        )
    recovered = next(records for records in by_attempt.values() if len(records) == 2)

    reused = copy.deepcopy(recovered[2])
    reused["payload"]["audit"] = copy.deepcopy(recovered[1]["payload"]["audit"])
    reused["inputs"] = recovered[1]["inputs"]
    with pytest.raises(SchemaRefusal, match="disagrees with its audit identity"):
        audit.validate_chain(tree, reused, reused["subject_id"])


def test_the_chain_refuses_perlectio_uncertainty_the_finding_did_not_establish(tmp_path):
    """The Perlectio layer projects the finding's spans; it may not add its own.

    R8 reconciles these two layers, so the projection is the seam that has to
    hold: an export-facing `uncertain_spans` entry with no exhausted-cap finding
    behind it would arrive at the canonical layer as uncertainty nobody measured.
    """
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = _records(tree, "perlectio")[0]
    assert final["payload"]["uncertain_spans"] == []

    invented = copy.deepcopy(final)
    invented["payload"]["uncertain_spans"] = [
        {"start": 0, "end": 3, "alternatives": [], "confidence": "low"}
    ]
    with pytest.raises(SchemaRefusal, match="audit uncertainty projection"):
        audit.validate_chain(tree, invented, final["subject_id"])


def test_shared_chain_refuses_draft_finding_restatement_drift(tmp_path):
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = _records(tree, "perlectio")[0]
    draft = tree.read_artifact_reference(
        final["payload"]["audit"]["draft_ref"],
        stage=PERLECTOR,
        kind="audit-draft",
        subject_id=final["subject_id"],
    )
    finding = tree.read_artifact_reference(
        final["payload"]["audit"]["finding_ref"],
        stage=PERLECTOR,
        kind="audit-finding",
        subject_id=final["subject_id"],
    )
    drifted_finding = copy.deepcopy(finding)
    drifted_finding["payload"]["page_id"] = "pg_drifted"
    audit.validate_finding(drifted_finding["payload"], text=final["payload"]["text"])

    class DriftedTree:
        def read_artifact_reference(self, _reference, *, stage, kind, subject_id):
            assert stage == PERLECTOR and subject_id == final["subject_id"]
            return draft if kind == "audit-draft" else drifted_finding

    with pytest.raises(SchemaRefusal, match="restate different frozen facts"):
        audit.validate_chain(DriftedTree(), final, final["subject_id"])
