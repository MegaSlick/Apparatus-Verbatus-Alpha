"""Focused audit tests for R6's new geometry and testimony coverage inputs."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting
from common.contracts.identities import attempt_id

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
    payload = {"page_ordinal": 1, "chair": chair, "attempt_ordinal": attempt_ordinal}
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


def test_a_reading_page_testimonium_cannot_lose_its_reported_text_and_take_the_skip():
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

    with pytest.raises(FatalAccounting, match="reading page Testimonium has no reported text"):
        RUN.testimony_content_findings(context)


def test_a_non_reading_page_testimonium_still_has_no_content_to_compare():
    context = _context(_page_testimonium(outcome="failed"))

    assert RUN.testimony_content_findings(context) == {}


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


def test_a_non_textual_reported_page_body_is_named_as_its_own_fault():
    """F-O2: two distinct producer faults may not share one refusal string."""
    context = _context(_page_testimonium(outcome="read", reported={"lines": []}))

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
