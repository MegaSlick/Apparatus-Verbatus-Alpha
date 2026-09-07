"""Focused audit tests for R6's new geometry and testimony coverage inputs."""

import importlib.util
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting, SchemaRefusal
from common.contracts.identities import attempt_id
from common.native_witness import partition_disagreement
from common.stage import RESIDUAL_ENUMERATION_COMPLETE, RESIDUAL_ENUMERATION_WITHHELD

ROOT = Path(__file__).resolve().parents[2]


def _load_recensor():
    path = ROOT / "pipeline/5_recensor/run.py"
    spec = importlib.util.spec_from_file_location("recensor_run_coverage_inputs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN = _load_recensor()


class _ArtifactTree:
    def __init__(self, records):
        self.records = {record["artifact_id"]: record for record in records}

    def build_manifest(self, stage):
        return {
            "artifacts": [
                {
                    "kind": record["kind"],
                    "artifact_id": record["artifact_id"],
                    "subject_id": record["subject_id"],
                }
                for record in self.records.values()
                if record["stage"] == stage
            ]
        }

    def read_artifact(self, stage, kind, artifact_id):
        record = self.records[artifact_id]
        assert record["stage"] == stage
        assert record["kind"] == kind
        return record


class _Context(SimpleNamespace):
    def artifact_ref(self, stage, kind, artifact_id):
        return {
            "relative_path": f"stages/{stage}/{kind}/{artifact_id}.json",
            "sha256": "0" * 64,
        }


class _PublishingContext:
    def publish(self, **kwargs):
        return kwargs


@pytest.mark.parametrize("field", ["consensus", "majority", "vote", "quorum"])
def test_review_payload_refuses_witness_preference_vocabulary(field):
    """The durable review write has the same selector guard as Perlectio."""
    with pytest.raises(SchemaRefusal, match="preference"):
        RUN.publish_review(
            _PublishingContext(),
            subject_id="act-1",
            outcome="accepted",
            attempt="act-1:recense:1",
            inputs=[],
            payload={field: True},
        )


def _context(*records):
    return _Context(tree=_ArtifactTree(records))


@pytest.fixture(autouse=True)
def _no_expected_acts(monkeypatch):
    """Default the proposal seal to "no acts" for every test in this file.

    `expected_acts` reads and self-hash-verifies a real Designator artifact,
    which these hand-built trees deliberately do not carry. Tests that need an
    act list set their own over the top of this one; the ones that exercise a
    page Testimonium's own refusals need only that the seal read not explode
    before the refusal they are about.
    """
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])


def _page_testimonium(*, outcome, retained=..., attempt_ordinal=1, artifact_id=None):
    subject_id = "page-1"
    chair = "attestator_1"
    # `page_role` is part of the closed page-Testimonium shape the producer
    # publishes; act-1's only region is on page 1, so `primary` is what
    # `reconcile_page_roles` re-derives for this double.
    payload = {
        "page_ordinal": 1,
        "page_role": "primary",
        "chair": chair,
        "attempt_ordinal": attempt_ordinal,
    }
    # `retained`, not `reported`: the page Testimonium's text field is
    # `payload`, and a double whose parameter still said `reported` invited
    # the next case here to build a record no producer can emit.
    if retained is not ...:
        payload["payload"] = retained
    return {
        "artifact_id": artifact_id or f"page-witness-{attempt_ordinal}",
        "stage": RUN.ATTESTATORES,
        "kind": "page-testimonium",
        "subject_id": subject_id,
        "attempt_id": attempt_id(subject_id, f"read:{chair}", attempt_ordinal),
        "outcome": outcome,
        "payload": payload,
    }


def _conservation(
    artifact_id,
    *,
    ordinal=1,
    measurable=True,
    components=None,
    counts=...,
    enumeration=RESIDUAL_ENUMERATION_COMPLETE,
    declared_count=...,
    bound=2000,
    keep_components=None,
):
    """One Designator conservation record as the Designator now publishes them.

    `residual_enumeration`, `residual_component_count` and
    `max_residual_components` are on every record the stage writes, so a double
    that omitted them would be testing a shape no producer emits and would pass
    checks the real artifact has to satisfy. A withheld record drops the
    `residual_components` key entirely -- `keep_components` exists only so the
    refusal against a withheld record that kept it can be built at all.
    """
    residual_components = [] if components is None else components
    if counts is ...:
        residual = sum(
            component["pixel_count"]
            for component in residual_components
            if isinstance(component, dict)
            and isinstance(component.get("pixel_count"), int)
            and not isinstance(component.get("pixel_count"), bool)
        )
        counts = (residual, 0, residual) if measurable else (None, None, None)
    total, claimed, residual = counts
    withheld = enumeration == RESIDUAL_ENUMERATION_WITHHELD
    payload = {
        "page_ordinal": ordinal,
        "ink_measurable": measurable,
        "total_ink_pixel_count": total,
        "claimed_pixel_count": claimed,
        "residual_pixel_count": residual,
        "residual_component_count": (
            (bound + 1 if withheld else len(residual_components))
            if declared_count is ...
            else declared_count
        ),
        "max_residual_components": bound,
        "residual_enumeration": enumeration,
    }
    if not withheld or keep_components is not None:
        payload["residual_components"] = (
            residual_components if keep_components is None else keep_components
        )
    return {
        "artifact_id": artifact_id,
        "stage": RUN.DESIGNATOR,
        "kind": "conservation",
        "subject_id": f"page-{artifact_id}",
        "outcome": "measured",
        "payload": payload,
    }


def _page_residual_act(ordinal=1):
    return {"act_key": f"page-residual:{ordinal}", "page_ordinal": ordinal, "outcome": "held"}


def _component(*, bounds=None, pixel_count=12):
    return {
        "bounds": {"x": 2, "y": 3, "w": 4, "h": 5} if bounds is None else bounds,
        "pixel_count": pixel_count,
        "review_priority": "normal",
    }


def _attachment(context, *, end, testimonium_id="page-witness-1"):
    # `attempt_id`/`attempt_ordinal` are not decoration: `current_act_attachments`
    # derives "current" through `latest_attempt`, which refuses a record whose
    # sealed attempt identity does not bind the ordinal it claims. A double
    # without them would pass a check the real artifact has to satisfy.
    return {
        "artifact_id": "attachment-1",
        "stage": RUN.ATTESTATORES,
        "kind": "act-attachment",
        "subject_id": "act-1",
        "attempt_id": attempt_id("act-1", "act-attachment", 1),
        "outcome": "attached",
        "payload": {
            "attempt_ordinal": 1,
            "attachments": [
                {
                    "chair": "attestator_1",
                    "page_witness": True,
                    # Required of every page-witness row since the attachment
                    # became page-scoped: `reconcile_page_roles` derives each
                    # page's role denominator from exactly this field.
                    "page_ordinal": 1,
                    "testimonium_ref": context.artifact_ref(
                        RUN.ATTESTATORES, "page-testimonium", testimonium_id
                    ),
                    "attached": True,
                    "alignment": {
                        "status": "aligned",
                        "witness_span": {"start": 0, "end": end},
                    },
                }
            ],
        },
    }


def _attachment_fact_record(rows):
    return {
        "artifact_id": "attachment-facts-1",
        "stage": RUN.ATTESTATORES,
        "kind": "act-attachment",
        "subject_id": "act-1",
        "attempt_id": attempt_id("act-1", "act-attachment", 1),
        "outcome": "attached",
        "payload": {"attempt_ordinal": 1, "attachments": rows},
    }


