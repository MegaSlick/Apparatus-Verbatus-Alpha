"""The outcome algebra: two layers, and a total mapping between them.

Harvest invariant #10 is the spine — *total partition, proven, every stage*: every
unit entering a stage is accounted for at the boundary as exactly one of completed
/ unresolved-with-evidence / failed, and a unit in none of those sets is a FATAL
accounting imbalance, never a warning. That is the CLASS layer, and it is the same
three words at every boundary in the pipeline.

Each stage also has its own closed OUTCOME vocabulary, because "failed" is not
informative enough to act on: the Recensor needs to tell a held act from a blank
one, and a witness chair that died from one that was never asked. Every outcome maps
to exactly one class, and every outcome maps either to a terminal Armarium category
or explicitly to None meaning "flows onward". Both mappings are total, and
`check_algebra_is_total` proves it rather than trusting it — an outcome added later
without a class or without a terminal decision fails that check loudly, which is
what "no stage invents a state" has to mean if it is to mean anything.

**Why the witness column is all None.** Witness outcomes are the one vocabulary in
this file that terminates nothing. They aggregate into a coverage record and never
into a category or a character of text. An act every one of whose chairs is `failed`
or `dead` still reaches the Perlector, which reads the ink; it may be delivered,
carrying `under_witnessed`. Any rule that let chair outcomes promote or demote an
act's text would be a picker wearing an accounting name, and principle 1 forbids
it under every name.
"""

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Final

from .canonical import is_plain_int
from .errors import ApprovalRefusal, FatalAccounting, SchemaRefusal
from .stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
)


class OutcomeClass(str, Enum):
    """Invariant #10's three sets. Every unit is in exactly one."""

    COMPLETED = "completed"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class ArmariumCategory(str, Enum):
    """The five terminal categories an act can end in, and nothing else."""

    DELIVERED = "delivered"
    HELD_FOR_REVIEW = "held-for-review"
    EXCLUDED_WITH_APPROVAL = "excluded-with-approval"
    CONFIRMED_BLANK = "confirmed-blank"
    REFUSED_WITH_REASON = "refused-with-reason"


_C = OutcomeClass
_A = ArmariumCategory
# The witness outcomes that ARE a reading: narrower than the ATTESTATORES
# COMPLETED class, which also holds an approval-bound `excluded`.
WITNESS_READING_OUTCOMES: Final = frozenset({"read", "genuinely-empty"})
INTERIM_GRANULARITY_BASIS: Final = "computed-act-attachment-alignment"
# Act-granularity facts that each name their own attachment basis, as against
# the weaker interim derivation.
NATIVE_GRANULARITY_BASIS: Final = "native-per-chair-attachment-basis"
LEGACY_GRANULARITY_BASIS: Final = "legacy-class-only"
# Which evidence decided one attachment; the floor arithmetic below reads it.
ATTACHMENT_BASES: Final = frozenset(
    {"presented-region", "anchor-line", "geometric-overlap", "unattached"}
)
# The shortest contiguous run of the act's anchor line a witness must match for
# the alignment to have LOCATED that line.  Measured against `align_to_anchor` on
# a 145-character register line (reproduced by `test_contracts_algebra.py::
# test_the_anchor_line_run_floor_sits_between_coincidence_and_a_real_reading`):
# unrelated prose reaches 1, random text over the anchor's alphabet 3 to 5,
# another act in the same formula 7, and a genuine reading 145, 25 and 14 at 0%,
# 10% and 20% character error (8 to 10 at 30% to 50%, one sample each).  Total
# matched coverage does not separate these; the longest run does.  It cannot
# tell a misread line from another act's line in the same formula, and nothing
# character-level can; it refuses coincidence.
ANCHOR_LINE_RUN_FLOOR: Final = 8


