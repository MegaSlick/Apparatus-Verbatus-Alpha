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
import reader as reader_module

from common.contracts.canonical import digest_of
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"


def test_witness_derived_location_classes_remain_the_one_open_class():
    """Location provenance does not make page-boundary evidence a text flag.

    Do not widen this set for boundary disagreement: that is Recensor page
    evidence, not a witness-derived text location.
    """
    assert audit.WITNESS_DERIVED_LOCATION_CLASSES == frozenset({"testimony-diff"})


def test_flag_location_basis_names_only_witnesses_that_located_a_frozen_diff():
    """An agreeing witness is evidence, but it did not locate the diff flag."""
    perlector = _perlector()
    text = "alpha beta"
    flags = [{"class": "testimony-diff", "location": {"start": 6, "end": 10}}]
    dossier = {
        "testimonia": [
            {
                "witness_label": "agreeing",
                "reported": text,
                "reported_basis": "own-report",
            },
            {
                "witness_label": "departing",
                "reported": "alpha xxxx",
                "reported_basis": "page-slice",
            },
            {
                "witness_label": "other-departure",
                "reported": "omega beta",
                "reported_basis": "own-report",
            },
        ]
    }

    assert perlector.flag_location_basis(dossier, flags, semi_final_text=text) == [
        {
            "class": "testimony-diff",
            "chair": "departing",
            "derivation": "page-slice",
            "location": {"start": 6, "end": 10},
        }
    ]


def test_audit_draft_requires_location_basis_exactly_when_testimony_located_a_flag():
    perlector = _perlector()
    policy = {"schema": audit.SCHEMA, "sha256": "a" * 64, "approval_ref": "approved"}
    draft = {
        "act_key": "a1",
        "attempt_ordinal": 1,
        "semi_final_text": "alpha beta",
        "page_id": "page-1",
        "page_ids": ["page-1"],
        "round_cap": 1,
        "policy": policy,
        "flags": [{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
        "flag_location_basis": [],
    }
    with pytest.raises(SchemaRefusal, match="flags and witness-derived location basis disagree"):
        perlector.audit.validate_draft(draft)

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
        perlector.audit.validate_draft(draft)

    # The case equal lengths could never catch: one flag, one basis row, and
    # the row accounting for a span no flag names. Read by count alone this
    # draft was well formed, and the record said a chair located a flag it
    # did not.
    draft["flags"] = [{"class": "testimony-diff", "location": {"start": 0, "end": 5}}]
    with pytest.raises(SchemaRefusal, match="flags and witness-derived location basis disagree"):
        perlector.audit.validate_draft(draft)

    draft["flags"] = [
        {"class": "testimony-diff", "location": {"start": 0, "end": 5}},
        {"class": "testimony-diff", "location": {"start": 6, "end": 10}},
    ]
    draft["flag_location_basis"] *= 2
    with pytest.raises(SchemaRefusal, match="repeats a witness-derived flag-location basis"):
        perlector.audit.validate_draft(draft)


# Everything ahead of the Perlector, run as real programs. The Pass-C delivery
# proof needs the stage's own `main()` in this process — that is the only way to
# hold the reader object it actually called — so the evidence it reads must be
# built by the real chain first, exactly as the Sol-S2 demonstration built it.
CHAIN_THROUGH_ATTESTATORES = (
    "pipeline/1_exemplar/door.py",
    "pipeline/1_exemplar/run.py",
    "pipeline/1_ink_map/run.py",
    "pipeline/2_designator/run.py",
    "pipeline/3_attestatores/run.py",
)


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


def _chain_through_attestatores(root: Path, scenario: str) -> None:
    for program in CHAIN_THROUGH_ATTESTATORES:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / program),
                "--run-root",
                str(root),
                "--run-id",
                "r",
                "--scenario",
                scenario,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        # `audit-change` is a clean-run scenario: every upstream stage exits 0.
        # Accepting a held exit here would let a silently degraded chain feed
        # the capture, and the act-count assertions downstream would report the
        # wrong defect.
        assert result.returncode == 0, f"{program}: {result.stderr}"


class _CapturingReader:
    """Records what the Perlector actually handed its reader, argument for argument.

    The keyword signature is spelled out rather than swallowed into `**kwargs`
    deliberately: it is the `reader.Reader` protocol, and a stage that started
    delivering the re-proof instrument by some other name would fail here rather
    than quietly capture nothing. `delivered_pixels` is reduced to counts — the
    buffers are page-sized and the claim under test is that they arrived, not
    what they contain.
    """

    def __init__(self, inner, calls: list[dict]):
        self._inner = inner
        self._calls = calls

    def read(self, dossier, *, pass_kind, delivered_pixels=None, audit_request=None):
        self._calls.append(
            {
                "pass_kind": pass_kind,
                "act_key": dossier.get("act_key"),
                "dossier_keys": sorted(dossier),
                "audit_request": copy.deepcopy(audit_request),
                "region_images": len((delivered_pixels or {}).get("region_images", [])),
                "page_render_images": len((delivered_pixels or {}).get("page_render_images", [])),
            }
        )
        return self._inner.read(
            dossier,
            pass_kind=pass_kind,
            delivered_pixels=delivered_pixels,
            audit_request=audit_request,
        )


def _perlector_with_capturing_reader(root: Path, scenario: str, monkeypatch):
    """Run the real Perlector stage in this process, holding every reader call."""
    perlector = _perlector()
    declared = perlector.FixtureReader
    calls: list[dict] = []
    monkeypatch.setattr(
        perlector,
        "FixtureReader",
        lambda fixture, fixture_scenario: _CapturingReader(
            declared(fixture, fixture_scenario), calls
        ),
    )
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(ROOT / "pipeline" / "4_perlector" / "run.py"),
            "--run-root",
            str(root),
            "--run-id",
            "r",
            "--scenario",
            scenario,
        ],
    )
    return perlector, perlector.main(), calls


