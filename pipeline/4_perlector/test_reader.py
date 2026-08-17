"""The fixture reader recognizes and actually inspects minted fallback acts."""

import re
from pathlib import Path

import pytest
import reader as reader_module
from reader import FixtureReader

from common.contracts.errors import ContractError
from common.contracts.identities import act_id as derive_act_id
from common.imaging import encode_grayscale_png
from common.stage import FALLBACK_PAGE_ACT_ORDINAL

PAGE = {"ordinal": 3, "width": 200, "height": 260}
PAGE_ID = "pg_0000000000000000"
BOUNDS = {"x": 0, "y": 0, "w": PAGE["width"], "h": PAGE["height"]}
BACKGROUND = 230


def _dossier(*, act_id, act_key):
    return {
        "act_id": act_id,
        "act_key": act_key,
        "regions": [{"image_path": "transient-test-crop", "image_sha256": "unused"}],
        "page_renders": [
            {
                "source_page_id": PAGE_ID,
                "source_page_ordinal": PAGE["ordinal"],
                "image_path": "transient-test-page-render",
                "image_sha256": "unused",
            }
        ],
    }


def _delivered_pixels(rows):
    image = encode_grayscale_png(PAGE["width"], PAGE["height"], rows)
    return {"region_images": [image], "page_render_images": [image]}


def _blank_rows():
    return [bytearray([BACKGROUND]) * PAGE["width"] for _ in range(PAGE["height"])]


def test_fallback_text_is_derived_from_the_dossier_identity_not_its_key():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    fallback_id = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, BOUNDS)

    result = reader.read(
        _dossier(act_id=fallback_id, act_key="misleading-key"),
        pass_kind="perlectio",
        delivered_pixels=_delivered_pixels(_blank_rows()),
    )

    assert result["text"] == ""


def test_fallback_text_refuses_when_a_delivered_crop_contains_faint_ink():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    fallback_id = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, BOUNDS)
    rows = _blank_rows()
    # Nineteen levels below paper: invisible at PRIMARY_MARGIN=20, but ink at
    # the Designator conservation denominator's SECONDARY_MARGIN=2.
    for y in range(100, 124):
        for x in range(20, 180):
            rows[y][x] = BACKGROUND - 19

    with pytest.raises(ContractError, match="cannot invent a reading for ink"):
        reader.read(
            _dossier(act_id=fallback_id, act_key="page-fallback:3"),
            pass_kind="perlectio",
            delivered_pixels=_delivered_pixels(rows),
        )


def test_a_fallback_act_without_delivered_pixels_is_refused_not_blanked():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    fallback_id = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, BOUNDS)

    with pytest.raises(ContractError, match="without its pixels"):
        reader.read(
            _dossier(act_id=fallback_id, act_key="page-fallback:3"),
            pass_kind="perlectio",
        )


def test_a_fallback_act_missing_one_delivered_crop_is_refused():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    fallback_id = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, BOUNDS)
    delivered = _delivered_pixels(_blank_rows())
    delivered["region_images"] = []

    with pytest.raises(ContractError, match="every delivered page-fallback crop"):
        reader.read(
            _dossier(act_id=fallback_id, act_key="page-fallback:3"),
            pass_kind="perlectio",
            delivered_pixels=delivered,
        )


# --- Pass A's declared prior draft (R5a) --------------------------------------
#
# R5a's F-S2 repair removed a silent cross-scenario fallback that handed
# `happy`'s prior draft to every scenario that declared none. The repair is real
# in `reader.py`, but until these tests it was load-bearing on nothing: the same
# fallback could be reinstated and the whole Perlector suite stayed green,
# because the regenerated fixture now declares a row for every scenario that
# reaches Pass A. A repair no test can lose is a repair the next edit can undo.

_TWO_ACT_FIXTURE = {
    "act": [{"key": "a1", "text": "final one"}, {"key": "a2", "text": "final two"}],
    "page": [PAGE],
    "scenario": [{"name": "happy"}, {"name": "undeclared"}],
    "prior_reading": [
        {"scenario": "happy", "act_key": "a1", "text": "happy prior one"},
        {"scenario": "happy", "act_key": "a2", "text": "happy prior two"},
    ],
}


def _ordinary_dossier(act_key):
    """An act dossier with no page-render shape, so the fallback path is not taken."""
    return {"act_id": "act_0000000000000000", "act_key": act_key, "regions": [], "page_renders": []}