def anchor_line_located(alignment: Any) -> bool:
    """Whether a page alignment placed THIS act's own anchor line in the witness text.

    Not "the alignment succeeded". Four separate things have to hold, and each
    of them is a different way the same record can be honest and still place
    nothing here:

    * `status == "aligned"` -- an unaligned record carries a reason and no span.
      A continuation page is forced to `continuation-page-no-act-anchor` before
      geometry is ever consulted (`pipeline/3_attestatores/run.py`), so this
      basis can never arise on a page the act is not primary on, which is
      correct: the anchor is derived from the act's own primary page.
    * `anchor_basis == "act-anchor"` -- `no-page-anchor` and
      `act-line-not-located` are aligned records that say, in the producer's own
      vocabulary, that no line for this act was located. They exist for the
      trivial attach a genuinely empty page reading gets.
    * a positive-length `witness_span` -- the same trivial attach carries
      `{"start": 0, "end": 0}`. A zero-length slice is not text this act was
      placed in, and counting it would put a chair on the witness floor for a
      reading that placed nothing (principle 8).
    * an `anchor_line_match` whose longest contiguous run reaches
      `ANCHOR_LINE_RUN_FLOOR` (or the whole anchor line, where the line is
      shorter than the floor).

    The fourth gives the third its meaning: `align_to_anchor` keeps matching
    blocks of size one, so any two coinciding characters make a positive span.

    Defensive about shape rather than validating it: this is read from
    untrusted retained evidence at three seams, and each of those seams
    validates the alignment's full closed shape itself. What this must never do
    is raise a bare `TypeError`/`KeyError` out of a derivation whose answer is
    then compared against a producer's boolean.
    """
    if not isinstance(alignment, Mapping) or alignment.get("status") != "aligned":
        return False
    if alignment.get("anchor_basis") != "act-anchor":
        return False
    span = alignment.get("witness_span")
    if not isinstance(span, Mapping):
        return False
    start, end = span.get("start"), span.get("end")
    if not all(is_plain_int(bound) for bound in (start, end)):
        return False
    if end <= start:
        return False
    match = alignment.get("anchor_line_match")
    if not isinstance(match, Mapping):
        return False
    anchor_characters = match.get("anchor_characters")
    matched = match.get("matched_characters")
    longest = match.get("longest_matched_run")
    if not all(is_plain_int(value) for value in (anchor_characters, matched, longest)):
        return False
    # An incoherent measurement refuses rather than clamps.
    if not 0 <= longest <= matched <= anchor_characters or anchor_characters <= 0:
        return False
    return longest >= min(ANCHOR_LINE_RUN_FLOOR, anchor_characters)


def page_attachment_basis(*, reading: bool, geometry_overlaps: bool, alignment: Any) -> str:
    """Which evidence attaches one page witness's reading to one act.

    The one derivation, called by the producer (`pipeline/3_attestatores/run.py`)
    and re-derived by both readers (the Perlector's `act_attachment_view` and the
    Recensor's `act_attachment_facts`), so one rule cannot drift into three.

    Geometry first: a chair that reported ink over the act's proposal attached on
    its own evidence, and `anchor-line` would understate that.

    The anchor line exists for page witnesses whose grammar carries no geometry
    (Churro's `HistoricalDocument`), which could otherwise never attach.  Not a
    picker (principle 1): the anchor, from another chair's response, decides only
    whether this chair's text was placed in this act, never whose reading is
    right.  It does cost independence, and the live seam says so.  It also costs
    forgery resistance: the readers take the recorded alignment as evidence, so a
    forged attachment needs only a forged alignment, still behind the
    Attestatores seal (`pipeline/4_perlector/test_comparability_seam.py`).  The
    fix is a reader that re-derives the alignment, which needs text neither
    reader holds today.
    """
    if not reading:
        return "unattached"
    if geometry_overlaps:
        return "geometric-overlap"
    if anchor_line_located(alignment):
        return "anchor-line"
    return "unattached"


# --- The vocabularies: outcome -> class, one closed set per stage ---------------

