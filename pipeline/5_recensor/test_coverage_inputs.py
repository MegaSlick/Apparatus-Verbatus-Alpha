"""Focused audit tests for R6's new geometry and testimony coverage inputs."""

import importlib.util
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.identities import attempt_id
from common.native_witness import partition_disagreement

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


def _page_testimonium(*, outcome, reported=..., attempt_ordinal=1, artifact_id=None):
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
    if reported is not ...:
        payload["reported"] = reported
    return {
        "artifact_id": artifact_id or f"page-witness-{attempt_ordinal}",
        "stage": RUN.ATTESTATORES,
        "kind": "page-testimonium",
        "subject_id": subject_id,
        "attempt_id": attempt_id(subject_id, f"read:{chair}", attempt_ordinal),
        "outcome": outcome,
        "payload": payload,
    }


def _conservation(artifact_id, *, ordinal=1, measurable=True, components=None, counts=...):
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
    return {
        "artifact_id": artifact_id,
        "stage": RUN.DESIGNATOR,
        "kind": "conservation",
        "subject_id": f"page-{artifact_id}",
        "outcome": "measured",
        "payload": {
            "page_ordinal": ordinal,
            "ink_measurable": measurable,
            "total_ink_pixel_count": total,
            "claimed_pixel_count": claimed,
            "residual_pixel_count": residual,
            "residual_components": residual_components,
        },
    }


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


def _page_fact(*, ordinal, attached, anchor_basis=None):
    alignment = (
        {
            "status": "aligned",
            "anchor_basis": anchor_basis,
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
        "attachment_basis": "geometric-overlap" if attached else "unattached",
        "testimonium_ref": {"artifact_id": f"pt-{ordinal}"},
        "content_health": {"truncated": False},
        "alignment": alignment,
    }


def test_artifact_tree_preserves_stage_ownership_in_manifests_and_reads():
    page = _page_testimonium(outcome="read", reported="page text")
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
    act_reading = {
        "artifact_id": "act-reading-1",
        "stage": RUN.ATTESTATORES,
        "kind": "testimonium",
        "subject_id": "act-1",
        "outcome": "read",
        "payload": {"chair": "attestator_1", "reported": "real act text"},
    }
    context = _context(act_reading, _page_testimonium(outcome="read"))
    context.tree.records["attachment-1"] = _attachment(context, end=0)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    with pytest.raises(FatalAccounting, match="reading page Testimonium has no reported text"):
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
    page = _page_testimonium(outcome="read", reported="ink")
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
    ),
)
def test_a_proposal_rectangle_short_of_its_four_numbers_is_named_not_indexed(monkeypatch, bounds):
    """These rectangles travel on as `proposal_boxes` and into
    `unrouted_observations`, and both index the four sides by name. Without the
    check the stage that decides whether recovery runs leaves as a bare
    `KeyError` naming neither page nor act, which is the traceback an operator
    cannot repair a region from."""
    page = _page_testimonium(outcome="read", reported="ink")
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
    page = _page_testimonium(outcome="read", reported="ink")
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
    page = _page_testimonium(outcome="read", reported=text)
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


def _patched_page_geometry(monkeypatch, context, observed_by_ordinal):
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
            "payload": {
                "chair": "attestator_1",
                "page_ordinal": ordinal,
                "observed": observed,
            },
        }

    monkeypatch.setattr(context.tree, "read_artifact_reference", fake_reference, raising=False)


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

    facts = RUN.act_attachment_facts(context, "act-1", {"attestator_1": "read"})

    assert facts["attestator_1"]["attached"] is True
    assert facts["attestator_1"]["anchor_basis"] == "act-anchor"


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

    facts = RUN.act_attachment_facts(context, "act-1", {"attestator_1": "read"})

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

    facts = RUN.act_attachment_facts(context, "act-1", {"attestator_1": "read"})

    assert facts["attestator_1"]["anchor_basis"] == "act-line-not-located"


def test_page_attachment_facts_refuse_a_duplicate_page_pair(monkeypatch):
    rows = [
        _page_fact(ordinal=1, attached=False),
        _page_fact(ordinal=1, attached=False),
    ]
    context = _context(_attachment_fact_record(rows))
    _patched_page_geometry(monkeypatch, context, {1: False})

    with pytest.raises(FatalAccounting, match="repeats attachment pair.*exactly one row"):
        RUN.act_attachment_facts(context, "act-1", {"attestator_1": "read"})


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
        RUN.act_attachment_facts(context, "act-1", {"attestator_1": "read"})


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
    assert "no page witness reported text for this page" in content["reason"]
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
    page = _page_testimonium(outcome="read", reported="alphaXYZ \tQ")
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


def test_content_coverage_uses_only_the_current_retained_page_testimonium(monkeypatch):
    historical = _page_testimonium(outcome="read", reported="obsolete", attempt_ordinal=1)
    current = _page_testimonium(outcome="read", reported="new", attempt_ordinal=2)
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
    first = _page_testimonium(outcome="read", reported="first", artifact_id="page-witness-a")
    duplicate = _page_testimonium(
        outcome="read", reported="duplicate", artifact_id="page-witness-b"
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
        }
    }


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
    context = _context(_page_testimonium(outcome="read", reported={"lines": []}))
    context.tree.records["attachment-1"] = _attachment(context, end=0)
    monkeypatch.setattr(
        RUN,
        "expected_acts",
        lambda unused: [{"act_id": "act-1", "act_key": "a1", "page_ordinal": 1}],
    )

    with pytest.raises(FatalAccounting, match="reported page text is not text"):
        RUN.testimony_content_findings(context)


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