def test_an_unnamed_pass_kind_is_refused_rather_than_served_as_the_establishing_read():
    """The failure direction is what makes this a refusal and not a shrug.

    `_reading_text` dispatches Pass A on one string equality and falls through
    to the act's own final text otherwise, so a misspelt `"lectio_prior"` would
    have published Pass B's own reading as the Pass-A draft -- a `self_revision`
    of nothing at all, against a draft nobody wrote, with every downstream
    check satisfied. A closed vocabulary is the only place that is visible.
    """
    reader = FixtureReader(_TWO_ACT_FIXTURE, "happy")

    with pytest.raises(ContractError, match="unknown Perlector pass kind 'lectio_prior'"):
        reader.read(_ordinary_dossier("a1"), pass_kind="lectio_prior")


def test_every_pass_kind_the_producer_uses_is_in_the_closed_vocabulary():
    """`run.py` passes these four literals; the set is not a wider net than the
    producer needs, and no producer call site sits outside it."""
    producer_calls = set(
        re.findall(r"pass_kind=\"([^\"]+)\"", (Path(__file__).parent / "run.py").read_text())
    )
    assert producer_calls == set(reader_module.PASS_KINDS)


def test_pass_a_reads_this_scenarios_own_declared_prior_not_the_first_row():
    reader = FixtureReader(_TWO_ACT_FIXTURE, "happy")

    assert (
        reader.read(_ordinary_dossier("a2"), pass_kind="lectio-prior")["text"] == "happy prior two"
    )
    # The same act under the establishing pass still reads the act's own text:
    # only Pass A is served from the prior table.
    assert reader.read(_ordinary_dossier("a2"), pass_kind="perlectio")["text"] == "final two"


def test_a_scenario_with_no_declared_prior_refuses_instead_of_borrowing_anothers():
    """The exact defect F-S2 fixed: 15 scenarios silently measuring their
    self-revision against a draft written for `happy`. The fixture here names
    its declaring scenario `happy` on purpose, so the literal borrow that was
    removed -- not merely some generic first-match one -- goes red."""
    reader = FixtureReader(_TWO_ACT_FIXTURE, "undeclared")

    with pytest.raises(KeyError, match="declares no prior reading for 'undeclared'"):
        reader.read(_ordinary_dossier("a1"), pass_kind="lectio-prior")


def test_two_prior_rows_for_one_pair_are_refused_before_either_answers():
    fixture = {
        **_TWO_ACT_FIXTURE,
        "prior_reading": [
            *_TWO_ACT_FIXTURE["prior_reading"],
            {"scenario": "happy", "act_key": "a1", "text": "a contradicting second draft"},
        ],
    }

    with pytest.raises(KeyError, match="declares .* twice"):
        FixtureReader(fixture, "happy").read(_ordinary_dossier("a1"), pass_kind="lectio-prior")


def test_a_prior_row_naming_an_undeclared_scenario_is_refused_not_ignored():
    """A misspelt scenario is a row that names nothing; the act it meant to
    cover would fall through to the missing-prior refusal with no clue why."""
    fixture = {
        **_TWO_ACT_FIXTURE,
        "prior_reading": [
            *_TWO_ACT_FIXTURE["prior_reading"],
            {"scenario": "hapyp", "act_key": "a1", "text": "typo"},
        ],
    }

    with pytest.raises(KeyError, match="undeclared scenario 'hapyp'"):
        FixtureReader(fixture, "happy").read(_ordinary_dossier("a1"), pass_kind="lectio-prior")


def test_a_prior_row_naming_an_undeclared_act_is_refused_not_ignored():
    fixture = {
        **_TWO_ACT_FIXTURE,
        "prior_reading": [
            *_TWO_ACT_FIXTURE["prior_reading"],
            {"scenario": "happy", "act_key": "a9", "text": "no such act"},
        ],
    }

    with pytest.raises(KeyError, match="undeclared act 'a9'"):
        FixtureReader(fixture, "happy").read(_ordinary_dossier("a1"), pass_kind="lectio-prior")


def test_a_fallback_shaped_key_cannot_blank_a_non_fallback_act():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    non_fallback_id = derive_act_id(PAGE_ID, 0, BOUNDS)

    with pytest.raises(KeyError, match="declares no act"):
        reader.read(
            _dossier(act_id=non_fallback_id, act_key="page-fallback:3"), pass_kind="perlectio"
        )
