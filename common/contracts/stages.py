"""The stage names, in flow order, and the eight handoffs between them.

Named once here so the run tree, the outcome algebra, and the boundary tests
cannot drift apart on what a stage is called. The directory names match
`pipeline/`'s numbering, which is what makes a run tree listing read top to bottom
like the diagram in ARCHITECTURE.md.

The door is a producer but not a numbered stage: it is the submit surface that
decides what may enter at all, and it writes its refusals where the Exemplar can
account for them. Giving it a name here is what lets "door -> Exemplar" be one of
the eight tested handoffs rather than an unexamined edge.
"""

from enum import Enum
from typing import Final

DOOR: Final = "door"
EXEMPLAR: Final = "exemplar"
INK_MAP: Final = "ink-map"
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
    INK_MAP,
    DESIGNATOR,
    ATTESTATORES,
    PERLECTOR,
    RECENSOR,
    ARCHETYPUS,
    ARMARIUM,
)

# The door has no directory: its refusals belong to the record of what arrived.
STAGE_DIRECTORIES: Final = {
    EXEMPLAR: "1_exemplar",
    INK_MAP: "1_ink_map",
    DESIGNATOR: "2_designator",
    ATTESTATORES: "3_attestatores",
    PERLECTOR: "4_perlector",
    RECENSOR: "5_recensor",
    ARCHETYPUS: "6_archetypus",
    ARMARIUM: "7_armarium",
}

# Every boundary is driven by the table-based corruption tests: its consumer must
# refuse a malformed schema or identity, a mismatched digest, and duplicate accounting.
HANDOFFS: Final = (
    (DOOR, EXEMPLAR),
    (EXEMPLAR, INK_MAP),
    (INK_MAP, DESIGNATOR),
    (DESIGNATOR, ATTESTATORES),
    (ATTESTATORES, PERLECTOR),
    (PERLECTOR, RECENSOR),
    (RECENSOR, ARCHETYPUS),
    (ARCHETYPUS, ARMARIUM),
)

ORCHESTRATOR: Final = "orchestrator"

# Consumer -> the producer whose completion seal it reads, derived from HANDOFFS
# so the two cannot drift; the orchestrator consumes the Armarium without writing.
SEAL_PREDECESSORS: Final = {
    **{consumer: producer for producer, consumer in HANDOFFS},
    ORCHESTRATOR: ARMARIUM,
}


# Where each producer writes; the door writes into the Exemplar's directory.
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

# Bounds quadratic pairwise overlap checks on untrusted input; one spelling for
# both validators, or the looser would decide.
MAX_TRIAGE_SPLIT_PARTS: Final = 64

# The triage decision-manifest row, closed; the pre-door manifest and the
# Exemplar boundary both hold rows to these sets. A deterministic offline
# producer is its own actor kind because it makes no model call.
TRIAGE_ACTOR_KINDS: Final = ("human", "model", "scantailor", "producer")
TRIAGE_ACTOR_FIELDS: Final = frozenset({"kind", "identity", "revision"})
TRIAGE_PART_FIELDS: Final = frozenset({"region", "crop_box", "rotation", "colour_mode"})
TRIAGE_ROW_FIELDS: Final = frozenset(
    {
        "corpus_id",
        "source_frame_sha256",
        "frame",
        "split",
        "re_shoot_cluster_id",
        "confidence",
        "mode",
        "actor",
        "human_override",
        "manifest_row_sha256",
    }
)


class RefusalReason(str, Enum):
    """The door's closed alarm vocabulary for damage and decoder failures.

    A format-policy refusal deliberately does not exist.  `UNSUPPORTED_VARIANT`
    names a real decoder gap so it is visible work for the pipeline, rather than a
    routine reason to abandon a submitted page.
    """

    EMPTY = "empty"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too-large"
    UNRECOGNIZED_FORMAT = "unrecognized-format"
    CORRUPT = "corrupt"
    UNSUPPORTED_VARIANT = "unsupported-variant"
    DIGEST_MISMATCH = "digest-mismatch"
