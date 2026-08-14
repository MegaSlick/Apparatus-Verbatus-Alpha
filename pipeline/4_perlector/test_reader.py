"""The fixture reader recognizes minted fallback acts by derived identity."""

import pytest
from reader import FixtureReader

from common.contracts.identities import act_id as derive_act_id
from common.stage import FALLBACK_PAGE_ACT_ORDINAL

PAGE = {"ordinal": 3, "width": 200, "height": 260}
PAGE_ID = "pg_0000000000000000"
BOUNDS = {"x": 0, "y": 0, "w": PAGE["width"], "h": PAGE["height"]}


def _dossier(*, act_id, act_key):
    return {
        "act_id": act_id,
        "act_key": act_key,
        "page_renders": [{"source_page_id": PAGE_ID, "source_page_ordinal": PAGE["ordinal"]}],
    }


def test_fallback_text_is_derived_from_the_dossier_identity_not_its_key():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    fallback_id = derive_act_id(PAGE_ID, FALLBACK_PAGE_ACT_ORDINAL, BOUNDS)

    result = reader.read(_dossier(act_id=fallback_id, act_key="misleading-key"), primed=True)

    assert result["text"] == ""


def test_a_fallback_shaped_key_cannot_blank_a_non_fallback_act():
    reader = FixtureReader({"act": [], "page": [PAGE], "scenario": []}, "scenario")
    non_fallback_id = derive_act_id(PAGE_ID, 0, BOUNDS)

    with pytest.raises(KeyError, match="declares no act"):
        reader.read(_dossier(act_id=non_fallback_id, act_key="page-fallback:3"), primed=True)