def test_the_reader_receives_exactly_the_reproof_plan_the_perlectio_seals(tmp_path, monkeypatch):
    """Sol-S2, red-proved: Pass C sealed an instrument the reader never received.

    The audit ran the real Door-through-Attestatores chain for `audit-change`
    and then ran the Perlector with a capturing reader. Both audit calls
    contained exactly `act_attachment, act_id, act_key, dossier_digest,
    page_renders, prior_draft, prior_draft_view, regions, semi_final_text,
    testimonia, witness_regime`; `HAS_REPROOFS`, `HAS_FLAGS`, `HAS_LOCATION`
    and `HAS_PROMPT` were all false, and the stage still exited 0. A changed
    final text was therefore published as the result of a measured, neutral,
    span-scoped re-proof that had never been presented to anything.

    The existing Pass-C tests could not see it: every one of them inspects the
    reproof strings stored *after* the call. So this test captures the call,
    and asserts the delivered instrument equals the sealed one field for field
    — including the digest the Perlectio now binds its response to. The four
    probes the audit ran are asserted in their true direction rather than
    described.
    """
    root = tmp_path / "runs"
    _chain_through_attestatores(root, "audit-change")
    _, code, calls = _perlector_with_capturing_reader(root, "audit-change", monkeypatch)
    assert code == 0

    tree = RunTree(root, "r")
    finals = {record["subject_id"]: record for record in _records(tree, "perlectio")}
    delivered = {
        record["payload"]["act_key"]: record
        for record in finals.values()
        if record["payload"]["audit"]["request_digest"] is not None
    }
    audit_calls = [call for call in calls if call["pass_kind"] == audit.REPROOF_PASS_KIND]
    # Every act that seals a delivered request had exactly one reader call, and
    # no act had one it did not seal. A count that drifted either way would mean
    # the record and the instrument had parted again.
    assert len(audit_calls) == len(delivered) == len(finals) == 2
    assert sorted(call["act_key"] for call in audit_calls) == sorted(delivered)

    for call in audit_calls:
        final = delivered[call["act_key"]]
        record = final["payload"]["audit"]
        draft = tree.read_artifact_reference(
            record["draft_ref"],
            stage=PERLECTOR,
            kind="audit-draft",
            subject_id=final["subject_id"],
        )
        request = call["audit_request"]

        # HAS_REPROOFS, HAS_FLAGS, HAS_LOCATION, HAS_PROMPT -- all four, true,
        # and equal to the frozen flag set rather than merely present.
        assert request is not None
        assert request["reproofs"]
        assert [reproof["class"] for reproof in request["reproofs"]] == [
            flag["class"] for flag in draft["payload"]["flags"]
        ]
        assert [reproof["location"] for reproof in request["reproofs"]] == [
            flag["location"] for flag in draft["payload"]["flags"]
        ]
        assert all(
            reproof["prompt"]
            == audit.neutral_prompt(
                start=reproof["location"]["start"],
                end=reproof["location"]["end"],
                text_length=len(draft["payload"]["semi_final_text"]),
            )
            for reproof in request["reproofs"]
        )
        # The prompt's exact words, pinned as a LITERAL rather than derived
        # through `audit.neutral_prompt`: every other check in this suite
        # compares the instrument against its own generator, so an edit that
        # made the generator directional would agree with its own output
        # everywhere. This is the one place the delivered text is held still
        # from outside the instrument (GOVERNANCE 10).
        for reproof in request["reproofs"]:
            start = reproof["location"]["start"]
            end = reproof["location"]["end"]
            assert reproof["prompt"] == (
                f"Re-examine the ink at character location [{start}, {end}) of the "
                "delivered act. Report only what the ink supports there; "
                "if it supports the existing text, record confirmed unchanged."
            )

        # Delivered == sealed, exactly: the plan, the frozen draft it indexes
        # into, and the digest the response is bound to.
        assert request["reproofs"] == record["reproofs"]
        assert request["draft_ref"] == record["draft_ref"]
        assert request["semi_final_text"] == draft["payload"]["semi_final_text"]
        assert request["act_key"] == draft["payload"]["act_key"]
        assert request["attempt_ordinal"] == final["payload"]["attempt_ordinal"]
        assert digest_of(request) == record["request_digest"]
        assert request == audit.audit_request(
            act_key=draft["payload"]["act_key"],
            attempt_ordinal=draft["payload"]["attempt_ordinal"],
            draft_ref=record["draft_ref"],
            semi_final_text=draft["payload"]["semi_final_text"],
            flags=draft["payload"]["flags"],
        )

        # The applicable pixels, and a dossier that is still the sealed one.
        # `semi_final_text` used to be spliced into it, which handed the reader
        # an object whose own `dossier_digest` no longer covered its contents.
        assert call["region_images"] == len(final["payload"]["dossier"]["regions"])
        assert call["page_render_images"] == len(final["payload"]["dossier"]["page_renders"])
        assert call["dossier_keys"] == sorted(final["payload"]["dossier"])
        assert "semi_final_text" not in call["dossier_keys"]

    # The changed act is still the changed act: the delivered instrument is what
    # the published text now comes out of, not a plan sealed beside it.
    changed = next(
        record for record in _records(tree, "audit-finding") if record["payload"]["change_record"]
    )
    assert finals[changed["subject_id"]]["payload"]["text"] == "SYNTHETIC ACT ONE alpha beta gamma!"


