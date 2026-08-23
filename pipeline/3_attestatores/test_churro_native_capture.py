"""Churro's full-page capture boundary, driven through the stage program.

Unit 12 replaced the fixture-only Churro serve with a real capture: raw bytes
retained before any parse, XML validated afterwards, truncation visible from the
transport's own stop reason, and post-hoc repetition detection that can never
reach the captured bytes.  Every one of those behaviours was pinned by unit
tests calling `captured_churro_page_attempt` directly -- and both defects the
audit chain found were found by *reading*, because no scenario ran the path.

That is the specific failure this project has already met once.  The Sol-S1
fallback branch wrote `genuinely-empty` for three chairs on a page none of them
was asked about, and it was green the whole time, because the code that would
have contradicted it was never executed by a scenario.  A capture path no
scenario runs is a page-witness mechanism that can be disabled without a single
test going red.

So the `churro-native` scenario declares four real-format Churro page responses
(`proof/build_fixture.py::CHURRO_PAGE_RESPONSES`) and this module runs the whole
stage program over them, asserting on what lands on disk:

* page 1 / attestator_1 -- parses, complete, and opens with a rubric line that
  belongs to no act.  An act-ordered concatenation structurally cannot contain
  that line, so its presence is what makes "the act attachment is derived rather
  than assumed" (this unit's first definition-of-done bullet) a measurement:
  each act's span must still land on its own words, and the header must surface
  as page text no act accounts for rather than quietly joining a neighbour.
* page 1 / attestator_3 -- parses, complete.  Both acts keep three witnesses on
  their primary page, so nothing here is really a witness-floor test wearing a
  capture test's name.
* page 2 / attestator_1 -- parses, and the transport stopped it at `length`.
  Kept, marked truncated, never completed and never re-asked (GOVERNANCE 7).
* page 2 / attestator_3 -- never closes its `<output>` element and was cut at
  `length`.  The raw bytes are retained anyway, the record is `failed` with
  `recordable=false`, and the reason names the cut as well as the parse refusal.

`happy` is asserted here too, and separately, because it is a PINNED scenario:
its four rows reproduce the previous synthetic join text exactly, so the pinned
reference run gains the real boundary while no act's reading moves.

The unit-level tests of this seam -- scenario precedence, the refusal of a
declaration no page chair could be asked for, the cut-off empty response -- live
beside the rest of `captured_churro_page_attempt`'s in `test_feeding.py`. What
is here needs a real run tree. The one exception is the reading-order test at the
end, which needs no run tree but belongs with the reading-order claim it bounds.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from common.contracts.stages import ATTESTATORES, RECENSOR
from common.runtree.store import RunTree

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "pipeline/orchestrator/run.py"
FIXTURE = "synthetic-two-page-v0"
HEADER = "[FOLIO RUBRIC 7 -- page furniture, belongs to no entry]"


def _orchestrate(run_root: Path, scenario: str):
    return subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR),
            "--fixture",
            FIXTURE,
            "--scenario",
            scenario,
            "--run-id",
            "r",
            "--run-root",
            str(run_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _page_testimonia(tree: RunTree) -> dict[tuple[int, str], dict]:
    records = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "page-testimonium":
            continue
        record = tree.read_artifact(ATTESTATORES, "page-testimonium", entry["artifact_id"])
        payload = record["payload"]
        records[(payload["page_ordinal"], payload["chair"])] = record
    return records


def _attachments(tree: RunTree) -> dict[str, list[dict]]:
    by_act = {}
    for entry in tree.build_manifest(ATTESTATORES)["artifacts"]:
        if entry["kind"] != "act-attachment":
            continue
        record = tree.read_artifact(ATTESTATORES, "act-attachment", entry["artifact_id"])
        by_act[record["payload"]["act_key"]] = record["payload"]["attachments"]
    return by_act


@pytest.fixture(scope="module")
def native_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-native") / "runs"
    result = _orchestrate(root, "churro-native")
    # `partial` and not `complete`, and that is the scenario working rather than
    # failing: the captured page reading carries a rubric line no sealed proposal
    # accounts for, and the Recensor holds both acts on it. Page text nobody can
    # attribute is exactly what must NOT pass silently (GOALS 1, GOVERNANCE 2).
    assert result.returncode == 3, result.stderr
    return RunTree(root, "r")


@pytest.fixture(scope="module")
def happy_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("churro-happy") / "runs"
    result = _orchestrate(root, "happy")
    assert result.returncode == 0, result.stderr
    return RunTree(root, "r")


def test_a_captured_page_reading_parses_and_keeps_its_raw_bytes(native_run):
    """The narrow waist, on disk: verbatim bytes in the blob, validated text above."""
    record = _page_testimonia(native_run)[(1, "attestator_1")]
    payload = record["payload"]
    capture = payload["native_capture"]

    assert record["outcome"] == "read"
    assert capture["adapter"] == "churro.v1"
    assert capture["parse"]["state"] == "parsed"
    assert payload["payload"] == capture["parse"]["text"]
    assert payload["payload"].startswith(HEADER)
    # The retained blob is the authority and it is the WHOLE response, tags and
    # all -- the parsed text is a derived view of it, never a replacement.
    raw = native_run.read_bytes(capture["raw_response_ref"]["relative_path"])
    assert raw == f"<output>{payload['payload']}</output>".encode()
    assert payload["content_health"]["recordable"] is True
    assert payload["content_health"]["truncated"] is False
    assert payload["content_health"]["characters"] == len(payload["payload"])


def test_each_act_lands_on_its_own_words_across_page_furniture(native_run):
    """Definition of done 1: the act attachment is DERIVED, not assumed.

    The captured page opens with a header no act accounts for. If any part of
    this pipeline placed acts by proposal order rather than by locating each
    act's own text, that header would displace every span by its own length and
    act a1 would be recorded as having been read as the rubric line.
    """
    page_text = _page_testimonia(native_run)[(1, "attestator_1")]["payload"]["payload"]
    attachments = _attachments(native_run)

    spans = {}
    for act_key in ("a1", "a2"):
        entry = next(
            item
            for item in attachments[act_key]
            if item["chair"] == "attestator_1" and item["page_ordinal"] == 1
        )
        assert entry["page_witness"] is True
        assert entry["alignment"]["status"] == "aligned"
        span = entry["alignment"]["witness_span"]
        spans[act_key] = page_text[span["start"] : span["end"]]

    assert "SYNTHETIC ACT ONE" in spans["a1"]
    assert "SYNTHETIC ACT TWO" in spans["a2"]
    # The load-bearing half: neither act absorbed the other's text, and neither
    # absorbed the header. A span that quietly grew to cover page furniture is
    # how "every act is accounted for" becomes true by arithmetic alone.
    assert "SYNTHETIC ACT TWO" not in spans["a1"]
    assert "SYNTHETIC ACT ONE" not in spans["a2"]
    assert HEADER not in spans["a1"] and HEADER not in spans["a2"]


def test_page_text_no_act_accounts_for_holds_rather_than_disappearing(native_run):
    """The header is not silently dropped: it holds the acts on the page.

    This is the other half of a derived attachment. Deriving each act's span from
    the text means some captured text may belong to no act at all -- and a
    full-page witness is precisely the thing that picks up marginalia, running
    heads and rubrics. Losing it would be a missed act, which GOALS 1 calls worse
    than a poorly read one.
    """
    reviews = [
        native_run.read_artifact(RECENSOR, "review", entry["artifact_id"])
        for entry in native_run.build_manifest(RECENSOR)["artifacts"]
        if entry["kind"] == "review"
    ]
    assert reviews and all(review["outcome"] == "held-for-review" for review in reviews)
    for review in reviews:
        coverage = review["payload"]["testimony_content_coverage"]["by_chair"]["attestator_1"]
        assert coverage["uncovered_non_whitespace"]["count"] > 0
        assert "outside the ordered union" in review["payload"]["reason"]


def test_a_truncated_capture_is_visible_and_is_never_completed_or_retried(native_run):
    """Definition of done 2, end to end.

    The transport's own stop reason is the authority. The text that did arrive is
    kept -- a partial reading is evidence -- and it is marked truncated so no
    later stage can read it as a whole page.
    """
    record = _page_testimonia(native_run)[(2, "attestator_1")]
    payload = record["payload"]
    health = payload["content_health"]

    assert record["outcome"] == "read"
    assert health["truncated"] is True
    assert health["truncation_basis"] == "trusted-response-boundary"
    assert payload["native_capture"]["transport_stop_reason"] == "length"
    # Not completed: the retained text is exactly what the (cut) response said,
    # with nothing appended to round it off.
    raw = native_run.read_bytes(payload["native_capture"]["raw_response_ref"]["relative_path"])
    assert raw.decode() == f"<output>{payload['payload']}</output>"
    # Not retried: one attempt at one ordinal for this (page, chair), full stop.
    assert payload["attempt_ordinal"] == 1


def test_a_captured_response_that_cannot_be_parsed_keeps_its_bytes_and_names_the_cut(native_run):
    """A retained, unusable response is not an unheard chair -- and says why.

    `recordable=False` fixes every measured field at null, so `truncated` cannot
    carry the cut here however loudly the transport announced it. The reason and
    the basis are the only places left, and "not parseable XML" on its own sends
    an operator hunting a schema bug for a provider that simply stopped at its
    token bound.
    """
    record = _page_testimonia(native_run)[(2, "attestator_3")]
    payload = record["payload"]
    health = payload["content_health"]
    capture = payload["native_capture"]

    assert record["outcome"] == "failed"
    assert payload["payload"] is None
    # Not `recordable=None`: that shape means no channel at all (`dead`,
    # `not-run`), and this chair demonstrably answered.
    assert health["recordable"] is False
    assert health["encoding"] == "invalid-or-unrecordable"
    assert capture["parse"]["state"] == "failed"
    assert capture["transport_stop_reason"] == "length"
    assert "length" in health["truncation_basis"]
    assert "cut off" in health["truncation_basis"]
    assert "stopped the response at its bound" in payload["reason"]
    # The bytes survive the refusal. This is the whole point of capture-then-parse.
    raw = native_run.read_bytes(capture["raw_response_ref"]["relative_path"])
    assert raw == b"<output>" + HEADER.encode() + b"\nSYNTHETIC ACT TWO delta epsiIon zeta eta"
    assert not raw.endswith(b"</output>")


def test_a_failed_page_capture_does_not_claim_a_missing_anchor(native_run):
    """The failed page's act view names the response, not an absent anchor.

    Page 2 has no Chandra anchor of its own and its attachment reason says so.
    What must not happen is the other misdescription: an act attachment for a
    chair whose page response failed reporting `missing-chandra-page-anchor`,
    which sends a reader looking for a declaration that is on disk.
    """
    entry = next(
        item
        for item in _attachments(native_run)["a2"]
        if item["chair"] == "attestator_3" and item["page_ordinal"] == 2
    )
    assert entry["attached"] is False
    assert entry["alignment"]["reason"] != "missing-chandra-page-anchor"
    # And the entry points at the record that actually failed, so the failure is
    # one dereference away rather than inferred.
    reference = entry["testimonium_ref"]["relative_path"]
    assert json.loads((native_run.root / reference).read_bytes())["outcome"] == "failed"


def test_the_pinned_happy_run_captures_through_churro_without_moving_a_reading(happy_run):
    """The pinned reference run exercises the real boundary; its text is unmoved.

    A capture path only the unpinned scenarios run is a capture path a refactor
    can delete from the reference run unnoticed. Declaring `happy`'s responses as
    the exact text its synthetic join already produced buys the coverage without
    buying a change of evidence: what moves is the path and the retained capture
    record, not one character of any act's reading.
    """
    records = _page_testimonia(happy_run)
    assert set(records) == {
        (1, "attestator_1"),
        (1, "attestator_3"),
        (2, "attestator_1"),
        (2, "attestator_3"),
    }
    for (page_ordinal, chair), record in records.items():
        payload = record["payload"]
        capture = payload["native_capture"]
        assert record["outcome"] == "read", (page_ordinal, chair)
        assert capture["parse"]["state"] == "parsed"
        assert capture["transport_stop_reason"] == "eos"
        assert payload["content_health"]["truncated"] is False
        raw = happy_run.read_bytes(capture["raw_response_ref"]["relative_path"])
        assert raw == f"<output>{payload['payload']}</output>".encode()

    assert records[(1, "attestator_1")]["payload"]["payload"] == (
        "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    )
    assert records[(2, "attestator_3")]["payload"]["payload"] == (
        "SYNTHETIC ACT TWO delta epsiIon zeta eta"
    )


def test_a_witness_reading_order_that_departs_from_the_anchor_degrades_visibly():
    """A MEASURED limitation, recorded so it cannot be discovered as a surprise.

    Churro reads the whole page in its own order, and the act attachment is
    derived by matching each act's Chandra anchor line against that page text
    (`common/alignment.py::align_to_anchor`). That matcher is monotonic: it finds
    an in-order correspondence, so a witness that read a two-column page in the
    opposite order to Chandra's anchor cannot have both acts matched.

    What this pins is what the pipeline actually DOES about that, because the
    difference between a tolerable limitation and a defect is whether the loss is
    visible. Measured here: the out-of-order act simply gets no span, so its
    attachment goes `unaligned` and its text lands in the Recensor's uncovered
    page-text accounting -- a hold, not a wrong reading, and never a span quietly
    filled from the neighbour it did match. Conservative in the safe direction.

    Making alignment order-independent belongs to Unit 14, which owns the
    Perlector and Recensor over native testimony. If it lands, this test goes red
    and is rewritten deliberately, which is the point of writing it down.
    """
    from common.alignment import align_to_anchor, load_alignment_limits

    limits, _ = load_alignment_limits(ROOT / "config/alignment.toml")
    anchor = (
        "<p>SYNTHETIC ACT ONE alpha beta gamma </p><p>SYNTHETIC ACT TWO delta epsilon zeta eta</p>"
    )
    in_order = "SYNTHETIC ACT ONE alpha beta gamma\nSYNTHETIC ACT TWO delta epsilon zeta eta"
    swapped = "SYNTHETIC ACT TWO delta epsilon zeta eta\nSYNTHETIC ACT ONE alpha beta gamma"

    matched = align_to_anchor(in_order, anchor, limits)
    assert matched["status"] == "aligned"
    # The whole page corresponds, so both acts' anchor ranges are covered.
    assert [(span["anchor"]["start"], span["anchor"]["end"]) for span in matched["spans"]] == [
        (0, 75)
    ]

    reordered = align_to_anchor(swapped, anchor, limits)
    assert reordered["status"] == "aligned"
    covered = [(span["anchor"]["start"], span["anchor"]["end"]) for span in reordered["spans"]]
    # Only act a2's anchor range (35..75) is matched. Act a1's range 0..35 is
    # untouched, so its clipped span is empty and its attachment is refused with
    # `no-overlap-with-act-anchor` rather than being handed a2's characters.
    assert covered == [(35, 75)]
    assert not any(start < 35 for start, _ in covered)
