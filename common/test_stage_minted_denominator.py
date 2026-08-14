"""Minted Designator rows remain verified when the fixture declares no acts."""

from types import SimpleNamespace

import pytest

from common.contracts.errors import FatalAccounting
from common.stage import _verify_synthetic_act_denominator


class _EmptyDesignatorTree:
    def build_manifest(self, _stage):
        return {"artifacts": []}


def test_an_empty_fixture_does_not_bypass_minted_row_verification():
    context = SimpleNamespace(fixture={"act": []}, tree=_EmptyDesignatorTree())
    uncorroborated_row = {
        "act_id": "act_0000000000000000",
        "act_key": "page-fallback:1",
        "page_id": "pg_0000000000000000",
        "page_ordinal": 1,
        "has_continuation": False,
        "outcome": "proposed",
        "evidence": [],
    }

    with pytest.raises(FatalAccounting, match="published no page-fallback record"):
        _verify_synthetic_act_denominator(context, [uncorroborated_row])
