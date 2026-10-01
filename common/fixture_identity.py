"""Fixture-schema identity helpers.

These helpers deliberately live outside ``common.contracts``: fixture rows are
test data, while the contracts package only defines durable evidence records.
"""

from typing import Any

from common.contracts.errors import ContractError
from common.contracts.identities import page_id


def page_identity(fixture: dict[str, Any], ordinal: int) -> str:
    for page in fixture["page"]:
        if page["ordinal"] == ordinal:
            return page_id({"kind": "source", "sha256": page["sha256"]}, {"operation": "whole"})
    raise ContractError(f"the fixture declares no page {ordinal}")