VOCABULARIES: Final[dict[str, dict[str, OutcomeClass]]] = {
    DOOR: {
        "admitted": _C.COMPLETED,
        "refused": _C.FAILED,
    },
    EXEMPLAR: {
        "sealed": _C.COMPLETED,
        "refused": _C.FAILED,
    },
    # Page evidence, not an act decision; Unit 14 owns the hold that makes an
    # unclaimed edge terminal.
    INK_MAP: {
        "mapped": _C.COMPLETED,
        "unclaimed-edge-ink": _C.UNRESOLVED,
        # A missing instrument, disclosed onward; not a failed act or a blank page.
        "ink-not-measurable": _C.UNRESOLVED,
    },
    DESIGNATOR: {
        "proposed": _C.COMPLETED,
        # Completed only because an approval record says so; `require_approval`
        # below is what stops the word from being enough on its own.
        "excluded": _C.COMPLETED,
        "held": _C.UNRESOLVED,
        "failed": _C.FAILED,
    },
    ATTESTATORES: {
        "read": _C.COMPLETED,
        # A reading that found nothing, not an absence.
        "genuinely-empty": _C.COMPLETED,
        # An attempt that produced no usable Testimonium.
        "failed": _C.FAILED,
        # Configured but unavailable; no attempt reached the region.
        "dead": _C.FAILED,
        # Configured, never attempted, nothing went wrong yet.
        "not-run": _C.UNRESOLVED,
        "excluded": _C.COMPLETED,
    },
    PERLECTOR: {
        "read": _C.COMPLETED,
        # Silence does not prove a blank, so it stays unresolved.
        "no-readable-text": _C.UNRESOLVED,
        "truncated": _C.FAILED,
        "failed": _C.FAILED,
        "not-run": _C.UNRESOLVED,
    },
    RECENSOR: {
        "accepted": _C.COMPLETED,
        "recovery-requested": _C.UNRESOLVED,
        "confirmed-blank": _C.COMPLETED,
        "held-for-review": _C.UNRESOLVED,
        "failed": _C.FAILED,
    },
    ARCHETYPUS: {
        "established": _C.COMPLETED,
        "refused": _C.FAILED,
    },
    ARMARIUM: {
        category.value: klass
        for category, klass in (
            (_A.DELIVERED, _C.COMPLETED),
            (_A.HELD_FOR_REVIEW, _C.UNRESOLVED),
            (_A.EXCLUDED_WITH_APPROVAL, _C.COMPLETED),
            (_A.CONFIRMED_BLANK, _C.COMPLETED),
            (_A.REFUSED_WITH_REASON, _C.FAILED),
        )
    },
}

BOUNDARY_OUTCOMES: Final = {
    "stage-seal": "sealed",
    "decode-environment": "recorded",
    # Accounting boundaries, not extra witness outcomes: the Testimonium stays the
    # one configured-chair denominator.
    "chandra-native-attempt-intent": "recorded",
    "chandra-native-attempt": "recorded",
}

# Boundary evidence, never an act's category (an Armarium boundary record is not
# `delivered`).  A hand-declared entry that disagrees stops the import rather than
# being overwritten in silence.
for _stage in VOCABULARIES:
    for _outcome in BOUNDARY_OUTCOMES.values():
        _declared = VOCABULARIES[_stage].get(_outcome)
        if _declared is not None and _declared is not _C.COMPLETED:
            raise FatalAccounting(
                f"stage {_stage!r} declares boundary outcome {_outcome!r} as "
                f"{_declared.value!r}, which the boundary vocabulary would overwrite"
            )
        VOCABULARIES[_stage][_outcome] = _C.COMPLETED

# --- The transition table: (stage, outcome) -> terminal category, or None -------
#
# None means "this unit flows onward and some later stage decides its category".
# A missing entry means nobody decided, which `check_algebra_is_total` treats as
# the defect it is.

TERMINAL_CATEGORY: Final[dict[tuple[str, str], ArmariumCategory | None]] = {
    (DOOR, "admitted"): None,
    (DOOR, "refused"): _A.REFUSED_WITH_REASON,
    (EXEMPLAR, "sealed"): None,
    (EXEMPLAR, "refused"): _A.REFUSED_WITH_REASON,
    (INK_MAP, "mapped"): None,
    (INK_MAP, "unclaimed-edge-ink"): None,
    (INK_MAP, "ink-not-measurable"): None,
    (DESIGNATOR, "proposed"): None,
    (DESIGNATOR, "excluded"): _A.EXCLUDED_WITH_APPROVAL,
    (DESIGNATOR, "held"): _A.HELD_FOR_REVIEW,
    (DESIGNATOR, "failed"): _A.REFUSED_WITH_REASON,
    # Every witness outcome is transitive. See the module docstring: this column
    # is where a picker would be born if any entry here were a category.
    (ATTESTATORES, "read"): None,
    (ATTESTATORES, "genuinely-empty"): None,
    (ATTESTATORES, "failed"): None,
    (ATTESTATORES, "dead"): None,
    (ATTESTATORES, "not-run"): None,
    (ATTESTATORES, "excluded"): None,
    (PERLECTOR, "read"): None,
    (PERLECTOR, "no-readable-text"): None,
    (PERLECTOR, "truncated"): None,
    (PERLECTOR, "failed"): None,
    (PERLECTOR, "not-run"): None,
    (RECENSOR, "accepted"): None,
    (RECENSOR, "recovery-requested"): None,
    (RECENSOR, "confirmed-blank"): _A.CONFIRMED_BLANK,
    (RECENSOR, "held-for-review"): _A.HELD_FOR_REVIEW,
    (RECENSOR, "failed"): _A.REFUSED_WITH_REASON,
    (ARCHETYPUS, "established"): _A.DELIVERED,
    (ARCHETYPUS, "refused"): _A.REFUSED_WITH_REASON,
    (ARMARIUM, _A.DELIVERED.value): _A.DELIVERED,
    (ARMARIUM, _A.HELD_FOR_REVIEW.value): _A.HELD_FOR_REVIEW,
    (ARMARIUM, _A.EXCLUDED_WITH_APPROVAL.value): _A.EXCLUDED_WITH_APPROVAL,
    (ARMARIUM, _A.CONFIRMED_BLANK.value): _A.CONFIRMED_BLANK,
    (ARMARIUM, _A.REFUSED_WITH_REASON.value): _A.REFUSED_WITH_REASON,
}