def _page_fact(*, ordinal, attached, anchor_basis=None, comparable=None):
    alignment = (
        {
            "status": "aligned",
            "anchor_basis": anchor_basis,
            "anchor_chair": "attestator_1" if anchor_basis == "act-anchor" else None,
            "anchor_span": {"start": 0, "end": 1},
            "witness_span": {"start": 0, "end": 1},
            "line_geometry": [],
            "loss": {},
            "offset_maps": {},
        }
        if attached
        else {"status": "unaligned", "reason": "continuation-page-no-act-anchor"}
    )
    return {
        "chair": "attestator_1",
        "page_witness": True,
        "page_ordinal": ordinal,
        "attached": attached,
        "comparable": attached if comparable is None else comparable,
        "attachment_basis": "geometric-overlap" if attached else "unattached",
        "testimonium_ref": {"artifact_id": f"pt-{ordinal}"},
        "content_health": {"truncated": False},
        "alignment": alignment,
    }


def test_artifact_tree_preserves_stage_ownership_in_manifests_and_reads():
    page = _page_testimonium(outcome="read", retained="page text")
    conservation = _conservation("conservation-1")
    tree = _ArtifactTree([page, conservation])

    assert [entry["artifact_id"] for entry in tree.build_manifest(RUN.DESIGNATOR)["artifacts"]] == [
        "conservation-1"
    ]
    assert [
        entry["artifact_id"] for entry in tree.build_manifest(RUN.ATTESTATORES)["artifacts"]
    ] == ["page-witness-1"]
    assert tree.read_artifact(RUN.DESIGNATOR, "conservation", "conservation-1") is conservation
    with pytest.raises(AssertionError):
        tree.read_artifact(RUN.ATTESTATORES, "conservation", "conservation-1")


def test_a_reading_page_testimonium_cannot_lose_its_reported_text_and_take_the_skip(monkeypatch):
    """V4: the no-report skip belongs only to a non-reading page record."""


def test_a_reading_page_testimonium_cannot_lose_its_native_payload_and_take_the_skip(monkeypatch):
    """V4: the no-payload skip belongs only to a non-reading page record."""
    act_reading = {
        "artifact_id": "act-reading-1",
        "stage": RUN.ATTESTATORES,
        "kind": "testimonium",
        "subject_id": "act-1",
        "outcome": "read",
        "payload": {"chair": "attestator_1", "payload": "real act text"},
    }
    context = _context(act_reading, _page_testimonium(outcome="read"))
    context.tree.records["attachment-1"] = _attachment(context, end=0)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    with pytest.raises(
        FatalAccounting,
        match="reading page Testimonium has no retained derived payload for content coverage",
    ):
        RUN.testimony_content_findings(context)


def test_a_non_reading_page_testimonium_still_has_no_content_to_compare(monkeypatch):
    context = _context(_page_testimonium(outcome="failed"))
    attachment = _attachment(context, end=0)
    row = attachment["payload"]["attachments"][0]
    row["attached"] = False
    row["alignment"] = {"status": "unaligned", "reason": "non-reading-page-attempt-failed"}
    context.tree.records["attachment-1"] = attachment
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    assert RUN.testimony_content_findings(context) == {}


def test_an_orphaned_page_testimonium_cannot_become_an_unowned_finding():
    context = _context(_page_testimonium(outcome="failed"))

    with pytest.raises(FatalAccounting, match="orphaned record.*page evidence cannot"):
        RUN.testimony_content_findings(context)


def test_missing_retained_partition_cannot_suppress_a_rederived_coverage_finding(monkeypatch):
    page = _page_testimonium(outcome="read", retained="ink")
    page["payload"].update(
        {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 50, "y": 50, "w": 10, "h": 10},
                    "bounds_source": "native",
                }
            ],
        }
    )
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=3)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )
    proposal = {
        "payload": {
            "origin": "proposal",
            "transform": {
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            },
        }
    }
    monkeypatch.setattr(RUN, "artifacts_for", lambda *unused: [proposal])

    finding = RUN.testimony_content_findings(context)[1]

    assert finding["unclaimed_observations"] == [
        {
            "kind": "unrouted-observation",
            "testimonium_id": "page-witness-1",
            "ordinal": 0,
            "source_page_id": "page-1",
            "bounds": {"x": 50, "y": 50, "w": 10, "h": 10},
            "overlap_rule": {"rule": "positive-area", "status": "unmeasured"},
        }
    ]


@pytest.mark.parametrize(
    "bounds",
    (
        pytest.param({"x": 0, "y": 0, "h": 10}, id="missing-width"),
        pytest.param({"x": 0, "y": 0, "w": 10, "h": 10, "z": 1}, id="extra-side"),
        pytest.param({"x": 0, "y": 0, "w": 10, "h": True}, id="boolean-height"),
        pytest.param({"x": 0, "y": 0, "w": 10, "h": "10"}, id="string-height"),
        pytest.param({"x": -1, "y": 0, "w": 10, "h": 10}, id="negative-x"),
        pytest.param({"x": 0, "y": -1, "w": 10, "h": 10}, id="negative-y"),
        pytest.param({"x": 0, "y": 0, "w": 0, "h": 10}, id="zero-width"),
        pytest.param({"x": 0, "y": 0, "w": 10, "h": 0}, id="zero-height"),
        pytest.param({"x": 0, "y": 0, "w": -10, "h": 10}, id="negative-width"),
        pytest.param({"x": 0, "y": 0, "w": 10, "h": -10}, id="negative-height"),
    ),
)
def test_a_proposal_rectangle_short_of_its_four_numbers_is_named_not_indexed(monkeypatch, bounds):
    """Two failures, one refusal. A rectangle missing a side travels on as a
    proposal box into `unrouted_observations`, which indexes all four by name,
    and leaves the stage that decides recovery as a bare `KeyError` naming
    neither page nor act. A rectangle that is merely degenerate -- off-page
    origin, or zero/negative extent -- is worse: it indexes cleanly and overlaps
    nothing, so ink a real proposal covers is published as an unrouted
    observation and drives bounded recovery on evidence the run manufactured.
    Every case here is a rectangle `_proposal_geometry_by_page` already refuses
    of the same sealed proposals."""
    page = _page_testimonium(outcome="read", retained="ink")
    page["payload"].update(
        {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 50, "y": 50, "w": 10, "h": 10},
                    "bounds_source": "native",
                }
            ],
        }
    )
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=3)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )
    proposal = {
        "payload": {
            "origin": "proposal",
            "transform": {
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": bounds,
            },
        }
    }
    monkeypatch.setattr(RUN, "artifacts_for", lambda *unused: [proposal])

    with pytest.raises(FatalAccounting, match="has no page-pixel bounds"):
        RUN.testimony_content_findings(context)


