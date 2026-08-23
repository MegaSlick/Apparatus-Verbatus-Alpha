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
act's text would be a picker wearing an accounting name, and GOVERNANCE 3 forbids
it under every name.

`failed` in the witness vocabulary is Sol's blocker 4 (finding B-2), repaired: spec
07 required a failed re-read to derive `current=FAILED` while the stated vocabulary
had no such member, so the supposedly closed algebra had a hole exactly where the
retention ruling bites.
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
# The witness outcomes that ARE a reading. Deliberately narrower than the
# ATTESTATORES COMPLETED class, which also holds `excluded` — an approval-bound
# exclusion is completed business, not a chair that looked at the ink. Defined
# here, in the vocabulary module, and re-exported by `common/stage.py`: it is one
# closed set, and a second literal spelling of it beside the floor arithmetic
# that depends on it is a silent divergence waiting to happen.
WITNESS_READING_OUTCOMES: Final = frozenset({"read", "genuinely-empty"})
INTERIM_GRANULARITY_BASIS: Final = "computed-act-attachment-alignment"
NATIVE_GRANULARITY_BASIS: Final = "native-observation-overlap"
LEGACY_GRANULARITY_BASIS: Final = "legacy-class-only"

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
    # The map is page evidence, not an act decision.  In particular, an
    # unclaimed edge signal is deliberately carried onward: Unit 14 owns the
    # explicit hold that will make that evidence terminal.
    INK_MAP: {
        "mapped": _C.COMPLETED,
        "unclaimed-edge-ink": _C.UNRESOLVED,
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
        # The chair read the region and there was genuinely nothing to report.
        # That is a reading, not an absence — the old stage could not tell the
        # difference, and collapsed both into one empty file.
        "genuinely-empty": _C.COMPLETED,
        # An attempt was made and produced no usable Testimonium. The failed
        # attempt artifact is the evidence. This is blocker 4's member.
        "failed": _C.FAILED,
        # Configured but unavailable; no attempt reached the region.
        "dead": _C.FAILED,
        # Configured, never attempted, nothing went wrong yet.
        "not-run": _C.UNRESOLVED,
        "excluded": _C.COMPLETED,
    },
    PERLECTOR: {
        "read": _C.COMPLETED,
        # An explicit status, never an empty string standing in for one. Silence
        # does not prove a blank page or act, so it remains unresolved until a
        # future Recensor blank-proof contract can establish confirmed-blank.
        "no-readable-text": _C.UNRESOLVED,
        # ARCHITECTURE: it reads through to the end — truncation is a failure,
        # not an output.
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

# These two record kinds are stage-boundary evidence, not a second outcome
# algebra.  They nevertheless use the ordinary envelope, whose outcome is
# intentionally total by producer stage.
for _stage in VOCABULARIES:
    if _stage == ARMARIUM:
        continue
    VOCABULARIES[_stage]["sealed"] = _C.COMPLETED
    VOCABULARIES[_stage]["recorded"] = _C.COMPLETED

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
    if _stage == ARMARIUM:
        continue
    TERMINAL_CATEGORY[(_stage, "sealed")] = None
    TERMINAL_CATEGORY[(_stage, "recorded")] = None

# The Perlector's failures are transitive on purpose: a truncated or failed reading
# is the Recensor's to act on — it may request bounded recovery — and only the
# Recensor's own outcome terminates the act. A Perlector failure that terminated
# the act here would take the recovery loop out of the architecture by accident.


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
    # Both spellings, because they are the same fact at two stages. A stage says
    # `excluded`; the Armarium's terminal category for it is
    # `excluded-with-approval`. Matching only the first left the category that
    # *names* approval as the one place the check did not reach, so an export
    # entry could carry it with no approval reference at all.
    if outcome not in ("excluded", ArmariumCategory.EXCLUDED_WITH_APPROVAL.value):
        return
    if not isinstance(approval_ref, str) or not approval_ref:
        raise ApprovalRefusal(
            f"{stage} outcome {outcome!r} carries no approval-record reference; "
            "only Tyrel approves an exclusion, and the artifact is the approval"
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
# the two words apart is that it can (Tyrel, 2026-08-05: "many of our records are
# damaged").
#
# It lives here rather than in the Archetypus because two stages read it and
# stages talk only through `common/` (`pipeline/test_stage_import_boundaries.py`):
# the Archetypus derives it when it seals a record, and the Armarium *recomputes*
# it from the layers travelling beside the text rather than believing the field.
# One spelling, so the two cannot drift into disagreeing about the same record.

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

    The canonical gaps are read *before* the empty-text case rather than only
    where the rest already said `established`. A whole-act gap over empty text is
    "ink present, wholly unread", which is the middle silence and not the last
    one; asking about it second returned `no_readable_text` for it and would have
    let a record be sealed claiming nothing was there while carrying a gap saying
    otherwise. `derive_text_status` has always ordered the older layer this way
    (`pipeline/6_archetypus/test_text_status.py::
    test_any_gap_forces_partial_even_with_empty_text`); this is the same ruling
    applied to the layer that superseded it.
    """
    if not isinstance(uncertainty, Mapping) or not isinstance(
        uncertainty.get("gaps"), (list, tuple)
    ):
        raise SchemaRefusal(
            "a record text status requires the canonical uncertainty layer's own gap list"
        )
    if uncertainty["gaps"]:
        # The text is still read for its type, so a malformed record cannot reach a
        # status by way of the one branch that never looked at it.
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
    if attachments is not None:
        unknown = set(attachments) - set(chair_outcomes)
        if unknown:
            raise FatalAccounting(
                f"act attachment facts name unconfigured chair(s) {sorted(unknown)}"
            )
        for chair, outcome in chair_outcomes.items():
            fact = attachments.get(chair)
            if fact is True:
                fact = {"attached": True, "comparable": True}
            elif fact is False or fact is None:
                fact = {"attached": False, "comparable": False}
            if (
                not isinstance(fact, Mapping)
                or not isinstance(fact.get("attached"), bool)
                or not isinstance(fact.get("comparable"), bool)
            ):
                raise FatalAccounting(
                    f"act attachment fact for {chair!r} has no boolean attached/comparable pair"
                )
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
        # Pre-R0 callers do not carry attachment facts. Preserve their established
        # class-level arithmetic; R0 consumers opt into act-granularity facts.
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
        # An unresolved chair is a question nobody answered; it is not evidence of
        # anything, so it cannot sit inside a run that calls itself complete.
        "unresolved_chairs": by_class[OutcomeClass.UNRESOLVED.value],
        # These facts are deliberately separate from the closed witness outcome
        # vocabulary. A page-only report, a truncation and an unaligned span are
        # coverage facts, not invented witness outcomes.
        "page_granularity_only": sum(
            1
            for chair, outcome in chair_outcomes.items()
            if outcome in WITNESS_READING_OUTCOMES and chair not in attached_chairs
        ),
        "health_unrecorded": health_unrecorded,
        "shortfalls": shortfalls,
        # Attachments are computed facts: page testimony counts only where its
        # retained text aligned through Chandra's anchor into act geometry.
        "granularity_basis": (
            NATIVE_GRANULARITY_BASIS if attachments is not None else LEGACY_GRANULARITY_BASIS
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

# Keyed on the closed vocabulary rather than tested for `!= "established"`, so a
# status added to `TEXT_STATUSES` later has to be given its own sentence here
# instead of inheriting a generic one — and an unknown status is fatal below
# rather than silently reconciling. `established` is the only member with no
# entry, because it is the only one that names no shortfall.
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

# Enforced at import, so the comment above is a checked claim rather than an
# intention: a status added to `TEXT_STATUSES` without its own sentence fails
# the first time anything touches this module, not the first time a run
# happens to deliver an act carrying it.
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
        # There is no page denominator to check silence against, and the caller is
        # already told so by the missing-census reason. Saying it twice would make
        # one defect look like two.
        return None
    if act_pages is None:
        if act_categories:
            reasons.append(NO_ATTRIBUTION_REASON)
            return None
        # No acts and no attribution is not ignorance: every page is silent, and
        # the loop below names each one.
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
) -> dict[str, Any]:
    """The run's own terminal state, and every reason it is not `complete`.

    GOVERNANCE 2, read literally: a partial result is visibly partial, and
    "complete" is refused unless everything reconciles. So `complete` here means
    every act reached a completed-class category, every configured chair
    reconciled against the configuration it was run under, AND every page in the
    census was sealed. Every reason is named in `reasons`; a run is never partial
    without saying why.

    The page census exists because acts are discovered but pages are given: the
    proposal seal only ever names acts that were marked out, so a page the door
    refused left no hole in the act-level conservation check at all. A run that
    lost a whole page could report `status: complete, reasons: []` — the census,
    keyed by ordinal with the Exemplar's own outcome for each page, is where that
    loss becomes visible. A page outcome outside the Exemplar's vocabulary is
    fatal, never routed around, and a refusal with no recorded reason still
    forces `partial` — absent evidence never reads cleaner than damaged evidence.

    `act_pages` maps each act key to every page ordinal it was marked out on, and
    it is what makes the blank-by-silence refusal reach a *page* rather than only
    the whole run. Tyrel's ruling of 2026-08-04 is that truly blank pages exist at the
    scale of tens of thousands of archival scans and are not failures — but that a
    blank sheet and a page whose faint ink the Designator missed give the identical
    zero-act signal. Blank is proved, never inferred. Checking only "did the run
    produce any acts at all" left that proof obligation satisfied by any other page:
    one silent page among ten thousand busy ones reconciled to `complete` with no
    reason named at all. Attribution is what closes it, and a run that supplies none
    is told so rather than believed.

    `act_text_status` maps each *delivered* act key to the `TEXT_STATUSES` word its
    Archetypus record sealed, and it carries the half of GOVERNANCE 2 the category
    vocabulary alone can never state. `delivered` is a fact about where the act
    ended; it says nothing about whether the reading that left is whole. An act the
    Perlector itself recorded a gap in — ink known to be present and unread — was
    aggregating to `complete` with an empty reason list, which is "a partial result
    is visibly partial" failing at the last boundary in the one case Tyrel expects
    to be ordinary rather than exceptional. A non-`established` status therefore
    contributes its own named reason, exactly as an under-witnessed act or a refused
    page does.

    A delivered act with no status supplied is named too, rather than assumed whole:
    that is the same "this run does not know" `NO_ATTRIBUTION_REASON` refuses to
    round up, and an unmeasured metric is a failure rather than a pass
    (GOVERNANCE 10). A status attached to an act that was *not* delivered is fatal
    instead — no Archetypus record exists for it, so the claim describes a reading
    that is not there.
    """
    reasons: list[str] = []
    by_category: dict[str, int] = {}

    # A run that examined nothing is not a run in which everything reconciled.
    # `reasons` starts empty and the loops below can each execute zero times, so
    # an aggregate over no acts and no pages fell straight through to `complete`
    # — a green verdict asserting that nothing had gone wrong with nothing.
    # GOVERNANCE 2 refuses "complete" unless everything reconciles, and an empty
    # population reconciles vacuously rather than actually.
    #
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

    # "Every configured chair reconciled against the configuration it was run
    # under" was in this docstring before anything checked it. A configured chair
    # whose role no stage addresses — a misspelt witness, most plainly — was
    # resolved by nothing and named in no artifact, and the run still reported
    # `complete`. It is named here instead, every time.
    for chair in sorted(unaddressed_chairs or ()):
        reasons.append(
            f"chair {chair} is configured and no stage addresses that role, so nothing "
            "resolved it and no artifact records it"
        )

    for act in sorted(act_categories):
        category = act_categories[act]
        if not isinstance(category, ArmariumCategory):
            raise FatalAccounting(f"act {act} carries {category!r}, not a category")
        by_category[category.value] = by_category.get(category.value, 0) + 1
        if VOCABULARIES[ARMARIUM][category.value] is not OutcomeClass.COMPLETED:
            reasons.append(f"act {act} is {category.value}")
        # Asked here, where `category` is already proven to be a real category, so
        # a status beside a malformed one is refused by the message about the
        # malformed category rather than by this one.
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
        # isinstance before membership: an unhashable value (a list, a dict)
        # out of a hand-built basis would raise TypeError from the frozenset
        # test, and this boundary owes a named fatal instead of a crash.
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
            # Not unconditionally `record["by_class"]["completed"]`: that is the
            # wider ATTESTATORES COMPLETED class (it also holds `excluded`, and --
            # since R4 -- a page witness that read its page but did not align
            # into this act), while `under_witnessed` above is decided from the
            # narrower attached-reading count. The two were equal before per-act
            # alignment existed, so this message could get away with the class
            # count; they can now diverge, and printing the wider number produced
            # a floor-satisfying count next to an under-witnessed verdict.
            # Rederived from `by_outcome` and `page_granularity_only` exactly as
            # `common/recensor_receipt.py` already does for the same reason
            # (including its same v1/v2 branch: `page_granularity_only` is
            # optional on a pre-R0 schema-v1 record, where `by_class['completed']`
            # is the whole answer with no page-granularity distinction to draw).
            # Deliberately raw indexing, not `.get(..., default)`: a record
            # claiming `under_witnessed` without the fields that justify it is
            # not a zero to report, it is malformed evidence, and
            # `pipeline/7_armarium/armarium_export.py::_run_aggregate` already
            # converts exactly that `KeyError` into a named refusal rather than
            # let a fabricated count stand in for one nothing measured.
            # Keyed on the recorded basis, not on key presence: `witness_coverage`
            # emits `page_granularity_only` on its legacy path too, where
            # `under_witnessed` is decided from the COMPLETED class (which also
            # holds `excluded`). Rederiving from reading outcomes there printed a
            # number no rule in this file produced -- {read, excluded, dead}
            # against a floor of 3 flags at 2 and reported 1 -- the same class of
            # defect this branch exists to repair.
            basis = record.get("granularity_basis", LEGACY_GRANULARITY_BASIS)
            if basis in {INTERIM_GRANULARITY_BASIS, NATIVE_GRANULARITY_BASIS}:
                reading_chairs = sum(
                    record["by_outcome"].get(outcome, 0) for outcome in WITNESS_READING_OUTCOMES
                )
                completed = reading_chairs - record["page_granularity_only"]
            elif basis == LEGACY_GRANULARITY_BASIS:
                completed = record["by_class"]["completed"]
            else:
                # A closed vocabulary, closed here too: a basis this module never
                # produced is malformed evidence, not a default to guess from.
                raise FatalAccounting(
                    f"act {act} coverage names unknown granularity basis {basis!r}"
                )
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