for _stage in VOCABULARIES:
    for _outcome in BOUNDARY_OUTCOMES.values():
        if TERMINAL_CATEGORY.get((_stage, _outcome), None) is not None:
            raise FatalAccounting(
                f"stage {_stage!r} declares boundary outcome {_outcome!r} as terminal, "
                "which the boundary vocabulary would overwrite"
            )
        TERMINAL_CATEGORY[(_stage, _outcome)] = None

# The Perlector's failures are transitive on purpose: the Recensor may request
# bounded recovery, and only its outcome terminates the act.


def classify(stage: str, outcome: Any) -> OutcomeClass:
    """The class of one stage outcome. An unknown outcome is fatal, not a warning."""
    vocabulary = VOCABULARIES.get(stage)
    if vocabulary is None:
        raise FatalAccounting(f"stage {stage!r} has no outcome vocabulary")
    try:
        return vocabulary[outcome]
    except (KeyError, TypeError):
        raise FatalAccounting(
            f"{stage} produced outcome {outcome!r}, which is in no terminal set; "
            f"its closed vocabulary is {sorted(vocabulary)}. Invariant #10: a unit "
            "in no set is a fatal accounting imbalance, never a warning"
        ) from None


def terminal_category(stage: str, outcome: Any) -> ArmariumCategory | None:
    """The category this outcome ends in, or None when it flows onward."""
    classify(stage, outcome)
    try:
        return TERMINAL_CATEGORY[(stage, outcome)]
    except KeyError:
        raise FatalAccounting(
            f"({stage}, {outcome!r}) has a class but no entry in the transition "
            "table: nobody decided whether it terminates the act or flows onward"
        ) from None


def require_approval(stage: str, outcome: Any, approval_ref: Any) -> None:
    """Refuse an approval-bound outcome that carries no approval-record reference.

    Only two outcomes in the whole algebra are approval-bound, and both mean a unit
    left the pipeline as `completed` without anyone reading its text. A claimed
    approval with no artifact is no approval.
    """
    # The stage word and the Armarium category that names approval.
    if outcome not in ("excluded", ArmariumCategory.EXCLUDED_WITH_APPROVAL.value):
        return
    if not isinstance(approval_ref, str) or not approval_ref:
        raise ApprovalRefusal(
            f"{stage} outcome {outcome!r} carries no approval-record reference; "
            "only the project lead approves an exclusion, and the artifact is the approval"
        )


def check_algebra_is_total() -> None:
    """Prove both mappings total, and prove the two layers agree.

    Called by the contract tests and by the orchestrator at startup, so a stage
    added later without a class or a terminal decision fails at the first run
    rather than at the first unusual page.
    """
    for stage, vocabulary in VOCABULARIES.items():
        if not vocabulary:
            raise FatalAccounting(f"stage {stage!r} has an empty vocabulary")
        for outcome, klass in vocabulary.items():
            if not isinstance(klass, OutcomeClass):
                raise FatalAccounting(f"({stage}, {outcome!r}) has no class")
            if (stage, outcome) not in TERMINAL_CATEGORY:
                raise FatalAccounting(
                    f"({stage}, {outcome!r}) is missing from the transition table"
                )
            category = TERMINAL_CATEGORY[(stage, outcome)]
            if category is not None and VOCABULARIES[ARMARIUM][category.value] is not klass:
                raise FatalAccounting(
                    f"({stage}, {outcome!r}) is class {klass.value} but terminates "
                    f"in {category.value}, which is class "
                    f"{VOCABULARIES[ARMARIUM][category.value].value}: a unit would "
                    "change class by crossing a boundary"
                )
    for stage, outcome in TERMINAL_CATEGORY:
        if outcome not in VOCABULARIES.get(stage, {}):
            raise FatalAccounting(
                f"transition table has ({stage}, {outcome!r}), which is in no "
                "stage vocabulary — a state invented by the table itself"
            )