def test_unclaimed_geometry_alone_does_not_publish_a_clean_text_measurement(monkeypatch):
    """A failed page has no text to diff, and must not look like a covered one.

    The unclaimed-observation route creates the page finding, seeded
    `shortfall: False`. On a page whose witness reported no text, nothing then
    measures anything -- and the seed would be published as a clean text
    coverage result, byte-identical to a page whose witnesses were read and
    covered every character (GOVERNANCE 10). The geometry stays; the text fact
    goes back to unmeasured.
    """
    page = _page_testimonium(outcome="failed")
    page["payload"].update(
        {
            "presented": {"source_page_id": "page-1"},
            "observed": [
                {
                    "ordinal": 0,
                    "bounds": {"x": 50, "y": 50, "w": 10, "h": 10},
                    "bounds_source": "native",
                }
            ],
        }
    )
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=3)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )
    proposal = {
        "payload": {
            "origin": "proposal",
            "transform": {
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            },
        }
    }
    monkeypatch.setattr(RUN, "artifacts_for", lambda *unused: [proposal])

    finding = RUN.testimony_content_findings(context)[1]

    assert finding["by_chair"] == {}
    assert finding["shortfall"] is None, "no chair's text was measured on this page"
    assert "was not measured" in finding["reason"]
    assert len(finding["unclaimed_observations"]) == 1, "the geometry finding still stands"


def test_retained_partition_is_bound_to_the_current_sealed_proposals(monkeypatch):
    """An internally consistent stale snapshot is still false evidence."""
    observed = [
        {
            "ordinal": 0,
            "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            "bounds_source": "native",
        }
    ]
    page = _page_testimonium(outcome="read", retained="ink")
    page["payload"].update(
        {
            "presented": {"source_page_id": "page-1"},
            "observed": observed,
        }
    )
    stale_proposal = {
        "payload": {
            "origin": "proposal",
            "transform": {
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": {"x": 20, "y": 20, "w": 5, "h": 5},
            },
        }
    }
    page["payload"]["partition_disagreement"] = partition_disagreement(page, [stale_proposal])
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=3)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )
    sealed_proposal = {
        "payload": {
            "origin": "proposal",
            "transform": {
                "source_page_id": "page-1",
                "source_page_ordinal": 1,
                "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
            },
        }
    }
    monkeypatch.setattr(RUN, "artifacts_for", lambda *unused: [sealed_proposal])

    with pytest.raises(FatalAccounting, match="false partition facts.*sealed proposals"):
        RUN.testimony_content_findings(context)


def test_large_untrusted_page_text_does_not_allocate_a_character_bitmap(monkeypatch):
    """Coverage memory stays bounded by attachment spans, not response length."""
    text = "x" * 1_000_000
    page = _page_testimonium(outcome="read", retained=text)
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=0)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    tracemalloc.start()
    try:
        finding = RUN.testimony_content_findings(context)[1]["by_chair"]["attestator_1"]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert finding["uncovered_non_whitespace"] == {
        "ranges": [{"start": 0, "end": len(text)}],
        "count": len(text),
    }
    assert peak < 2_000_000


def test_an_attached_page_witness_requires_its_current_page_testimonium(monkeypatch):
    context = _context()
    context.tree.records["attachment-1"] = _attachment(context, end=5)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    with pytest.raises(
        FatalAccounting,
        match="act act-1.*attestator_1.*no current page Testimonium",
    ):
        RUN.testimony_content_findings(context)


def test_an_unaligned_continuation_still_requires_its_current_page_testimonium(monkeypatch):
    """An explicit inability to align is retained evidence, not permission to lose it."""
    context = _context()
    attachment = _attachment(context, end=0)
    row = attachment["payload"]["attachments"][0]
    row["attached"] = False
    row["alignment"] = {
        "status": "unaligned",
        "reason": "continuation-page-no-act-anchor",
    }
    context.tree.records["attachment-1"] = attachment
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    with pytest.raises(
        FatalAccounting,
        match="act act-1.*attestator_1.*no current page Testimonium",
    ):
        RUN.testimony_content_findings(context)


def _patched_page_geometry(monkeypatch, context, observed_by_ordinal, textless=()):
    """Stand in for 10C's geometric sub-derivation so merge/duplicate/enum
    protections stay testable in isolation. Attached rows get one native box
    overlapping the page's sole proposal; unattached rows get none. The real
    geometric derivation keeps its own tests on real run trees."""
    box = {"x": 0, "y": 0, "w": 10, "h": 10}
    monkeypatch.setattr(
        RUN,
        "_proposal_geometry_by_page",
        lambda unused_context, unused_act_id: {
            ordinal: {"source_page_id": f"pg-{ordinal}", "bounds": [dict(box)]}
            for ordinal in observed_by_ordinal
        },
    )
    monkeypatch.setattr(RUN, "validate_page_testimonium_payload", lambda payload, **unused: payload)

    def fake_reference(reference, *, stage, kind, subject_id):
        ordinal = int(subject_id.rsplit("-", 1)[1])
        observed = (
            [{"ordinal": 0, "bounds": dict(box), "bounds_source": "native", "span": None}]
            if observed_by_ordinal[ordinal]
            else []
        )
        return {
            "artifact_id": f"pt-{ordinal}",
            "outcome": "read" if observed_by_ordinal[ordinal] else "failed",
            "payload": {
                "chair": "attestator_1",
                "page_ordinal": ordinal,
                "observed": observed,
                # Retained page text: comparability requires an aligned slice
                # of it, and the fake mirrors the real record's shape. A page in
                # `textless` is observed but retained no text of its own -- the
                # continuation a witness reached without producing a page slice,
                # which is how one chair's two rows differ in `comparable`.
                "payload": (
                    "page text"
                    if observed_by_ordinal[ordinal] and ordinal not in textless
                    else None
                ),
            },
        }

    monkeypatch.setattr(context.tree, "read_artifact_reference", fake_reference, raising=False)
    monkeypatch.setattr(context.tree, "read_bytes", lambda relative_path: b"", raising=False)


def test_page_attachment_facts_preserve_a_later_primary_alignment(monkeypatch):
    """An earlier-page continuation cannot erase the act's primary alignment.

    This order alone does not discriminate — last-row-wins would report the same
    two values — so the reverse order below is the half that can actually fail.
    Both are kept because the merge has to hold in either direction.
    """
    rows = [
        _page_fact(ordinal=1, attached=False),
        _page_fact(ordinal=2, attached=True, anchor_basis="act-anchor"),
    ]
    context = _context(_attachment_fact_record(rows))
    _patched_page_geometry(monkeypatch, context, {1: False, 2: True})

    facts = RUN.act_attachment_facts(
        context, "act-1", {"attestator_1": {"outcome": "read", "read_evidence": {}}}
    )

    assert facts["attestator_1"]["attached"] is True
    assert facts["attestator_1"]["anchor_basis"] == "act-anchor"


@pytest.mark.parametrize(
    "comparable_first",
    (True, False),
    ids=("comparable-page-first", "comparable-page-second"),
)
def test_page_attachment_facts_keep_the_comparable_page_in_either_row_order(
    monkeypatch, comparable_first
):
    """`comparable` is a per-page fact, so the merge may not depend on row order.

    The producer derives it from that page's own alignment status and that
    page's own retained text, so one chair's two rows for one act genuinely
    differ in it. Merged on `attached` alone, whichever row arrived first won --
    and rows arrive in page order, so a continuation page that attached without
    comparable text erased the primary page that had both. The chair then
    dropped out of the witness floor and the act read under-witnessed, held for
    a human on evidence that was there all along.
    """
    comparable_page = _page_fact(ordinal=1, attached=True, anchor_basis="act-anchor")
    # Attached with a real alignment, but no comparable text of its own: the
    # continuation page a witness reached without producing a page slice.
    incomparable_page = _page_fact(
        ordinal=2, attached=True, anchor_basis="no-page-anchor", comparable=False
    )
    rows = (
        [comparable_page, incomparable_page]
        if comparable_first
        else [incomparable_page, comparable_page]
    )
    context = _context(_attachment_fact_record(rows))
    _patched_page_geometry(monkeypatch, context, {1: True, 2: True}, textless=(2,))

    facts = RUN.act_attachment_facts(
        context, "act-1", {"attestator_1": {"outcome": "read", "read_evidence": {}}}
    )

    assert facts["attestator_1"]["attached"] is True
    assert facts["attestator_1"]["comparable"] is True, (
        "the page that produced comparable text was discarded because of its row order"
    )


