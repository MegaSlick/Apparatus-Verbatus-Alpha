"""The order the pipeline's stages first need each chair."""

from __future__ import annotations

from typing import Iterable

from common.chairs.models import is_witness_role

# Which stage first needs each chair, in pipeline order: the Designator, the
# Attestatores, the Perlector, then the Coniector's reconstructor. It follows
# `pod_run.RunPlan.required_chairs`; a chair no stage names comes last.
_DESIGNATOR_CHAIRS = ("secondary_proposer", "designator_surya")


def stage_need_rank(role: str) -> int:
    """The position of the first stage that needs `role`, for ordering fills and smokes."""

    if role in _DESIGNATOR_CHAIRS:
        return 0
    if is_witness_role(role):
        return 1
    if role == "perlector":
        return 2
    if role == "reconstructor":
        return 3
    return 4


def in_stage_need_order(roles: Iterable[str]) -> list[str]:
    """`roles` ordered by the stage that first needs each, then by name."""

    return sorted(roles, key=lambda role: (stage_need_rank(role), role))
