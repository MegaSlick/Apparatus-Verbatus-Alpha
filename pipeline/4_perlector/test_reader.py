"""The fixture reader recognizes and actually inspects minted fallback acts."""

import pytest
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


def test_a_fallback_shaped_key_cannot_blank_a_non_fallback_act():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    non_fallback_id = derive_act_id(PAGE_ID, 0, BOUNDS)

    with pytest.raises(KeyError, match="declares no act"):
        reader.read(
            _dossier(act_id=non_fallback_id, act_key="page-fallback:3"), pass_kind="perlectio"
        )