def test_page_attachment_facts_preserve_an_earlier_primary_alignment(monkeypatch):
    """A later continuation row may not overwrite the primary page's alignment.

    The discriminating order: replace the `attached or attached` merge with
    last-row-wins and this reports False and None instead.
    """
    rows = [
        _page_fact(ordinal=1, attached=True, anchor_basis="act-anchor"),
        _page_fact(ordinal=2, attached=False),
    ]
    context = _context(_attachment_fact_record(rows))
    _patched_page_geometry(monkeypatch, context, {1: True, 2: False})

    facts = RUN.act_attachment_facts(
        context, "act-1", {"attestator_1": {"outcome": "read", "read_evidence": {}}}
    )

    assert facts["attestator_1"]["attached"] is True
    assert facts["attestator_1"]["anchor_basis"] == "act-anchor"


@pytest.mark.parametrize(
    "bases",
    (("act-line-not-located", "act-anchor"), ("act-anchor", "act-line-not-located")),
    ids=("failure-first", "failure-second"),
)
def test_page_attachment_facts_keep_a_failed_geometry_across_its_pages(bases, monkeypatch):
    """`act-line-not-located` survives an aligned sibling row, in either order.

    This is the clause the merge's second condition exists for, and it decides
    whether blank_corroboration can block a confirmed-blank. Drop it and an act
    whose page geometry located no line for it seals as a proved blank — a real
    baptism or burial leaving the export as "no readable text", with nothing
    downstream able to tell.
    """
    rows = [
        _page_fact(ordinal=1, attached=True, anchor_basis=bases[0]),
        _page_fact(ordinal=2, attached=True, anchor_basis=bases[1]),
    ]
    context = _context(_attachment_fact_record(rows))
    _patched_page_geometry(monkeypatch, context, {1: True, 2: True})

    facts = RUN.act_attachment_facts(
        context, "act-1", {"attestator_1": {"outcome": "read", "read_evidence": {}}}
    )

    assert facts["attestator_1"]["anchor_basis"] == "act-line-not-located"


def test_page_attachment_facts_refuse_a_duplicate_page_pair(monkeypatch):
    rows = [
        _page_fact(ordinal=1, attached=False),
        _page_fact(ordinal=1, attached=False),
    ]
    context = _context(_attachment_fact_record(rows))
    _patched_page_geometry(monkeypatch, context, {1: False})

    with pytest.raises(FatalAccounting, match="repeats attachment pair.*exactly one row"):
        RUN.act_attachment_facts(
            context, "act-1", {"attestator_1": {"outcome": "read", "read_evidence": {}}}
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [("status", "computed alignment fact"), ("anchor_basis", "malformed aligned")],
)
def test_page_attachment_facts_name_unhashable_alignment_enums(field, message, monkeypatch):
    """JSON arrays at enum fields are refusals, never set-membership tracebacks."""
    row = _page_fact(ordinal=1, attached=True, anchor_basis="act-anchor")
    row["alignment"][field] = []
    context = _context(_attachment_fact_record([row]))
    _patched_page_geometry(monkeypatch, context, {1: True})

    with pytest.raises(FatalAccounting, match=message):
        RUN.act_attachment_facts(
            context, "act-1", {"attestator_1": {"outcome": "read", "read_evidence": {}}}
        )


def test_a_held_act_without_an_attachment_refuses_even_without_page_testimony(monkeypatch):
    context = _context()
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            {
                "act_id": "act-1",
                "act_key": "a1",
                "page_ordinal": 1,
                "outcome": "held",
            }
        ],
    )

    with pytest.raises(FatalAccounting, match="act act-1 has no attachment for content coverage"):
        RUN.testimony_content_findings(context)


def test_an_attachment_without_rows_refuses_even_without_page_testimony(monkeypatch):
    context = _context()
    attachment = _attachment(context, end=0)
    attachment["payload"]["attachments"] = {}
    context.tree.records["attachment-1"] = attachment
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            {
                "act_id": "act-1",
                "act_key": "a1",
                "page_ordinal": 1,
                "outcome": "held",
            }
        ],
    )

    with pytest.raises(FatalAccounting, match="attachment for act-1 has no rows"):
        RUN.testimony_content_findings(context)


def test_each_act_gets_a_private_copy_of_its_pages_content_finding():
    """V3: mutating one act's nested payload cannot corrupt a sibling review."""
    findings = {
        1: {
            "by_chair": {
                "attestator_1": {
                    "attached_spans": [{"start": 0, "end": 5, "act_id": "act-1"}],
                    "uncovered_non_whitespace": {
                        "ranges": [{"start": 5, "end": 6}],
                        "count": 1,
                    },
                }
            },
            "shortfall": True,
        }
    }

    first = RUN.testimony_content_for_page(findings, 1)
    sibling = RUN.testimony_content_for_page(findings, 1)
    first["by_chair"]["attestator_1"]["uncovered_non_whitespace"]["ranges"].clear()
    first["shortfall"] = False

    assert sibling["by_chair"]["attestator_1"]["uncovered_non_whitespace"] == {
        "ranges": [{"start": 5, "end": 6}],
        "count": 1,
    }
    assert sibling["shortfall"] is True
    assert findings[1]["by_chair"]["attestator_1"]["uncovered_non_whitespace"] == {
        "ranges": [{"start": 5, "end": 6}],
        "count": 1,
    }


def test_unreported_page_content_is_unavailable_and_cannot_fire_the_shortfall_route():
    """An absent measurement is neither a measured clean page nor a shortfall."""
    content = RUN.testimony_content_for_page({}, 7)

    assert content == RUN.NO_PAGE_CONTENT_COVERAGE
    assert content["by_chair"] is None
    assert content["shortfall"] is None
    assert "no page witness supplied comparable page text for this page" in content["reason"]
    assert content is not RUN.NO_PAGE_CONTENT_COVERAGE
    assert (
        RUN.review_route_from_findings(
            testimony_shortfall=content["shortfall"],
            audit_unresolved=False,
            under_witnessed=False,
        )
        is None
    )


def test_real_uncovered_testimony_ranges_route_to_review_losslessly(monkeypatch):
    """V2a: a genuine content shortfall is measured and reaches the hold route."""
    page = _page_testimonium(outcome="read", retained="alphaXYZ \tQ")
    context = _context(page)
    context.tree.records["attachment-1"] = _attachment(context, end=5)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    finding = RUN.testimony_content_findings(context)[1]

    uncovered = finding["by_chair"]["attestator_1"]["uncovered_non_whitespace"]
    assert uncovered == {
        "ranges": [{"start": 5, "end": 8}, {"start": 10, "end": 11}],
        "count": 4,
    }
    assert uncovered["count"] == sum(item["end"] - item["start"] for item in uncovered["ranges"])
    assert finding["shortfall"] is True
    outcome, reason = RUN.review_route_from_findings(
        testimony_shortfall=finding["shortfall"],
        audit_unresolved=False,
        under_witnessed=False,
    )
    assert outcome == "held-for-review"
    assert "testimony coverage is incomplete at the whole-page level" in reason
    assert "may belong to another act on the same page" in reason


