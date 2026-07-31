"""The stage names, in flow order, and the seven handoffs between them.

Named once here so the run tree, the outcome algebra, and the boundary tests
cannot drift apart on what a stage is called. The directory names match
`pipeline/`'s numbering, which is what makes a run tree listing read top to bottom
like the diagram in ARCHITECTURE.md.

The door is a producer but not a numbered stage: it is the submit surface that
decides what may enter at all, and it writes its refusals where the Exemplar can
account for them. Giving it a name here is what lets "door -> Exemplar" be one of
the seven tested handoffs rather than an unexamined edge.
"""

from typing import Final

DOOR: Final = "door"
EXEMPLAR: Final = "exemplar"
DESIGNATOR: Final = "designator"
ATTESTATORES: Final = "attestatores"
PERLECTOR: Final = "perlector"
RECENSOR: Final = "recensor"
ARCHETYPUS: Final = "archetypus"
ARMARIUM: Final = "armarium"

# Flow order. The door leads because a refusal there is still a unit that has to
# be accounted for downstream.
STAGES: Final = (
    DOOR,
    EXEMPLAR,
    DESIGNATOR,
    ATTESTATORES,
    PERLECTOR,
    RECENSOR,
    ARCHETYPUS,
    ARMARIUM,
)

# Where each stage writes inside a run tree. The door has no directory of its own —
# it writes into the Exemplar's, because its refusals are part of the record of
# what arrived, and a stage directory nothing downstream reads would be a place for
# evidence to go quiet.
STAGE_DIRECTORIES: Final = {
    EXEMPLAR: "1_exemplar",
    DESIGNATOR: "2_designator",
    ATTESTATORES: "3_attestatores",
    PERLECTOR: "4_perlector",
    RECENSOR: "5_recensor",
    ARCHETYPUS: "6_archetypus",
    ARMARIUM: "7_armarium",
}

# The seven boundaries spec 01 names, in order. Every one of these is driven by the
# table-driven boundary test: the consumer must refuse a corrupted schema, a
# malformed identity, a mismatched input digest, and a duplicate accounting entry.
HANDOFFS: Final = (
    (DOOR, EXEMPLAR),
    (EXEMPLAR, DESIGNATOR),
    (DESIGNATOR, ATTESTATORES),
    (ATTESTATORES, PERLECTOR),
    (PERLECTOR, RECENSOR),
    (RECENSOR, ARCHETYPUS),
    (ARCHETYPUS, ARMARIUM),
)


def stage_directory(stage: str) -> str:
    """The run-tree directory a stage owns."""
    try:
        return STAGE_DIRECTORIES[stage]
    except KeyError:
        raise KeyError(
            f"{stage!r} owns no run-tree directory; known stages: {sorted(STAGE_DIRECTORIES)}"
        ) from None
