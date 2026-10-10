"""The outcome algebra: two layers, and a total mapping between them.

The spine is a *total partition, proven at every stage*: every
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

from .canonical import is_plain_int
from .errors import ApprovalRefusal, FatalAccounting, SchemaRefusal
from .stages import (
    ARCHETYPUS,
    ARMARIUM,
    ATTESTATORES,
    CONIECTOR,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
)


class OutcomeClass(str, Enum):
    """The three terminal sets. Every unit is in exactly one."""

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
    # Page evidence, not an act decision: the page is held for review through the
    # `edge_hold_pages` that `run_aggregate` takes, so an unclaimed edge still
    # reaches a terminal category.
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
        # Completed only because an operator review decision says the unit is not
        # an act; `require_approval` refuses the word without its record.
        "excluded": _C.COMPLETED,
    },
    ARCHETYPUS: {
        "established": _C.COMPLETED,
        "refused": _C.FAILED,
    },
    # The Coniector's records describe a reconstruction beneath an act, never the
    # act: none of them decides where an act ends.
    CONIECTOR: {
        # The run's plan: which calls it asks, under which switches.
        "planned": _C.COMPLETED,
        # One page's call: answered in the grammar, or not (named problems).
        "answered": _C.COMPLETED,
        "not-answered": _C.FAILED,
        # One act's or one join's reconstruction; not made carries its reason.
        "made": _C.COMPLETED,
        "not-made": _C.UNRESOLVED,
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
    # What one Recensor pass did with the run's operator review decisions; it
    # decides about no unit, each review does.
    "review-decisions": "recorded",
    # Which page a routed witness reads (`common/witness_routing.py`): a roster
    # fact about one page, never a witness outcome or an act's category.
    "witness-routing": "recorded",
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
    (PERLECTOR, "failed"): None,
    (PERLECTOR, "not-run"): None,
    (PERLECTOR, "held"): None,
    (RECENSOR, "accepted"): None,
    (RECENSOR, "confirmed-blank"): _A.CONFIRMED_BLANK,
    (RECENSOR, "held-for-review"): _A.HELD_FOR_REVIEW,
    (RECENSOR, "failed"): _A.REFUSED_WITH_REASON,
    (RECENSOR, "excluded"): _A.EXCLUDED_WITH_APPROVAL,
    (ARCHETYPUS, "established"): _A.DELIVERED,
    (ARCHETYPUS, "refused"): _A.REFUSED_WITH_REASON,
    (CONIECTOR, "planned"): None,
    (CONIECTOR, "answered"): None,
    (CONIECTOR, "not-answered"): None,
    (CONIECTOR, "made"): None,
    (CONIECTOR, "not-made"): None,
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
            f"its closed vocabulary is {sorted(vocabulary)}. A unit in no set is a "
            "fatal accounting imbalance, never a warning"
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

    Only two outcome words in the whole algebra are approval-bound, `excluded` (an
    Attestatores or Recensor outcome) and its Armarium category, and both mean a unit
    left the pipeline as `completed` without its text being established. A claimed
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


def derive_record_text_status(text: Any, uncertainty: Any) -> str:
    """established | partial | no_readable_text, from a record's text and its gaps.

    A gap in the canonical `uncertainty` layer means some ink is known and unread,
    whether `text` is otherwise empty or full: `partial`. No gap and no text is
    the only remaining case, `no_readable_text`; no stage establishes or delivers
    one. Gaps are read before the empty-text case, so a whole-act gap over empty
    text is `partial`: "we could not read it" never becomes "there was nothing to
    read". The text is type-checked on every path, so no status is returned over
    a text nobody checked.
    """
    if not isinstance(text, str):
        raise SchemaRefusal("a text status requires exactly one string text field")
    if not isinstance(uncertainty, Mapping) or not isinstance(
        uncertainty.get("gaps"), (list, tuple)
    ):
        raise SchemaRefusal(
            "a record text status requires the canonical uncertainty layer's own gap list"
        )
    if uncertainty["gaps"]:
        return "partial"
    if text.strip() == "":
        return "no_readable_text"
    return "established"


# --- Witness coverage: outcomes aggregate into counts, never into text ----------


def witness_coverage(chair_outcomes: Mapping[str, str], configured_floor: int) -> dict[str, Any]:
    """Aggregate one unit's chair outcomes into the coverage counts.

    Returns counts and two flags, and deliberately returns no category and no
    text. The caller may record this beside a unit; nothing may branch the
    unit's reading on it.

    `under_witnessed` is chairs reaching a completed-class outcome below the
    configured floor. Three chairs is the floor; the machinery tolerates fewer so
    one dead witness never kills a run, and a run below the floor is recorded as
    under-witnessed in the Recensor receipt and the export manifest, visibly,
    every time. A page review judges its own flag by `witnessed_count`.
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
SYSTEMIC_REASON_PREFIX: Final = "systemic: "
# What `run_aggregate` reads to name a systemic held share
# (`common.page_review.held_share`): the held pages, the pages counted, and the
# run's sealed limit.
SYSTEMIC_REVIEW_FIELDS: Final = frozenset({"held_pages", "pages", "max_held_page_share"})
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
                    "does not account for; an act on a page nobody counted is an accounting "
                    "imbalance"
                )
            if page.get("outcome") != "sealed":
                raise FatalAccounting(
                    f"act {act} was marked out on page {ordinal}, which the Exemplar did not "
                    "seal; nothing may be marked out on pixels that were never sealed"
                )
            attributed.add(ordinal)
    return attributed