def _continuation_page_context(monkeypatch, *, reason):
    """A page whose only act is primary somewhere else, declared unalignable here.

    The shape the Perlector really produces (`pipeline/4_perlector/run.py`) and
    the Attestatores really write (`pipeline/3_attestatores/run.py`): act-1 is
    marked out on page 2, one of its proposal regions is cut from page 1, and
    page 1's attachment row for it carries the forced unaligned alignment. The
    page witness transcribed page 1's whole text, so nothing on it is covered.
    """
    page = _page_testimonium(outcome="read", retained="alphaXYZ \tQ")
    page["payload"]["page_role"] = "continuation"
    context = _context(page)
    attachment = _attachment(context, end=0)
    attachment["payload"]["attachments"][0]["alignment"] = {
        "status": "unaligned",
        "reason": reason,
    }
    context.tree.records["attachment-1"] = attachment
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 2}],
    )
    monkeypatch.setattr(
        RUN,
        "artifacts_for",
        lambda *unused: [
            {
                "payload": {
                    "origin": "proposal",
                    "transform": {
                        "source_page_id": "page-1",
                        "source_page_ordinal": 1,
                        "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                    },
                }
            }
        ],
    )
    return context


def test_a_continuation_pages_content_coverage_is_recorded_unmeasured_by_name(monkeypatch):
    """Tyrel's ruling on Unit 12 F2, at the measurement itself.

    The span union this page's text was diffed against is empty because the
    Perlector declared it empty, not because the witnesses covered nothing. The
    count is kept -- it is a real observation and somebody has to be able to act
    on it -- and the verdict is withheld, in the spelling this module already
    uses for a measurement nobody took (GOVERNANCE 10).
    """
    context = _continuation_page_context(monkeypatch, reason="continuation-page-no-act-anchor")

    finding = RUN.testimony_content_findings(context)[1]

    measured = finding["by_chair"]["attestator_1"]
    assert measured["attached_spans"] == []
    assert measured["uncovered_non_whitespace"] == {
        "ranges": [{"start": 0, "end": 8}, {"start": 10, "end": 11}],
        "count": 9,
    }
    assert finding["shortfall"] is None
    # Every fact the ruling asked the record to name, each asserted on its own.
    assert "continuation-page-no-act-anchor" in finding["reason"]
    assert "page 1's testimony content coverage is unmeasured" in finding["reason"]
    assert "chair 'attestator_1' saw 9 uncovered non-whitespace character(s)" in finding["reason"]
    assert "act-1 declared unanchored" in finding["reason"]
    assert "continuation-page alignment in the Perlector" in finding["reason"]
    # Unmeasured is not a hold: `None` routes like absence, never like a cause.
    assert (
        RUN.review_route_from_findings(
            testimony_shortfall=finding["shortfall"],
            audit_unresolved=False,
            under_witnessed=False,
        )
        is None
    )


def test_an_ordinary_unaligned_row_still_measures_a_real_shortfall(monkeypatch):
    """The counterfactual for the rule above: only the declaration withholds the verdict.

    Same page, same text, same empty span union -- but the alignment failed for
    a reason the Perlector measured rather than declared, so the uncovered text
    IS a shortfall and it routes. Without this, the change above would read as
    "an empty span union never counts", which would silence real findings.
    """
    context = _continuation_page_context(monkeypatch, reason="no-overlap-with-act-anchor")

    finding = RUN.testimony_content_findings(context)[1]

    assert finding["by_chair"]["attestator_1"]["uncovered_non_whitespace"]["count"] == 9
    assert finding["shortfall"] is True
    assert "reason" not in finding
    outcome, _reason = RUN.review_route_from_findings(
        testimony_shortfall=finding["shortfall"],
        audit_unresolved=False,
        under_witnessed=False,
    )
    assert outcome == "held-for-review"


def test_a_measured_shortfall_on_the_same_page_outranks_the_unmeasured_one(monkeypatch):
    """Two chairs, two verdicts: the one that was actually measured decides.

    A page can carry both -- one chair whose acts are all declared unanchorable,
    another whose aligned spans left real text uncovered. Withholding the page's
    verdict for the first would bury the second, so the measured shortfall wins
    and the unmeasured half is recorded beside it rather than dropped.
    """
    context = _continuation_page_context(monkeypatch, reason="continuation-page-no-act-anchor")
    second = _page_testimonium(
        outcome="read", retained="alphaXYZ \tQ", artifact_id="page-witness-2"
    )
    second["payload"]["page_role"] = "continuation"
    second["payload"]["chair"] = "attestator_3"
    # The sealed attempt identity binds the chair, so a second chair's record
    # needs its own; `latest_attempt` refuses one that does not derive.
    second["attempt_id"] = attempt_id("page-1", "read:attestator_3", 1)
    context.tree.records["page-witness-2"] = second
    row = dict(context.tree.records["attachment-1"]["payload"]["attachments"][0])
    row["chair"] = "attestator_3"
    row["alignment"] = {"status": "unaligned", "reason": "no-overlap-with-act-anchor"}
    row["testimonium_ref"] = context.artifact_ref(
        RUN.ATTESTATORES, "page-testimonium", "page-witness-2"
    )
    context.tree.records["attachment-1"]["payload"]["attachments"].append(row)

    finding = RUN.testimony_content_findings(context)[1]

    assert finding["shortfall"] is True
    assert "continuation-page-no-act-anchor" in finding["unmeasured_reason"]
    assert "chair 'attestator_1'" in finding["unmeasured_reason"]


def _mixed_page_context(monkeypatch, *, anchor_reason=None):
    """One page, one chair, two acts: one starting here, one continuing through.

    The commonest real shape at the head of a continuation. `act-1` is marked
    out on page 1 and its page row aligns there; `act-2` is primary on page 2
    and its page-1 row carries the forced unaligned declaration. `page_role` is
    `mixed`, which is exactly what `reconcile_page_roles` re-derives from these
    two attachments. `anchor_reason` replaces `act-1`'s aligned span with an
    ordinary unaligned row, which is how the same page reaches an empty span
    union without changing anything else about it.
    """
    page = _page_testimonium(outcome="read", retained="alphaXYZ \tQ")
    page["payload"]["page_role"] = "mixed"
    context = _context(page)
    anchored = _attachment(context, end=5)
    if anchor_reason is not None:
        anchored["payload"]["attachments"][0]["attached"] = False
        anchored["payload"]["attachments"][0]["alignment"] = {
            "status": "unaligned",
            "reason": anchor_reason,
        }
    context.tree.records["attachment-1"] = anchored
    continuation = _attachment(context, end=0)
    continuation["artifact_id"] = "attachment-2"
    continuation["subject_id"] = "act-2"
    continuation["attempt_id"] = attempt_id("act-2", "act-attachment", 1)
    continuation["payload"]["attachments"][0]["attached"] = False
    continuation["payload"]["attachments"][0]["alignment"] = {
        "status": "unaligned",
        "reason": "continuation-page-no-act-anchor",
    }
    context.tree.records["attachment-2"] = continuation
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            {"act_id": "act-1", "act_key": "a1", "page_ordinal": 1},
            {"act_id": "act-2", "act_key": "a2", "page_ordinal": 2},
        ],
    )
    monkeypatch.setattr(
        RUN,
        "artifacts_for",
        lambda *unused: [
            {
                "payload": {
                    "origin": "proposal",
                    "transform": {
                        "source_page_id": "page-1",
                        "source_page_ordinal": 1,
                        "bounds": {"x": 0, "y": 0, "w": 10, "h": 10},
                    },
                }
            }
        ],
    )
    return context


