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
act's text would be a picker wearing an accounting name, no matter what it is
called.
"""

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any, Final

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
        # A whole-page reading, or one entry of it, kept for review: read, but
        # not accepted as it stands (`pipeline/4_perlector/page_run.py`).
        "held": _C.UNRESOLVED,
    },
    RECENSOR: {
        "accepted": _C.COMPLETED,
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
    (PERLECTOR, "held"): None,
    (RECENSOR, "accepted"): None,
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

# The Perlector's failures are transitive on purpose: only the Recensor's
# review terminates the act.


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


def witness_coverage(chair_outcomes: Mapping[str, str], configured_floor: int) -> dict[str, Any]:
    """Aggregate one unit's chair outcomes into the coverage counts.

    Returns counts and two flags, and deliberately returns no category and no
    text. The caller may record this beside a unit; nothing may branch the
    unit's reading on it.

    `under_witnessed` is chairs reaching a completed-class outcome below the
    configured floor. Spec 07: three chairs is the floor, the machinery tolerates
    fewer so one dead witness never kills a run, and a run below the floor is
    recorded as under-witnessed in the Recensor receipt and the export manifest,
    visibly, every time. A page review judges its own flag by `witnessed_count`.
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
    return {
        "configured": len(chair_outcomes),
        "floor": configured_floor,
        "by_outcome": by_outcome,
        "by_class": by_class,
        "under_witnessed": by_class[OutcomeClass.COMPLETED.value] < configured_floor,
        # An unanswered chair cannot sit inside a complete run.
        "unresolved_chairs": by_class[OutcomeClass.UNRESOLVED.value],
    }


def witnessed_count(coverage: Mapping[str, Any]) -> int:
    """The count a page review's `under_witnessed` flag is judged from.

    Every witness reads the whole page, so it is the reading outcomes less the
    truncated ones: reading outcomes, not the COMPLETED class, because that
    class also holds approval exclusions that never looked at the ink.
    """
    reading_chairs = sum(
        coverage["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
    )
    return reading_chairs - coverage["shortfalls"]["truncated"]


# A run reads every sealed page whole, so a page reaches the aggregate through
# its readings.
PAGE_READ_SILENT_PAGE_REASON: Final = (
    "page {ordinal} was sealed and read whole, yet the page-read denominator counts no "
    "reading of it; a page no reading accounts for cannot be told from one nobody read"
)
NO_ACT_PAGE_HELD_REASON: Final = (
    "page {ordinal} was read and carries no act; its other readings are {categories}, not "
    "delivered, because they are held until the Recensor confirms that no act is on the "
    "page; once it does, they are delivered in the other layer and the page counts as a "
    "confirmed no-act page"
)
HELD_OTHER_ON_ACT_PAGE_REASON: Final = (
    "page {ordinal} carries acts and other readings that are {categories}, not delivered; "
    "an other reading held for review may be an act the reading did not establish, so the "
    "page's readings are not all accounted for"
)
CONFIRMED_NO_ACT_PAGE_REASON: Final = (
    "page {ordinal} was read and the Recensor confirmed it carries no act; its other "
    "readings are delivered in the other layer"
)
UNPAIRED_CONTINUATION_REASON: Final = (
    "act {act} says it {says}, and no continuation link pairs it with a reading across "
    "that break; it is delivered as its own literal and may be only part of an act"
)
CONTINUATION_FLAGS: Final = ("continues_from_previous_page", "continues_to_next_page")
_CONTINUATION_SAYS: Final = {
    "continues_from_previous_page": "continues from the previous page",
    "continues_to_next_page": "continues onto the next page",
}

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


def run_aggregate(
    act_categories: Mapping[str, ArmariumCategory],
    coverage_records: Mapping[str, Mapping[str, Any]] | None = None,
    page_census: Mapping[int, Mapping[str, Any]] | None = None,
    unaddressed_chairs: Sequence[str] | None = None,
    act_pages: Mapping[str, Sequence[int]] | None = None,
    act_text_status: Mapping[str, str] | None = None,
    edge_hold_pages: Sequence[int] | None = None,
    continuation_joins: Sequence[Mapping[str, Any]] | None = None,
    *,
    other_categories_by_page: Mapping[int, Sequence[str]] | None = None,
    unpaired_continuations: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """The run's own terminal state, and every reason it is not `complete`.

    A partial result is visibly partial, and
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
    status is named rather than assumed whole; a status on an act
    that was not delivered is fatal, since no record exists for it to describe.

    `edge_hold_pages` is page-scoped because no act can yet own the unclaimed
    ink, so a held page keeps the aggregate partial even if its acts were
    delivered. Each `continuation_joins` row does the same for a page break the
    geometry says an act may cross: its sides are delivered apart, unjoined.

    A sealed page with no act row is a page whose readings are all `other` (`other_categories_by_page`
    names their categories): held, with its reason, until every one is
    delivered, which the Recensor allows only once it confirms no act is on the
    page. On a page with acts, an `other` reading that was not delivered is
    a reason too: it may be an act the reading did not establish. Each
    `unpaired_continuations` row `(act, flag)` is a delivered act
    whose continuation flag no link pairs, which keeps the run partial.
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
            "reading region on the page claims, so "
            "its coverage is not reconciled"
        )

    for join in continuation_joins or ():
        crossing = (
            f"continuation join {join['join_id']} ({join['status']}): an act may cross the "
            f"break from page {join['head_page_ordinal']} to page {join['tail_page_ordinal']}; "
        )
        if join["status"] == "reconstructed":
            reasons.append(
                crossing + "each side is delivered as its own literal beside a labelled, "
                "unconfirmed reconstruction"
            )
        else:
            reasons.append(
                crossing + f"no reconstruction was made ({join['not_reconstructed_reason']}), "
                "and no act was joined"
            )

    for act, flag in sorted(set(unpaired_continuations)):
        if act not in act_categories or flag not in _CONTINUATION_SAYS:
            raise FatalAccounting(
                f"an unpaired continuation names {act!r} and {flag!r}, not a counted act and a "
                "continuation flag"
            )
        reasons.append(UNPAIRED_CONTINUATION_REASON.format(act=act, says=_CONTINUATION_SAYS[flag]))

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
            # Raw indexing: a flag without its counts is malformed, and the
            # Armarium's `_aggregate_from_basis` turns the `KeyError` into a refusal.
            reasons.append(
                f"act {act} is under-witnessed ({witnessed_count(record)} of a floor of "
                f"{record['floor']})"
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
        elif pages_with_acts is not None and ordinal in pages_with_acts:
            held = set((other_categories_by_page or {}).get(ordinal) or ()) - {
                ArmariumCategory.DELIVERED.value
            }
            if held:
                reasons.append(
                    HELD_OTHER_ON_ACT_PAGE_REASON.format(
                        ordinal=ordinal, categories=", ".join(sorted(held))
                    )
                )
        elif pages_with_acts is not None and ordinal not in pages_with_acts:
            others = (other_categories_by_page or {}).get(ordinal)
            if not others:
                reasons.append(PAGE_READ_SILENT_PAGE_REASON.format(ordinal=ordinal))
            elif set(others) != {ArmariumCategory.DELIVERED.value}:
                reasons.append(
                    NO_ACT_PAGE_HELD_REASON.format(
                        ordinal=ordinal, categories=", ".join(sorted(set(others)))
                    )
                )

    return {
        "status": "complete" if not reasons else "partial",
        "by_category": by_category,
        "by_page_outcome": by_page_outcome,
        "reasons": reasons,
    }