# --- The established text's own status: the three silences, kept apart ---------
#
# A closed vocabulary about *what one record's `text` contains*, beside — never
# inside — the category vocabulary above, which is about *where the act ended*.
# An act can be `delivered` and `partial` at once, and the whole point of keeping
# the two words apart is that it can — many records are damaged.
#
# Here because two stages read it: the Archetypus derives it, and the Armarium
# recomputes it rather than believing the field.


TEXT_STATUSES: Final = frozenset({"established", "partial", "no_readable_text"})


def derive_text_status(text: Any, annotations: Any) -> str:
    """established | partial | no_readable_text, from the text and its gaps alone.

    A gap anywhere means some ink is known and unread, whether `text` is otherwise
    empty or full: `partial`. No gap and no text is the only remaining case, and
    the only one that may be called `no_readable_text` — a positive finding that
    owes its own evidence (`pipeline/6_archetypus/run.py::validate_text_status`).

    "We could not read it" must never quietly become "there was nothing to read".
    """
    if not isinstance(text, str):
        raise SchemaRefusal("a text status requires exactly one string text field")
    if not isinstance(annotations, (list, tuple)):
        raise SchemaRefusal("a text status cannot be derived from a non-list annotation layer")
    for index, note in enumerate(annotations):
        if not isinstance(note, Mapping) or "kind" not in note:
            raise SchemaRefusal(
                f"annotation {index} carries no kind, so whether it records unread ink "
                "cannot be decided; an unreadable damage layer is refused, never skipped"
            )
    if any(note["kind"] == "illegible" for note in annotations):
        return "partial"
    if text.strip() == "":
        return "no_readable_text"
    return "established"


def derive_record_text_status(text: Any, annotations: Any, uncertainty: Any) -> str:
    """The same three words over *both* damage layers a sealed record carries.

    A record carries the canonical `uncertainty` layer (`gaps`, the shape every
    Perlectio actually produces) and the older `annotations` layer (`illegible`
    notes, which nothing upstream populates yet). Either one recording unread ink
    makes the record `partial`, and neither can hide damage the other saw, so the
    two travelling together is honest even where they are not identical: this
    union is the one status both of them answer to.

    Gaps are read before the empty-text case: a gap over empty text is ink
    present and unread, `partial`, never `no_readable_text`.
    """
    if not isinstance(uncertainty, Mapping) or not isinstance(
        uncertainty.get("gaps"), (list, tuple)
    ):
        raise SchemaRefusal(
            "a record text status requires the canonical uncertainty layer's own gap list"
        )
    if uncertainty["gaps"]:
        # Still type-checks the text, so a malformed record cannot slip through here.
        derive_text_status(text, annotations)
        return "partial"
    return derive_text_status(text, annotations)


# --- Witness coverage: outcomes aggregate into counts, never into text ----------