def test_a_mixed_pages_uncovered_text_is_measured_beside_a_declared_continuation(monkeypatch):
    """The declaration withholds the verdict only where it empties the union.

    A page where one act starts and another continues through still has a real
    span union -- the starting act's -- so the text outside it was measured, and
    calling that unmeasured would hide a genuine coverage loss behind the
    neighbouring act's declaration (GOALS 1). The unmeasured half is still
    recorded beside the verdict rather than dropped (GOVERNANCE 2).
    """
    context = _mixed_page_context(monkeypatch)

    finding = RUN.testimony_content_findings(context)[1]

    measured = finding["by_chair"]["attestator_1"]
    assert measured["attached_spans"] == [{"start": 0, "end": 5, "act_id": "act-1"}]
    assert measured["uncovered_non_whitespace"]["count"] == 4
    assert finding["shortfall"] is True
    # The page carries a verdict, so the declaration is recorded beside it and
    # never as the page's own reason.
    assert "reason" not in finding
    assert "act-2 declared unanchored" in finding["unmeasured_reason"]
    assert "continuation-page-no-act-anchor" in finding["unmeasured_reason"]
    # Scoped to the declared acts, never to the page: this page WAS measured,
    # and the page-wide sentence would be a false statement about it.
    assert "every act attachment" not in finding["unmeasured_reason"]
    outcome, _reason = RUN.review_route_from_findings(
        testimony_shortfall=finding["shortfall"],
        audit_unresolved=False,
        under_witnessed=False,
    )
    assert outcome == "held-for-review"


def test_the_same_page_without_an_aligned_span_is_unmeasured_again(monkeypatch):
    """The counterfactual on the same fixture: the span union decides, not the acts.

    Identical page, identical declaration, identical uncovered text -- only the
    starting act's span is gone. With nothing for the page text to be diffed
    against, the count is kept and the verdict is withheld, exactly as for a
    page whose every act continues through.
    """
    context = _mixed_page_context(monkeypatch, anchor_reason="no-overlap-with-act-anchor")

    finding = RUN.testimony_content_findings(context)[1]

    measured = finding["by_chair"]["attestator_1"]
    assert measured["attached_spans"] == []
    assert measured["uncovered_non_whitespace"]["count"] == 9
    assert finding["shortfall"] is None
    assert "page 1's testimony content coverage is unmeasured" in finding["reason"]
    assert "act-2 declared unanchored" in finding["reason"]
    assert (
        RUN.review_route_from_findings(
            testimony_shortfall=finding["shortfall"],
            audit_unresolved=False,
            under_witnessed=False,
        )
        is None
    )


def test_every_page_an_act_spans_but_is_not_primary_on_is_restated():
    """The derivation F2 named: the residual-ink rule, applied to this measurement.

    Both fixture acts are primary on page 1, so page 2's finding reached no
    review at all before this. The row is present and empty for an act that
    spans one page, so "spans no continuation" is not silence.
    """
    measured = {"attached_spans": [], "uncovered_non_whitespace": {"count": 34}}
    findings = {
        2: {
            "by_chair": {"attestator_1": measured},
            "shortfall": None,
            "reason": "unmeasured, and here is why",
        },
    }
    regions = [
        {"payload": {"transform": {"source_page_ordinal": ordinal}}} for ordinal in (1, 2, 2)
    ]

    rows = RUN.testimony_content_for_continuation_pages(findings, regions, 1)

    assert rows == [
        {
            "page_ordinal": 2,
            "by_chair": {"attestator_1": measured},
            "shortfall": None,
            "reason": "unmeasured, and here is why",
        }
    ]
    assert RUN.testimony_content_for_continuation_pages(findings, regions[:1], 1) == []
    # A private copy per consumer, exactly as `testimony_content_for_page` gives
    # -- and private all the way down, not only at the top level. The row's
    # evidence is the nested `by_chair` object; a shallow copy would leave every
    # act's review sharing one page's counts, so an edit made while preparing a1
    # would reach a2's still-to-be-published record. Both depths are pinned,
    # because only the deeper one fails if `copy.deepcopy` becomes `dict(...)`.
    rows[0]["shortfall"] = True
    rows[0]["by_chair"]["attestator_1"]["uncovered_non_whitespace"]["count"] = 0
    assert findings[2]["shortfall"] is None
    assert findings[2]["by_chair"]["attestator_1"]["uncovered_non_whitespace"]["count"] == 34


def test_a_continuation_page_with_no_finding_at_all_is_restated_as_unavailable():
    """Absence stays absence: never a measured clean page (GOVERNANCE 10)."""
    regions = [{"payload": {"transform": {"source_page_ordinal": 2}}}]

    assert RUN.testimony_content_for_continuation_pages({}, regions, 1) == [
        {"page_ordinal": 2, **RUN.NO_PAGE_CONTENT_COVERAGE}
    ]


def test_content_coverage_uses_only_the_current_retained_page_testimonium(monkeypatch):
    historical = _page_testimonium(outcome="read", retained="obsolete", attempt_ordinal=1)
    current = _page_testimonium(outcome="read", retained="new", attempt_ordinal=2)
    context = _context(historical, current)
    context.tree.records["attachment-1"] = _attachment(
        context, end=3, testimonium_id="page-witness-2"
    )
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    finding = RUN.testimony_content_findings(context)[1]

    assert finding == {
        "by_chair": {
            "attestator_1": {
                "attached_spans": [{"start": 0, "end": 3, "act_id": "act-1"}],
                "uncovered_non_whitespace": {"ranges": [], "count": 0},
            }
        },
        "shortfall": False,
    }


def test_ambiguous_current_page_testimonia_refuse():
    first = _page_testimonium(outcome="read", retained="first", artifact_id="page-witness-a")
    duplicate = _page_testimonium(
        outcome="read", retained="duplicate", artifact_id="page-witness-b"
    )

    with pytest.raises(FatalAccounting, match="duplicate attempt ordinal 1"):
        RUN.testimony_content_findings(_context(first, duplicate))


def test_geometry_coverage_accepts_a_matching_residual_partition(monkeypatch):
    context = _context(_conservation("conservation-1", components=[_component()]))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"}],
    )

    assert RUN.geometry_coverage_inputs(context) == {
        1: {
            "ink_measurable": True,
            "residual_component_count": 1,
            "residual_act_count": 1,
            # The same key set every other shape this function returns carries,
            # so a consumer reads one schema and finds absence as a value. The
            # bound was in force on this page and is named; nothing was withheld
            # and there is nothing to say about it.
            "residual_enumeration": RESIDUAL_ENUMERATION_COMPLETE,
            "max_residual_components": 2000,
            "page_residual_act_count": 0,
            "reason": None,
        }
    }