def test_the_reader_refuses_a_reproof_pass_whose_instrument_never_arrived():
    """The seam's own half of the repair, independent of any run.

    `run.py` building the request correctly is one claim; a reader that would
    have carried on without one is the other, and it is the half that made the
    defect invisible. A reader may not condition generation on `pass_kind`, so
    a re-proof pass with no request is not a call it can complete honestly --
    there is no span to re-examine and no delivered task to answer.
    """
    request = audit.audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/artifacts/draft.json", "sha256": "a" * 64},
        semi_final_text="alpha beta gamma",
        flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
    )

    with pytest.raises(ContractError, match="no audit request"):
        reader_module.validate_audit_delivery(
            {"act_key": "a1"}, pass_kind=audit.REPROOF_PASS_KIND, audit_request=None
        )
    # The mirror: a span-scoped task delivered to a read of the whole act.
    with pytest.raises(ContractError, match="belongs to the pass that seals it"):
        reader_module.validate_audit_delivery(
            {"act_key": "a1"}, pass_kind="perlectio", audit_request=request
        )
    # And one act's frozen locations beside another act's pixels.
    with pytest.raises(ContractError, match="delivered beside the dossier"):
        reader_module.validate_audit_delivery(
            {"act_key": "a2"}, pass_kind=audit.REPROOF_PASS_KIND, audit_request=request
        )
    assert (
        reader_module.validate_audit_delivery(
            {"act_key": "a1"}, pass_kind=audit.REPROOF_PASS_KIND, audit_request=request
        )
        == request
    )