def witness_coverage(
    chair_outcomes: Mapping[str, str],
    configured_floor: int,
    *,
    attachments: Mapping[str, Mapping[str, Any] | bool] | None = None,
) -> dict[str, Any]:
    """Aggregate one act's chair outcomes into the coverage record.

    Returns counts and two flags, and deliberately returns no category and no
    text. The caller may record this beside an act; nothing may branch the act's
    reading on it.

    `under_witnessed` is chairs reaching a completed-class outcome below the
    configured floor. Spec 07: three chairs is the floor, the machinery tolerates
    fewer so one dead witness never kills a run, and a run below the floor is
    recorded as under-witnessed in the Recensor receipt and the export manifest,
    visibly, every time.
    """
    if configured_floor < 0:
        raise FatalAccounting(f"configured witness floor {configured_floor} is negative")
    by_outcome: dict[str, int] = {}
    by_class: dict[str, int] = {klass.value: 0 for klass in OutcomeClass}
    for chair, outcome in chair_outcomes.items():
        if not chair:
            raise FatalAccounting("a chair outcome was recorded against an unnamed chair")
        klass = classify(ATTESTATORES, outcome)
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        by_class[klass.value] += 1
    attached_chairs: set[str] = set()
    health_unrecorded = 0
    shortfalls = {"failed": 0, "truncated": 0, "unaligned": 0}
    # Whether act-granularity facts were supplied decides the arithmetic; the
    # native basis is claimed only when every fact names the basis that decided it.
    native_evidence = attachments is not None
    if attachments is not None:
        unknown = set(attachments) - set(chair_outcomes)
        if unknown:
            raise FatalAccounting(
                f"act attachment facts name unconfigured chair(s) {sorted(unknown)}"
            )
        for chair, outcome in chair_outcomes.items():
            fact = attachments.get(chair)
            if fact is None:
                fact = False
            if isinstance(fact, bool):
                # The shorthand measured no comparison, so it earns none.
                fact = {"attached": fact, "comparable": False}
            if (
                not isinstance(fact, Mapping)
                or not isinstance(fact.get("attached"), bool)
                or not isinstance(fact.get("comparable"), bool)
            ):
                raise FatalAccounting(
                    f"act attachment fact for {chair!r} has no boolean attached/comparable pair. "
                    "The act-level witness floor cannot be derived from an ambiguous attachment. "
                    "Rebuild the attachment from the retained Testimonia before retrying."
                )
            if fact.get("attachment_basis") not in ATTACHMENT_BASES:
                native_evidence = False
            if fact.get("health_unrecorded") is True:
                health_unrecorded += 1
            truncated = fact.get("truncated")
            if truncated is True:
                shortfalls["truncated"] += 1
            elif truncated not in (False, None):
                raise FatalAccounting(
                    f"act attachment fact for {chair!r} has invalid truncated state"
                )
            if outcome == "failed":
                shortfalls["failed"] += 1
            if not fact["attached"] or not fact["comparable"]:
                shortfalls["unaligned"] += 1
            elif outcome in WITNESS_READING_OUTCOMES and truncated is not True:
                attached_chairs.add(chair)
    else:
        # Callers without attachment facts keep the class-level arithmetic.
        attached_chairs = {
            chair
            for chair, outcome in chair_outcomes.items()
            if classify(ATTESTATORES, outcome) is OutcomeClass.COMPLETED
        }

    completed = len(attached_chairs)
    return {
        "configured": len(chair_outcomes),
        "floor": configured_floor,
        "by_outcome": by_outcome,
        "by_class": by_class,
        "under_witnessed": completed < configured_floor,
        # An unanswered chair cannot sit inside a complete run.
        "unresolved_chairs": by_class[OutcomeClass.UNRESOLVED.value],
        # Coverage facts, kept apart from the closed witness outcome vocabulary.
        "page_granularity_only": sum(
            1
            for chair, outcome in chair_outcomes.items()
            if outcome in WITNESS_READING_OUTCOMES and chair not in attached_chairs
        ),
        "health_unrecorded": health_unrecorded,
        "shortfalls": shortfalls,
        # The granularity of the evidence, never which evidence attached a chair:
        # that is each chair's own `attachment_basis`.
        "granularity_basis": (
            NATIVE_GRANULARITY_BASIS
            if native_evidence
            else INTERIM_GRANULARITY_BASIS
            if attachments is not None
            else LEGACY_GRANULARITY_BASIS
        ),
    }


SILENT_PAGE_REASON: Final = (
    "page {ordinal} was sealed and no act was marked out on it; silence cannot "
    "distinguish a blank page from a detection failure, and a blank page is proved "
    "rather than inferred"
)

NO_ATTRIBUTION_REASON: Final = (
    "the run supplied no act-to-page attribution, so no page could be checked for "
    "silence; a page that produced nothing cannot be told from a page nobody marked out"
)

NO_TEXT_STATUS_REASON: Final = (
    "act {act} was delivered with no established-text status record, so whether its "
    "one reading is whole was never measured"
)

# Keyed on the closed vocabulary, so a new status needs its own sentence; checked
# at import below.  `established` alone names no shortfall.
TEXT_STATUS_REASONS: Final[dict[str, str]] = {
    "partial": (
        "act {act} was delivered with partial text: its record carries ink the Perlector "
        "knows is present and could not read"
    ),
    "no_readable_text": (
        "act {act} was delivered with a record that establishes no readable text; a proved "
        "blank is confirmed-blank business, and a delivered act with no text is not reconciled"
    ),
}

if TEXT_STATUSES - {"established"} != set(TEXT_STATUS_REASONS):
    raise AssertionError(
        "every established-text status except 'established' must carry its own "
        f"aggregate reason sentence; statuses {sorted(TEXT_STATUSES)} vs reasons "
        f"{sorted(TEXT_STATUS_REASONS)}"
    )