def test_every_geometry_coverage_shape_carries_the_same_keys(monkeypatch):
    """One schema, three answers -- absence is a value here, never a missing key.

    An enumerated page, a page held as one review item, and a page with no
    conservation record at all reach the same review payload field, and a
    consumer branching on `residual_enumeration` or reading `reason` may not
    have to know which of the three it got before it can look.
    """
    enumerated = _context(_conservation("conservation-1", components=[_component()]))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"}],
    )
    complete = RUN.geometry_coverage_inputs(enumerated)[1]

    withheld_context = _context(
        _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=10)
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])
    withheld = RUN.geometry_coverage_inputs(withheld_context)[1]

    assert set(complete) == set(withheld) == set(RUN.NO_PAGE_CONSERVATION)
    assert complete["residual_enumeration"] != withheld["residual_enumeration"]
    assert complete["reason"] is None and withheld["reason"] is not None


def test_an_enumerated_record_naming_no_integer_bound_is_refused(monkeypatch):
    """The bound is on every record, so a record without one is not reconcilable.

    The withheld branch has always held its record to an integer bound; the
    enumerated branch now publishes that bound in its own finding, and a value
    published to a reviewer is checked rather than passed through.
    """
    context = _context(_conservation("conservation-1", components=[_component()], bound=None))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"}],
    )

    with pytest.raises(FatalAccounting, match="no integer max_residual_components"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_divergent_residual_partition(monkeypatch):
    context = _context(_conservation("conservation-1", components=[_component()]))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="held-act partition diverges"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_non_object_component(monkeypatch):
    context = _context(_conservation("conservation-1", components=[None]))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="page 1.*component 0.*malformed"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_malformed_component_bounds(monkeypatch):
    context = _context(
        _conservation(
            "conservation-1",
            components=[_component(bounds={"x": 2, "y": 3, "w": 4, "h": True})],
        )
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="page 1.*component 0.*malformed"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_non_integer_component_pixel_count(monkeypatch):
    context = _context(_conservation("conservation-1", components=[_component(pixel_count=True)]))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="page 1.*component 0.*malformed"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_negative_component_pixel_count(monkeypatch):
    context = _context(
        _conservation(
            "conservation-1",
            components=[_component(pixel_count=-1), _component(pixel_count=13)],
        )
    )
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            {"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"},
            {"act_key": "residual:1:1", "page_ordinal": 1, "outcome": "held"},
        ],
    )

    with pytest.raises(FatalAccounting, match="page 1.*component 0.*malformed"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_mismatched_pixel_arithmetic(monkeypatch):
    context = _context(_conservation("conservation-1", counts=(12, 5, 6)))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="page 1 pixel accounting does not reconcile"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_malformed_pixel_count_type(monkeypatch):
    context = _context(_conservation("conservation-1", counts=(12, True, 12)))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="page 1 has malformed measured pixel counts"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_residual_component_sum_mismatch(monkeypatch):
    context = _context(
        _conservation("conservation-1", components=[_component()], counts=(13, 0, 13))
    )
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"}],
    )

    with pytest.raises(FatalAccounting, match="page 1 residual component pixel sum"):
        RUN.geometry_coverage_inputs(context)


def test_unmeasured_geometry_requires_all_pixel_counts_to_be_none(monkeypatch):
    context = _context(_conservation("conservation-1", measurable=False, counts=(0, None, None)))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="unmeasured.*page 1.*must carry None"):
        RUN.geometry_coverage_inputs(context)


def test_unmeasured_geometry_cannot_list_residual_components(monkeypatch):
    context = _context(_conservation("conservation-1", measurable=False, components=[_component()]))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="unmeasured.*page 1.*no residual components"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_malformed_page_facts(monkeypatch):
    context = _context(_conservation("conservation-1", ordinal=True))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="malformed or duplicate page facts"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_duplicate_page_facts(monkeypatch):
    context = _context(
        _conservation("conservation-1"),
        _conservation("conservation-2"),
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="malformed or duplicate page facts"):
        RUN.geometry_coverage_inputs(context)


def test_unmeasured_geometry_cannot_mint_a_residual_act(monkeypatch):
    context = _context(_conservation("conservation-1", measurable=False))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"}],
    )

    with pytest.raises(FatalAccounting, match="unmeasured.*minted residual acts"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_an_unknown_residual_enumeration(monkeypatch):
    """A third spelling is not a page fact this stage may guess at.

    Defaulting an unrecognised value to "complete" would pass a page with
    nothing listed as fully enumerated, which is the exact loss the field
    exists to make impossible.
    """
    context = _context(_conservation("conservation-1", enumeration="partial"))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="records its residual enumeration as 'partial'"):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_names_an_unhashable_residual_enumeration_by_value(monkeypatch):
    """A list at this field is refused the same way an unknown string is.

    `RESIDUAL_ENUMERATIONS` (`common/stage.py`) is a plain tuple, so
    membership is decided by equality against each member, never by hashing
    the candidate -- an unhashable JSON value here does not raise `TypeError`,
    it is simply never equal to either sealed spelling and falls into the
    same named refusal a bad string would.
    """
    context = _context(_conservation("conservation-1", enumeration=["complete"]))
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(
        FatalAccounting, match=r"records its residual enumeration as \['complete'\]"
    ):
        RUN.geometry_coverage_inputs(context)


def test_geometry_coverage_refuses_a_missing_residual_enumeration(monkeypatch):
    """Absence is the same failure arriving by omission, and refuses the same way."""
    record = _conservation("conservation-1")
    del record["payload"]["residual_enumeration"]
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="records its residual enumeration as None"):
        RUN.geometry_coverage_inputs(_context(record))