def test_a_directional_or_empty_audit_request_is_refused_at_the_delivery_boundary():
    """Neutrality is screened where the instrument is handed over, not only where
    it is stored. `payload.audit.reproofs` was already held to `neutral_prompt`
    exactly; the request now goes through the same screen, so a prompt telling
    the reader which way to argue cannot reach a reader by travelling on the
    delivered copy instead of the sealed one (GOVERNANCE 10)."""
    request = audit.audit_request(
        act_key="a1",
        attempt_ordinal=1,
        draft_ref={"relative_path": "4_perlector/artifacts/draft.json", "sha256": "b" * 64},
        semi_final_text="alpha beta gamma",
        flags=[{"class": "testimony-diff", "location": {"start": 6, "end": 10}}],
    )

    directional = copy.deepcopy(request)
    directional["reproofs"][0]["prompt"] = "The reading is wrong; replace it with gamma."
    with pytest.raises(SchemaRefusal, match="neutral location-only"):
        audit.validate_audit_request(directional)

    moved = copy.deepcopy(request)
    moved["reproofs"][0]["location"] = {"start": 6, "end": 99}
    with pytest.raises(SchemaRefusal, match="lies outside the delivered text"):
        audit.validate_audit_request(moved)

    empty = copy.deepcopy(request)
    empty["reproofs"] = []
    with pytest.raises(SchemaRefusal, match="delivers no re-proof location"):
        audit.validate_audit_request(empty)

    # `validate_input_refs` reads two keys and ignores the rest, so without the
    # nested closure an extra field here would reach the reader, enter the
    # digest, and survive `validate_chain`'s rebuild -- directional text riding
    # past the neutrality screen on the reference.
    widened = copy.deepcopy(request)
    widened["draft_ref"]["note"] = "the reading is probably gamma"
    with pytest.raises(SchemaRefusal, match="draft reference is not its closed shape"):
        audit.validate_audit_request(widened)


def test_the_chain_refuses_a_request_digest_that_is_not_the_frozen_plans_own(tmp_path):
    """The sealed digest is re-derived from the draft, never taken on trust.

    Without this the new field would be decoration: a producer could name any
    digest and the record would still validate, which is the same "the record
    says so" the delivery defect rested on. Both directions are refused -- a
    digest that does not render from the frozen plan, and a claimed delivery on
    an act whose plan or cap left nothing to deliver.
    """
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = _records(tree, "perlectio")[0]
    assert final["payload"]["audit"]["request_digest"] is not None

    forged = copy.deepcopy(final)
    forged["payload"]["audit"]["request_digest"] = "c" * 64
    with pytest.raises(SchemaRefusal, match="does not name the exact audit request"):
        audit.validate_chain(tree, forged, final["subject_id"])

    undelivered = copy.deepcopy(final)
    undelivered["payload"]["audit"]["request_digest"] = None
    with pytest.raises(SchemaRefusal, match="does not name the exact audit request"):
        audit.validate_chain(tree, undelivered, final["subject_id"])