def _attributed_pages(
    act_categories: Mapping[str, ArmariumCategory],
    page_census: Mapping[int, Mapping[str, Any]],
    act_pages: Mapping[str, Sequence[int]] | None,
    reasons: list[str],
) -> set[int] | None:
    """Which sealed pages an act was marked out on, or None when nobody said.

    Each act maps to *every* page ordinal it was marked out on, not to one. An act
    that runs over a page break is cut on both sides and examines both pages; a
    single primary ordinal would have called the far side silent and refused a run
    whose continuation page was read exactly as it should have been.

    Returning None rather than an empty set is the difference between "no page
    produced an act" and "this run does not know" — collapsing the two would let a
    caller that supplies nothing silently satisfy the silence check for every page.
    """
    if not page_census:
        # The missing-census reason already names this defect.
        return None
    if act_pages is None:
        if act_categories:
            reasons.append(NO_ATTRIBUTION_REASON)
            return None
        # No acts and no attribution: every page is silent, and each is named.
        return set()

    unknown_acts = sorted(set(act_pages) - set(act_categories))
    if unknown_acts:
        raise FatalAccounting(
            f"act-to-page attribution names unknown act(s) {unknown_acts}; attribution and "
            "terminal categories must describe the same act denominator"
        )
    attributed: set[int] = set()
    for act in sorted(act_categories):
        if act not in act_pages:
            reasons.append(
                f"act {act} names no page, so the page it came from cannot be checked for coverage"
            )
            continue
        for ordinal in act_pages[act]:
            page = page_census.get(ordinal)
            if page is None:
                raise FatalAccounting(
                    f"act {act} was marked out on page {ordinal}, which the run's page census "
                    "does not account for; an act on a page nobody counted is invariant #10's "
                    "imbalance"
                )
            if page.get("outcome") != "sealed":
                raise FatalAccounting(
                    f"act {act} was marked out on page {ordinal}, which the Exemplar did not "
                    "seal; nothing may be marked out on pixels that were never sealed"
                )
            attributed.add(ordinal)
    return attributed