def test_geometry_coverage_refuses_a_count_the_component_list_does_not_support(monkeypatch):
    """An enumerated page's count is recomputed from the list beside it."""
    context = _context(_conservation("conservation-1", components=[_component()], declared_count=7))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"}],
    )

    with pytest.raises(FatalAccounting, match="names residual_component_count 7 but lists 1"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_is_a_named_finding_rather_than_a_malformed_record(monkeypatch):
    """The branch this unit exists for: a withheld record is read, not accused.

    Before it, an omitted `residual_components` key fell through to the
    malformed-facts refusal, so the operator was told the Designator wrote a
    broken artifact when what actually happened was that a page was held under
    a sealed policy.
    """
    context = _context(
        _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=10)
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    findings = RUN.geometry_coverage_inputs(context)

    assert findings[1]["ink_measurable"] is True
    assert findings[1]["residual_component_count"] == 11
    assert findings[1]["residual_act_count"] == 0
    assert findings[1]["page_residual_act_count"] == 1
    assert findings[1]["residual_enumeration"] == RESIDUAL_ENUMERATION_WITHHELD
    assert findings[1]["max_residual_components"] == 10
    assert "11 residual components against the sealed bound of 10" in findings[1]["reason"]
    # Each act's review gets its own copy, exactly as an enumerated page's does.
    assert RUN.geometry_coverage_for(findings, 1) == findings[1]
    assert RUN.geometry_coverage_for(findings, 1) is not findings[1]


def test_a_withheld_page_with_no_page_residual_act_is_a_silent_loss(monkeypatch):
    context = _context(
        _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=10)
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [])

    with pytest.raises(FatalAccounting, match="accounted for by 0 page-residual acts"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_may_not_also_mint_its_components(monkeypatch):
    context = _context(
        _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=10)
    )
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            _page_residual_act(),
            {"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"},
        ],
    )

    with pytest.raises(FatalAccounting, match="still minted 1 per-component residual acts"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_that_kept_its_component_list_refuses(monkeypatch):
    context = _context(
        _conservation(
            "conservation-1",
            enumeration=RESIDUAL_ENUMERATION_WITHHELD,
            bound=10,
            keep_components=[],
        )
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="still carries a residual_components key"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_without_a_usable_integer_bound_refuses(monkeypatch):
    """The count-and-bound refusal is exercised, not merely present in source.

    A page held with no `max_residual_components` at all -- or one that is a
    float or a bool rather than a plain int -- must not fall through to the
    `count <= bound` comparison and be silently accepted or misreported; a
    mutation deleting this check left every test in this file green while a
    withheld page with an absent bound was reported to a reviewer as held
    against a sealed bound of 0.
    """
    context = _context(
        _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=10.0)
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="no integer bound it was judged against"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_with_a_boolean_bound_refuses(monkeypatch):
    context = _context(
        _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=True)
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="no integer bound it was judged against"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_with_no_bound_at_all_refuses(monkeypatch):
    record = _conservation("conservation-1", enumeration=RESIDUAL_ENUMERATION_WITHHELD, bound=10)
    del record["payload"]["max_residual_components"]
    context = _context(record)
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="no integer bound it was judged against"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_page_within_its_own_bound_refuses(monkeypatch):
    context = _context(
        _conservation(
            "conservation-1",
            enumeration=RESIDUAL_ENUMERATION_WITHHELD,
            bound=10,
            declared_count=10,
        )
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="counted 10 residual components against a bound"):
        RUN.geometry_coverage_inputs(context)


def test_an_unmeasured_page_may_not_withhold_an_enumeration(monkeypatch):
    """Nothing was measured, so nothing was counted for a bound to stop."""
    context = _context(
        _conservation(
            "conservation-1",
            measurable=False,
            enumeration=RESIDUAL_ENUMERATION_WITHHELD,
            bound=10,
        )
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="unmeasured.*withheld its residual enumeration"):
        RUN.geometry_coverage_inputs(context)


def test_an_enumerated_page_may_not_also_be_held_as_one_item(monkeypatch):
    """One disposition per page: N held acts, or the one item that replaced them."""
    context = _context(_conservation("conservation-1", components=[_component()]))
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            _page_residual_act(),
            {"act_key": "residual:1:0", "page_ordinal": 1, "outcome": "held"},
        ],
    )

    with pytest.raises(FatalAccounting, match="enumerated its residual components and is also"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_pages_ink_accounting_is_still_reconciled(monkeypatch):
    """The components are not listed; the pixels still have to add up."""
    context = _context(
        _conservation(
            "conservation-1",
            enumeration=RESIDUAL_ENUMERATION_WITHHELD,
            bound=10,
            counts=(100, 40, 40),
        )
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="pixel accounting does not reconcile"):
        RUN.geometry_coverage_inputs(context)


def test_a_withheld_pages_malformed_pixel_counts_are_refused(monkeypatch):
    """The withheld shape's own copy of the malformed-count check, pinned directly.

    `geometry_coverage_inputs` and `_withheld_page_conservation` each held this
    check separately until they were folded into one shared helper
    (`_require_reconciled_pixels`); nothing in this file exercised the withheld
    copy on its own, so a mutation that dropped it left the whole suite green.
    This pins the withheld call site by name rather than inferring it from the
    enumerated page's coverage above.
    """
    context = _context(
        _conservation(
            "conservation-1",
            enumeration=RESIDUAL_ENUMERATION_WITHHELD,
            bound=10,
            counts=(12, True, 12),
        )
    )
    monkeypatch.setattr(RUN, "expected_acts", lambda unused: [_page_residual_act()])

    with pytest.raises(FatalAccounting, match="page 1 has malformed measured pixel counts"):
        RUN.geometry_coverage_inputs(context)


def test_a_non_held_act_requires_its_pages_conservation_record(monkeypatch):
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_key": "declared:1", "page_ordinal": 7, "outcome": "proposed"}],
    )

    with pytest.raises(FatalAccounting, match="page 7.*no conservation record"):
        RUN.geometry_coverage_inputs(_context())


def test_an_all_held_page_keeps_absent_conservation_as_absence(monkeypatch):
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [
            {"act_key": "declared:1", "page_ordinal": 7, "outcome": "held"},
            {"act_key": "declared:2", "page_ordinal": 7, "outcome": "held"},
        ],
    )

    findings = RUN.geometry_coverage_inputs(_context())

    assert findings == {}
    assert RUN.geometry_coverage_for(findings, 7) == RUN.NO_PAGE_CONSERVATION


def test_a_page_with_no_conservation_record_is_not_recorded_as_measured():
    """F-O1: an absent record may not read as `ink_measurable: False`.

    `refused-first-page` reaches this exactly: page 1 never sealed, so the
    Designator published no conservation record for it, and both its acts are
    reviewed anyway.
    """
    measured = {
        1: {"ink_measurable": False, "residual_component_count": 0, "residual_act_count": 0}
    }

    absent = RUN.geometry_coverage_for(measured, 2)

    assert absent["ink_measurable"] is None
    assert absent["residual_component_count"] is None
    assert absent["residual_act_count"] is None
    assert "no conservation record" in absent["reason"]
    assert absent != RUN.geometry_coverage_for(measured, 1)


def test_each_act_gets_a_private_copy_of_its_pages_geometry_finding():
    """F-O1: the sibling-aliasing fix covers every once-per-page finding."""
    findings = {1: {"ink_measurable": True, "residual_component_count": 1, "residual_act_count": 1}}

    first = RUN.geometry_coverage_for(findings, 1)
    sibling = RUN.geometry_coverage_for(findings, 1)
    first["residual_act_count"] = 99

    assert sibling["residual_act_count"] == 1
    assert findings[1]["residual_act_count"] == 1
    assert RUN.geometry_coverage_for({}, 7) is not RUN.NO_PAGE_CONSERVATION


def test_a_non_textual_reported_page_body_is_named_as_its_own_fault(monkeypatch):
    """F-O2: two distinct producer faults may not share one refusal string."""


def test_a_structured_native_page_payload_is_retained_without_text_coverage(monkeypatch):
    """A declared non-comparable native payload is not forged into text."""
    context = _context(_page_testimonium(outcome="read", retained={"lines": []}))
    context.tree.records["attachment-1"] = _attachment(context, end=0)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    assert RUN.testimony_content_findings(context) == {}


def test_content_and_audit_holds_compose_in_stable_recorded_order():
    """V5: R6 coverage wins precedence, but neither active cause disappears."""
    outcome, reason = RUN.review_route_from_findings(
        testimony_shortfall=True,
        audit_unresolved=True,
        under_witnessed=True,
        unreconciled=True,
    )

    assert outcome == "held-for-review"
    assert reason.index("testimony coverage") < reason.index("audit re-proof cap")
    assert reason.index("audit re-proof cap") < reason.index("witness floor")
    assert reason.index("witness floor") < reason.index("did not reconcile")