REVIEW_CLEARANCE_FIELDS: Final = frozenset({"scope", "subject", "page", "decision", "cleared"})
# The decisions that clear a hold, by scope.
_CLEARING_DECISIONS: Final = {"unit": ("release", "exclude"), "page": ("no-missed-act",)}


def systemic_reason(held_pages: Sequence[int], pages: int, limit: str) -> str:
    """The reason a run whose held share is above its sealed limit carries, wherever it goes."""
    return (
        f"{SYSTEMIC_REASON_PREFIX}{len(held_pages)} of {pages} page(s) are held after the "
        f"recensor, more than the sealed limit of {limit} (config/review.toml); so many holds "
        "point to a problem with the run itself, not a few hard pages "
        f"(held pages: {', '.join(str(page) for page in held_pages)})"
    )


def _systemic_review_reason(review: Mapping[str, Any]) -> str:
    """The systemic reason of a `run_aggregate` `systemic_review` record, refused when malformed.

    The pages are the alarm's own count of the run's reviewed pages; canary pages
    are not among them, so the reason names only pages of the register.
    """
    if not isinstance(review, Mapping) or set(review) != SYSTEMIC_REVIEW_FIELDS:
        raise FatalAccounting(
            f"a systemic review record is not the closed {sorted(SYSTEMIC_REVIEW_FIELDS)} record"
        )
    held, pages, limit = review["held_pages"], review["pages"], review["max_held_page_share"]
    if (
        not isinstance(held, list)
        or not held
        or not all(is_plain_int(page) and page > 0 for page in held)
        or held != sorted(set(held))
        or not is_plain_int(pages)
        or not len(held) <= pages
        or type(limit) is not str
        or not limit
    ):
        raise FatalAccounting(
            f"the systemic review record names held pages {held!r} of {pages!r} under "
            f"{limit!r}, not page ordinals within a page count under a sealed limit"
        )
    return systemic_reason(held, pages, limit)