def _attached_reading_count(act: str, record: Mapping[str, Any]) -> int:
    """The attached-reading count `under_witnessed` was decided from.

    Not the COMPLETED class count, which also holds `excluded` and page witnesses
    that did not align into this act; derived as `common/recensor_receipt.py`
    does, keyed on the recorded basis.  Raw indexing on purpose: a record
    claiming `under_witnessed` without its fields is malformed, and the
    Armarium's `_aggregate_from_basis` turns that `KeyError` into a refusal.
    """
    basis = record.get("granularity_basis", LEGACY_GRANULARITY_BASIS)
    if basis in {INTERIM_GRANULARITY_BASIS, NATIVE_GRANULARITY_BASIS}:
        reading_chairs = sum(
            record["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
        )
        return reading_chairs - record["page_granularity_only"]
    if basis == LEGACY_GRANULARITY_BASIS:
        return record["by_class"]["completed"]
    raise FatalAccounting(f"act {act} coverage names unknown granularity basis {basis!r}")


def run_aggregate(
    act_categories: Mapping[str, ArmariumCategory],
    coverage_records: Mapping[str, Mapping[str, Any]] | None = None,
    page_census: Mapping[int, Mapping[str, Any]] | None = None,
    unaddressed_chairs: Sequence[str] | None = None,
    act_pages: Mapping[str, Sequence[int]] | None = None,
    act_text_status: Mapping[str, str] | None = None,
    edge_hold_pages: Sequence[int] | None = None,
) -> dict[str, Any]:
    """The run's own terminal state, and every reason it is not `complete`.

    Principle 2, read literally: a partial result is visibly partial, and
    "complete" is refused unless everything reconciles. So `complete` here means
    every act reached a completed-class category, every configured chair
    reconciled against the configuration it was run under, AND every page in the
    census was sealed. Every reason is named in `reasons`; a run is never partial
    without saying why.

    Acts are discovered but pages are given, so the page census (the
    Exemplar's outcome per ordinal) is where a lost page shows.  An unknown
    page outcome is fatal, and a refusal with no recorded reason still forces
    `partial`.

    `act_pages` maps each act to every page it was marked out on, so a silent
    page is named rather than hidden by busy ones: a blank sheet and a missed
    faint page give the same zero-act signal, and blank is proved, never
    inferred.  A run that supplies no attribution is told so.

    `act_text_status` maps each delivered act to its sealed `TEXT_STATUSES`
    word: `delivered` says where an act ended, not whether its reading is
    whole, so a non-`established` status is a reason.  A delivered act with no
    status is named rather than assumed whole (principle 8); a status on an act
    that was not delivered is fatal, since no record exists for it to describe.

    `edge_hold_pages` is page-scoped because no act can yet own the unclaimed
    ink, so a held page keeps the aggregate partial even if its acts were
    delivered.
    """
    reasons: list[str] = []
    by_category: dict[str, int] = {}

    # An empty population reconciles vacuously, not actually.
    if not act_categories and not (page_census or {}):
        reasons.append("the run accounted for no acts and no pages, so nothing was reconciled")

    pages_with_acts = _attributed_pages(act_categories, page_census or {}, act_pages, reasons)

    coverage = coverage_records or {}
    unexpected_coverage = sorted(set(coverage) - set(act_categories))
    if unexpected_coverage:
        raise FatalAccounting(
            f"witness coverage names unknown act(s) {unexpected_coverage}; coverage and terminal "
            "categories must describe the same act denominator"
        )
    for act in sorted(set(act_categories) - set(coverage)):
        reasons.append(f"act {act} has no witness-coverage record")

    text_status = act_text_status or {}
    unexpected_status = sorted(set(text_status) - set(act_categories))
    if unexpected_status:
        raise FatalAccounting(
            f"established-text status names unknown act(s) {unexpected_status}; text status and "
            "terminal categories must describe the same act denominator"
        )

    if act_categories and not page_census:
        reasons.append("the run has acts but no page census, so page conservation was not checked")

    # A configured role no stage addresses (a misspelt witness) was resolved by
    # nothing.  A set, because the clean-machine verifier rebuilds this list from
    # retained basis and a repeat would read as two chairs.
    for chair in sorted(set(unaddressed_chairs or ())):
        reasons.append(
            f"chair {chair} is configured and no stage addresses that role, so nothing "
            "resolved it and no artifact records it"
        )

    # A hold on a page the census never counted would describe a page that does not exist.
    unknown_holds = sorted(set(edge_hold_pages or ()) - set(page_census or {}))
    if unknown_holds:
        raise FatalAccounting(
            f"an edge hold names page(s) {unknown_holds}, which the run's page census does not "
            "account for; edge holds and the page census must describe the same page denominator"
        )

    # A set: a repeated ordinal is one held page.
    for ordinal in sorted(set(edge_hold_pages or ())):
        reasons.append(
            f"page {ordinal} carries unreleased unclaimed-edge-ink: ink at its edge that no "
            "Designator crop on the page claims, so its coverage is not reconciled"
        )

    for act in sorted(act_categories):
        category = act_categories[act]
        if not isinstance(category, ArmariumCategory):
            raise FatalAccounting(f"act {act} carries {category!r}, not a category")
        by_category[category.value] = by_category.get(category.value, 0) + 1
        if VOCABULARIES[ARMARIUM][category.value] is not OutcomeClass.COMPLETED:
            reasons.append(f"act {act} is {category.value}")
        # After the category check, so a malformed category is what gets named.
        if category is not ArmariumCategory.DELIVERED:
            if act in text_status:
                raise FatalAccounting(
                    f"act {act} is {category.value} and carries an established-text status; only "
                    "a delivered act has an Archetypus record for a status to describe"
                )
            continue
        status = text_status.get(act)
        if status is None:
            reasons.append(NO_TEXT_STATUS_REASON.format(act=act))
        # isinstance first: an unhashable status would raise TypeError.
        elif not isinstance(status, str) or status not in TEXT_STATUSES:
            raise FatalAccounting(
                f"act {act} carries established-text status {status!r}, which is not one of "
                f"{sorted(TEXT_STATUSES)}"
            )
        elif status in TEXT_STATUS_REASONS:
            reasons.append(TEXT_STATUS_REASONS[status].format(act=act))

    for act in sorted(coverage):
        record = coverage[act]
        if record.get("under_witnessed"):
            completed = _attached_reading_count(act, record)
            reasons.append(
                f"act {act} is under-witnessed ({completed} of a floor of {record['floor']})"
            )
        if record.get("unresolved_chairs"):
            reasons.append(
                f"act {act} has {record['unresolved_chairs']} chair(s) with no outcome yet"
            )

    by_page_outcome: dict[str, int] = {}
    for ordinal in sorted(page_census or {}):
        outcome = page_census[ordinal].get("outcome")
        classify(EXEMPLAR, outcome)
        by_page_outcome[outcome] = by_page_outcome.get(outcome, 0) + 1
        if outcome != "sealed":
            reason = page_census[ordinal].get("reason") or "no reason was recorded"
            reasons.append(f"page {ordinal} was {outcome}: {reason}")
        elif pages_with_acts is not None and ordinal not in pages_with_acts:
            reasons.append(SILENT_PAGE_REASON.format(ordinal=ordinal))

    return {
        "status": "complete" if not reasons else "partial",
        "by_category": by_category,
        "by_page_outcome": by_page_outcome,
        "reasons": reasons,
    }