def test_an_exhausted_cap_seals_its_plan_without_claiming_a_delivered_request(tmp_path):
    """`reproofs` alone could not tell a plan that ran from one that never could.

    With `round_cap = 0` the flags are frozen and their neutral locations are
    still sealed -- they are what the exhausted-cap uncertainty spans point at --
    but no reader is called at all. Recording that as an absent request is the
    difference between "a re-proof confirmed this span" and "nothing re-examined
    it", which is exactly the distinction GOVERNANCE 10 asks a measurement to
    keep.
    """
    exhausted = tmp_path / "exhausted.toml"
    exhausted.write_text(
        'schema = "perlector-audit.v1"\n'
        "default_round_cap = 1\n"
        "absolute_round_cap = 2\n"
        "round_cap = 0\n"
        'approval_ref = ""\n'
    )
    result = _run(tmp_path / "runs", "--perlector-audit-config", str(exhausted))
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    finals = _records(tree, "perlectio")
    assert finals
    for final in finals:
        record = final["payload"]["audit"]
        assert record["reproofs"], "the frozen plan is still sealed for the review to read"
        assert record["request_digest"] is None
        audit.validate_chain(tree, final, final["subject_id"])

        claimed = copy.deepcopy(final)
        claimed["payload"]["audit"]["request_digest"] = "d" * 64
        with pytest.raises(SchemaRefusal, match="left nothing to deliver"):
            audit.validate_chain(tree, claimed, final["subject_id"])


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
            "geometry_order": (0, 0),
            "text": "No 2 1689 alpha",
            "testimonia": ["No 2 1689 beta"],
            "within_crop": True,
        },
        {
            "act_id": "a2",
            "page_id": "p1",
            "order": 1,
            "geometry_order": (1, 0),
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
    # Only the different-input direction carries information: recomputing over
    # the SAME frozen rows equals itself for any implementation (the function
    # is pure), so that comparison was a tautology, not evidence. The
    # production ordering -- _page_flags called once, before the re-proof
    # loop -- is what the no-cascade guarantee actually rests on.
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


def test_omitting_an_intermediate_sibling_can_invent_an_adjacency_flag():
    """A shortened recovery denominator is not conservative in either direction."""
    first = {
        "act_id": "a1",
        "page_id": "p1",
        "order": 0,
        "geometry_order": (0, 0),
        "text": "1689 first",
        "testimonia": ["1689 first"],
        "within_crop": True,
    }
    intermediate = {
        "act_id": "a2",
        "page_id": "p1",
        "order": 1,
        "geometry_order": (1, 0),
        "text": "1688 intermediate",
        "testimonia": ["1688 intermediate"],
        "within_crop": True,
    }
    recovered = {
        "act_id": "a3",
        "page_id": "p1",
        "order": 2,
        "geometry_order": (2, 0),
        "text": "1688 recovered",
        "testimonia": ["1688 recovered"],
        "within_crop": True,
    }

    complete = audit.flags_once_per_page([first, intermediate, recovered])
    shortened = audit.flags_once_per_page([first, recovered])

    assert "date-sequence" not in {flag["class"] for flag in complete["a3"]}
    assert "date-sequence" in {flag["class"] for flag in shortened["a3"]}


def test_audit_page_set_keeps_the_act_primary_first_when_continuation_ordinal_is_lower():
    """Source ordinal orders pages, not an act's primary-first reading basis."""
    perlector = _perlector()
    bases = [
        {"source_page_ordinal": 2, "source_page_id": "primary-page"},
        {"source_page_ordinal": 1, "source_page_id": "earlier-continuation"},
        {"source_page_ordinal": 2, "source_page_id": "primary-page"},
    ]

    assert perlector.audit_page_ids(bases) == ["primary-page", "earlier-continuation"]


def test_recovery_sibling_context_is_sealed_and_never_republished(tmp_path):
    result = _run(tmp_path / "runs", scenario="review")
    assert result.returncode == 3, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    perlector = _perlector()
    readings = _records(tree, "perlectio")
    # More than one act may reach ordinal two; sealed act identities, not the
    # ordinal alone, distinguish the recovery from its sibling.
    recovered = next(
        record
        for record in readings
        if record["payload"]["act_key"] == "a1" and record["payload"]["attempt_ordinal"] == 2
    )
    sibling = next(
        record
        for record in readings
        if record["payload"]["act_key"] == "a2" and record["payload"]["attempt_ordinal"] == 1
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

    # Recovery must use the same per-page rows as the whole-run pass without
    # publishing a new sibling reading.
    sibling_pages = sorted(
        {region["source_page_id"] for region in sibling["payload"]["basis"]["regions"]}
    )
    assert len(sibling_pages) == 2
    assert [row["act_id"] for row in merged] == [sibling["subject_id"]] * 2
    assert [row["page_id"] for row in merged] == sibling_pages
    assert {row["text"] for row in merged} == {sibling["payload"]["text"]}
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


def test_recovery_selects_a_sibling_reaching_the_page_only_by_continuation(tmp_path):
    """Selection uses the sealed Perlectio page set, not its primary-page scalar.

    The sibling's complete page set must come from the real run tree while its
    proposal row continues to identify page 1 as primary.
    """
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    perlector = _perlector()
    sibling = next(
        record
        for record in _records(tree, "perlectio")
        if len({region["source_page_id"] for region in record["payload"]["basis"]["regions"]}) == 2
    )
    pages_by_ordinal = {
        region["source_page_ordinal"]: region["source_page_id"]
        for region in sibling["payload"]["basis"]["regions"]
    }
    sibling_pages = [pages_by_ordinal[ordinal] for ordinal in sorted(pages_by_ordinal)]
    primary_page, continuation_page = pages_by_ordinal[1], pages_by_ordinal[2]
    recovered_id = "recovery-subject-not-in-the-tree"
    expected = [
        {"act_id": recovered_id, "page_id": continuation_page},
        {"act_id": sibling["subject_id"], "page_id": primary_page},
    ]
    context = SimpleNamespace(tree=tree, config_digest=tree.read_run()["config_digest"])
    protocol_config, protocol_sha256 = perlector.protocol.load(
        ROOT / "config" / "perlector_protocol.toml"
    )

    merged = perlector._sealed_sibling_semi_finals(
        context,
        [{"act_id": recovered_id, "page_id": continuation_page}],
        expected=expected,
        protocol_config=protocol_config,
        protocol_sha256=protocol_sha256,
    )

    assert [row["act_id"] for row in merged] == [sibling["subject_id"]] * 2
    assert sorted(row["page_id"] for row in merged) == sorted(sibling_pages)


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
            "geometry_order": (0, 0),
            "text": degenerate,
            "testimonia": [degenerate],
            "within_crop": True,
        },
        {
            "act_id": "a2",
            "page_id": "p1",
            "order": 1,
            "geometry_order": (1, 0),
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


def test_unhashable_audit_classes_are_named_schema_refusals():
    """Resealed JSON arrays cannot escape class allowlists as raw TypeError."""
    policy = {"schema": audit.SCHEMA, "sha256": "0" * 64, "approval_ref": ""}
    draft = {
        "act_key": "a1",
        "attempt_ordinal": 1,
        "semi_final_text": "x",
        "page_id": "p1",
        "page_ids": ["p1"],
        "round_cap": 1,
        "policy": policy,
        "flags": [{"class": [], "location": {"start": 0, "end": 1}}],
        "flag_location_basis": [],
    }
    with pytest.raises(SchemaRefusal, match="unknown class"):
        audit.validate_draft(draft)

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
        }
    )
    with pytest.raises(SchemaRefusal, match="unknown triggering flag class"):
        audit.validate_finding(finding, text="y", flag_text="x")

    reference = {"relative_path": "4_perlector/audit.json", "sha256": "0" * 64}
    perlectio_audit = {
        "draft_ref": reference,
        "finding_ref": reference,
        "finding_digest": "0" * 64,
        "unresolved": False,
        "reproofs": [
            {
                "class": [],
                "location": {"start": 0, "end": 1},
                "prompt": audit.neutral_prompt(start=0, end=1, text_length=1),
            }
        ],
        "request_digest": None,
    }
    with pytest.raises(SchemaRefusal, match="unknown class or prompt"):
        audit.validate_perlectio_audit(perlectio_audit, text_length=1)


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
    # `None`, never `False`: no audit exists, which is a different recorded
    # fact from "audited, resolved".
    assert _recensor().audit_state(SimpleNamespace(tree=None), not_run, "act-1") is None


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
    drifted_finding["payload"]["page_ids"] = ["pg_drifted"]
    audit.validate_finding(drifted_finding["payload"], text=final["payload"]["text"])

    class DriftedTree:
        def read_artifact_reference(self, _reference, *, stage, kind, subject_id):
            assert stage == PERLECTOR and subject_id == final["subject_id"]
            return draft if kind == "audit-draft" else drifted_finding

    with pytest.raises(SchemaRefusal, match="restate different frozen facts"):
        audit.validate_chain(DriftedTree(), final, final["subject_id"])


