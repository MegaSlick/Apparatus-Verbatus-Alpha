"""Fixture-schema identity helpers.

These helpers deliberately live outside ``common.contracts``: fixture rows are
test data, while the contracts package only defines durable evidence records.
"""

from typing import Any

from common.contracts.errors import ContractError
from common.contracts.identities import act_id, page_id


def page_for(fixture: dict[str, Any], ordinal: int) -> dict[str, Any]:
    for page in fixture["page"]:
        if page["ordinal"] == ordinal:
            return page
    raise ContractError(f"the fixture declares no page {ordinal}")


def page_identity(fixture: dict[str, Any], ordinal: int) -> str:
    page = page_for(fixture, ordinal)
    return page_id({"kind": "source", "sha256": page["sha256"]}, {"operation": "whole"})


def act_bounds(act: dict[str, Any]) -> dict[str, int]:
    return {"x": act["x"], "y": act["y"], "w": act["w"], "h": act["h"]}


def act_identity(fixture: dict[str, Any], act: dict[str, Any]) -> str:
    return act_id(page_identity(fixture, act["page_ordinal"]), "proposal", act_bounds(act))