def _clearance_reasons(
    rows: Sequence[Mapping[str, Any]],
    act_categories: Mapping[str, ArmariumCategory],
    page_census: Mapping[int, Mapping[str, Any]],
) -> list[str]:
    """The reason each operator clearance keeps the run partial, one per cleared subject."""
    named: dict[tuple[str, str], str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != REVIEW_CLEARANCE_FIELDS:
            raise FatalAccounting(
                f"a review clearance is not the closed {sorted(REVIEW_CLEARANCE_FIELDS)} row"
            )
        scope, subject, page = row["scope"], row["subject"], row["page"]
        decision, cleared = row["decision"], row["cleared"]
        if not is_plain_int(page) or page not in page_census:
            raise FatalAccounting(f"a review clearance names page {page!r}, not a census page")
        if scope == "unit" and type(subject) is str and subject:
            what = f"act {subject}" if subject in act_categories else f"reading {subject}"
            what += f" on page {page}"
        elif scope == "page" and subject == page:
            what = f"page {page}"
        else:
            raise FatalAccounting(f"a review clearance names {scope!r} {subject!r} on page {page}")
        if decision not in _CLEARING_DECISIONS[scope] or not (
            isinstance(cleared, list) and all(type(code) is str and code for code in cleared)
        ):
            raise FatalAccounting(
                f"the review clearance of {what} names no clearing decision or no codes"
            )
        key = (scope, repr(subject))
        if key in named:
            raise FatalAccounting(f"{what} is named by more than one review clearance")
        codes = ", ".join(sorted(cleared)) or "no machine hold"
        named[key] = (
            f"{what} was cleared by an operator review decision ({decision}), clearing "
            f"{codes}; a person's decision, not a machine check"
        )
    return [named[key] for key in sorted(named)]


# The sentences that state one fact each. The Armarium's terminal ledger names
# the same facts with the same functions, so one fact is one string on both sides.


def unresolved_act_reason(act: str, category: str) -> str:
    """An act that did not reach a completed category."""
    return f"act {act} is {category}"


def edge_hold_reason(ordinal: int) -> str:
    """A sealed page held for ink at its edge no reading region claims."""
    return (
        f"page {ordinal} carries unreleased unclaimed-edge-ink: ink at its edge that no "
        "reading region on the page claims, so its coverage is not reconciled"
    )


def unsealed_page_reason(ordinal: int, outcome: str, reason: str | None) -> str:
    """A submitted page the Exemplar did not seal, with its recorded reason."""
    return f"page {ordinal} was {outcome}: {reason or 'no reason was recorded'}"


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
    review_clearances: Sequence[Mapping[str, Any]] = (),
    review_page_holds: Mapping[int, Sequence[str]] | None = None,
    systemic_review: Mapping[str, Any] | None = None,
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
    delivered. Each `continuation_joins` row does the same for a page break an
    answer's continuation flag names: its sides are delivered apart, unjoined.

    A sealed page with no act row is a page whose readings are all `other` (`other_categories_by_page`
    names their categories): held, with its reason, until every one is
    delivered, which the Recensor allows only once it confirms no act is on the
    page. On a page with acts, an `other` reading that was not delivered is
    a reason too: it may be an act the reading did not establish. Each
    `unpaired_continuations` row `(act, flag)` is a delivered act
    whose continuation flag no link pairs, which keeps the run partial.

    Each `review_clearances` row `{scope, subject, page, decision, cleared}` is
    a hold an operator review decision cleared: a unit (`scope` "unit",
    `subject` its key, an act or another reading) or a page (`scope` "page",
    `subject` its ordinal), on census page `page`. A person's decision is not a
    machine check, so every clearance is a reason and a run whose every hold
    was cleared stays partial. `review_page_holds` maps each page still held
    once decisions are applied, reviewed or not, to its codes; it holds even
    when every unit on it was excluded. `systemic_review` is given when more of
    the run's pages were held after the Recensor than its sealed limit allows
    and a person's advance passed them: the run reached export, and its
    systemic reason travels with it.
    """
    reasons: list[str] = []
    by_category: dict[str, int] = {}
    review_page_holds = review_page_holds or {}

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
        reasons.append(edge_hold_reason(ordinal))

    for join in continuation_joins or ():
        crossing = (
            f"continuation join {join['join_id']} ({join['status']}): an act may cross the "
            f"break from page {join['head_page_ordinal']} to page {join['tail_page_ordinal']}; "
        )
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

    reasons.extend(_clearance_reasons(review_clearances, act_categories, page_census or {}))
    for ordinal in sorted(review_page_holds):
        codes = review_page_holds[ordinal]
        if ordinal not in (page_census or {}) or not codes:
            raise FatalAccounting(
                f"a review page hold names page {ordinal!r} and codes {codes!r}, not a held "
                "census page"
            )
        if not isinstance(codes, list) or not all(type(code) is str and code for code in codes):
            raise FatalAccounting(
                f"the review page hold of page {ordinal} names {codes!r}, not a list of hold codes"
            )
        reasons.append(f"page {ordinal} is still held by {', '.join(sorted(codes))}")
    if systemic_review is not None:
        reasons.append(_systemic_review_reason(systemic_review))

    for act in sorted(act_categories):
        category = act_categories[act]
        if not isinstance(category, ArmariumCategory):
            raise FatalAccounting(f"act {act} carries {category!r}, not a category")
        by_category[category.value] = by_category.get(category.value, 0) + 1
        if VOCABULARIES[ARMARIUM][category.value] is not OutcomeClass.COMPLETED:
            reasons.append(unresolved_act_reason(act, category.value))
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
        # Both flags are required, so a record stripped of them cannot pass as witnessed.
        if (
            not isinstance(record, Mapping)
            or type(record.get("under_witnessed")) is not bool
            or type(record.get("unresolved_chairs")) is not int
            or record["unresolved_chairs"] < 0
        ):
            raise FatalAccounting(
                f"act {act}'s witness coverage record carries no boolean under_witnessed and "
                "non-negative integer unresolved_chairs, so it cannot say whether the act "
                "was witnessed"
            )
        if record["under_witnessed"]:
            reasons.append(
                f"act {act} is under-witnessed ({witnessed_count(record)} of a floor of "
                f"{record['floor']})"
            )
        if record["unresolved_chairs"]:
            reasons.append(
                f"act {act} has {record['unresolved_chairs']} chair(s) with no outcome yet"
            )

    by_page_outcome: dict[str, int] = {}
    for ordinal in sorted(page_census or {}):
        outcome = page_census[ordinal].get("outcome")
        classify(EXEMPLAR, outcome)
        by_page_outcome[outcome] = by_page_outcome.get(outcome, 0) + 1
        if outcome != "sealed":
            reasons.append(
                unsealed_page_reason(ordinal, outcome, page_census[ordinal].get("reason"))
            )
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