def test_shared_chain_refuses_a_page_set_forged_back_to_the_primary_page(tmp_path):
    """A coordinated draft/finding reseal cannot erase page 2 from the audit.

    The Perlectio's sealed region basis remains the independent page fact.
    """
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = next(
        record
        for record in _records(tree, "perlectio")
        if len({region["source_page_id"] for region in record["payload"]["basis"]["regions"]}) == 2
    )
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
    forged_draft = copy.deepcopy(draft)
    forged_finding = copy.deepcopy(finding)
    primary_page = forged_draft["payload"]["page_id"]
    assert len(forged_draft["payload"]["page_ids"]) == 2
    forged_draft["payload"]["page_ids"] = [primary_page]
    forged_finding["payload"]["page_ids"] = [primary_page]
    forged_final = copy.deepcopy(final)
    forged_final["payload"]["audit"]["finding_digest"] = digest_of(forged_finding["payload"])

    class ForgedTree:
        def read_artifact_reference(self, _reference, *, stage, kind, subject_id):
            assert stage == PERLECTOR and subject_id == final["subject_id"]
            return forged_draft if kind == "audit-draft" else forged_finding

    with pytest.raises(SchemaRefusal, match="page set.*sealed region basis"):
        audit.validate_chain(ForgedTree(), forged_final, final["subject_id"])


def test_shared_chain_keeps_primary_first_when_a_continuation_page_ordinal_is_lower(tmp_path):
    """The independent audit verifier must use sealed region order too."""
    result = _run(tmp_path / "runs")
    assert result.returncode == 0, result.stderr
    tree = RunTree(tmp_path / "runs", "r")
    final = next(
        record
        for record in _records(tree, "perlectio")
        if len({region["source_page_id"] for region in record["payload"]["basis"]["regions"]}) == 2
    )
    reversed_ordinals = copy.deepcopy(final)
    primary, continuation = reversed_ordinals["payload"]["basis"]["regions"][:2]
    primary["source_page_ordinal"] = 2
    continuation["source_page_ordinal"] = 1

    # The primary-first audit record remains valid. Sorting these pages by
    # source ordinal would reverse them and falsely reject the sealed chain.
    audit.validate_chain(tree, reversed_ordinals, final["subject_id"])
