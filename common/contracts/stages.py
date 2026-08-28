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

ORCHESTRATOR: Final = "orchestrator"

# Completion seals are witnessed statements at the end of each producer pass.
# Keys are consumers, including the orchestrator as Armarium's final consumer.
#
# Derived from HANDOFFS rather than restated. A seal is the witness to exactly the
# boundary above it, so a hand-kept second copy of the same seven pairs could drift
# from the list this module exists to be the single source of — and a seal bound to
# a boundary nobody hands off across is a statement about nothing. The orchestrator
# is the one entry HANDOFFS cannot carry: it consumes the Armarium's pass without
# being a stage that writes.
SEAL_PREDECESSORS: Final = {
    **{consumer: producer for producer, consumer in HANDOFFS},
    ORCHESTRATOR: ARMARIUM,
}


# Where each producer *writes*, which is not the same question as which directory
# a stage owns. The door owns nothing and writes into the Exemplar's directory, so
# its refusals sit inside the record of what arrived rather than in a drawer no
# downstream stage reads.
WRITING_DIRECTORIES: Final = {**STAGE_DIRECTORIES, DOOR: STAGE_DIRECTORIES[EXEMPLAR]}


def writing_directory(stage: str) -> str:
    """The run-tree directory a producer writes into."""
    try:
        return WRITING_DIRECTORIES[stage]
    except KeyError:
        raise KeyError(
            f"{stage!r} writes nowhere in a run tree; "
            f"known producers: {sorted(WRITING_DIRECTORIES)}"
        ) from None


def stage_directory(stage: str) -> str:
    """The run-tree directory a stage owns."""
    try:
        return STAGE_DIRECTORIES[stage]
    except KeyError:
        raise KeyError(
            f"{stage!r} owns no run-tree directory; known stages: {sorted(STAGE_DIRECTORIES)}"
        ) from None


# The config sections, manifest schema, and every future driver must import this
# vocabulary rather than maintain independent spellings that can drift.
TRIAGE_MODES: Final = ("manual", "semi", "auto")
