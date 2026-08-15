"""Run the complete fake candidate matrix and derive evidence without a picker.

There is intentionally no adapter implementation here.  Tests inject small fakes
behind ``Candidate``.  A later real adapter must pass the same preflight and cannot
make an external call before prompt, held-out, and disclosure checks have succeeded.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from math import fsum, isfinite
from typing import Iterable

from .encoding import is_sha256
from .errors import MatrixRefusal, MeasurementRefusal
from .gates import RunAuthorization, require_authorized_delivery
from .holdout import EvaluationManifest, PrivateSampleAccounting
from .models import (
    ALL_CONDITIONS,
    Candidate,
    CandidateResponse,
    Condition,
    DissentSummary,
    DossierTestimonium,
    EvaluationAct,
    LimitationDisclosureState,
    MaterialClass,
    OutputStatus,
    Perlectio,
    PublicLimitationCode,
    ReferenceStatus,
    ResolvedIdentity,
    RunLimitations,
    WitnessConfiguration,
    anonymous_testimonia,
    dossier_for,
)
from .normalization import NormalizationProfile, normalize_text, require_canonical_profile
from .prompting import PromptRegistry
from .protocol import require_predeclared_protocol
from .roster import CandidateRoster, validate_perlector_candidate
from .scoring import ActScore, hypothesis_for_status, score_response


@dataclass(frozen=True, slots=True)
class CandidateCell:
    """One private, accounted-for candidate × act × condition observation."""

    opaque_act_id: str
    perlectio: Perlectio
    raw_response_text: str | None
    score: ActScore | None


@dataclass(frozen=True, slots=True)
class WitnessBaseline:
    """A Testimonium scored directly against checked ink, never emitted as text."""

    opaque_act_id: str
    public_source_index: int
    status: OutputStatus
    score: ActScore | None


class FailedAttemptKind(StrEnum):
    """A harness failure retained without inventing a Perlectio or model score."""

    ADAPTER_EXCEPTION = "adapter-exception"
    INVALID_RESPONSE = "invalid-response"
    PROMPT_RECEIPT_MISMATCH = "prompt-receipt-mismatch"
    DOSSIER_RECEIPT_MISMATCH = "dossier-receipt-mismatch"
    DELIVERY_RECEIPT_MISMATCH = "delivery-receipt-mismatch"


@dataclass(frozen=True, slots=True)
class FailedCandidateAttempt:
    """One planned delivery that did not yield a proved Perlectio."""

    identity: ResolvedIdentity
    opaque_act_id: str
    condition: Condition
    prompt_format_sha256: str
    dossier_sha256: str
    delivery_sha256: str
    kind: FailedAttemptKind
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ResolvedIdentity):
            raise MatrixRefusal("failed attempt needs a checked candidate identity")
        if not isinstance(self.opaque_act_id, str) or not self.opaque_act_id:
            raise MatrixRefusal("failed attempt needs an opaque act ID")
        if not isinstance(self.condition, Condition):
            raise MatrixRefusal("failed attempt needs a declared condition")
        if not isinstance(self.kind, FailedAttemptKind):
            raise MatrixRefusal("failed attempt needs a named failure kind")
        if not isinstance(self.detail, str) or not self.detail:
            raise MatrixRefusal("failed attempt needs a private diagnostic")
        if not all(
            is_sha256(value)
            for value in (
                self.prompt_format_sha256,
                self.dossier_sha256,
                self.delivery_sha256,
            )
        ):
            raise MatrixRefusal("failed attempt needs exact prompt, dossier, and delivery digests")


@dataclass(frozen=True, slots=True)
class AggregateMetrics:
    """Micro-aggregate metrics: align per act, then sum operations/denominators."""

    cell_count: int
    cer_errors: int
    cer_reference_units: int
    cer_matches: int
    wer_errors: int
    wer_reference_units: int
    wer_matches: int
    complete_count: int
    truncated_count: int
    no_readable_text_count: int
    refused_count: int
    missing_count: int
    unavailable_count: int
    malformed_count: int
    dissent_compared: int
    dissent_departed: int
    dissent_unavailable: int
    elapsed_observed_count: int
    elapsed_total_ms: float
    cost_observed_count: int
    cost_total_usd: float

    def __post_init__(self) -> None:
        counts = (
            self.cell_count,
            self.cer_errors,
            self.cer_reference_units,
            self.cer_matches,
            self.wer_errors,
            self.wer_reference_units,
            self.wer_matches,
            self.complete_count,
            self.truncated_count,
            self.no_readable_text_count,
            self.refused_count,
            self.missing_count,
            self.unavailable_count,
            self.malformed_count,
            self.dissent_compared,
            self.dissent_departed,
            self.dissent_unavailable,
            self.elapsed_observed_count,
            self.cost_observed_count,
        )
        if min(counts) < 0:
            raise MatrixRefusal("aggregate counts may not be negative")
        if bool(self.cer_reference_units) is not bool(self.wer_reference_units):
            raise MatrixRefusal("CER and WER checked denominators must be absent together")
        if not self.cer_reference_units and any(
            (self.cer_errors, self.cer_matches, self.wer_errors, self.wer_matches)
        ):
            raise MatrixRefusal("an unscored aggregate carries CER or WER operations")
        if self.dissent_departed > self.dissent_compared:
            raise MatrixRefusal("aggregate dissent departures exceed comparisons")
        if (
            self.complete_count
            + self.truncated_count
            + self.no_readable_text_count
            + self.refused_count
            + self.missing_count
            + self.unavailable_count
            + self.malformed_count
            != self.cell_count
        ):
            raise MatrixRefusal("response states do not account for all planned cells")
        if (
            self.elapsed_observed_count > self.cell_count
            or self.cost_observed_count > self.cell_count
        ):
            raise MatrixRefusal("metric observation counts may not exceed planned cells")
        if not all(
            value >= 0 and isfinite(value) for value in (self.elapsed_total_ms, self.cost_total_usd)
        ):
            raise MatrixRefusal("elapsed and cost totals must be finite and non-negative")

    @property
    def cer(self) -> float | None:
        return self.cer_errors / self.cer_reference_units if self.cer_reference_units else None

    @property
    def wer(self) -> float | None:
        return self.wer_errors / self.wer_reference_units if self.wer_reference_units else None

    @property
    def completeness(self) -> float | None:
        return self.cer_matches / self.cer_reference_units if self.cer_reference_units else None

    @property
    def dissent_rate(self) -> float | None:
        return self.dissent_departed / self.dissent_compared if self.dissent_compared else None

    @property
    def mean_elapsed_ms(self) -> float | None:
        if not self.elapsed_observed_count:
            return None
        return self.elapsed_total_ms / self.elapsed_observed_count

    @property
    def mean_cost_usd(self) -> float | None:
        if not self.cost_observed_count:
            return None
        return self.cost_total_usd / self.cost_observed_count


@dataclass(frozen=True, slots=True)
class ConditionAggregate:
    """Aggregate evidence for one non-identifying candidate slot and condition."""

    public_slot: int
    condition: Condition
    metrics: AggregateMetrics


@dataclass(frozen=True, slots=True)
class WitnessAggregate:
    """Aggregate direct baseline evidence for one non-identifying witness index."""

    public_source_index: int
    metrics: AggregateMetrics


@dataclass(frozen=True, slots=True)
class CandidateConditionDeltas:
    """Fixed within-candidate evidence; the session interprets without choosing text."""

    public_slot: int
    priming_cer_delta: float
    priming_wer_delta: float
    image_cer_delta: float
    image_wer_delta: float


@dataclass(frozen=True, slots=True)
class PairwiseConditionDeltas:
    """Fixed comparative values, not a ranking or automatic chair decision."""

    compared_public_slot: int
    base_public_slot: int
    nuda_cer_advantage: float
    primed_cer_advantage: float
    image_absent_cer_advantage: float
    witness_only_cer_advantage: float


@dataclass(frozen=True, slots=True)
class MeasurementRun:
    """A complete private evidence matrix plus independently scored witness baselines."""

    profile: NormalizationProfile
    candidates: tuple[ResolvedIdentity, ...]
    acts: tuple[EvaluationAct, ...]
    cells: tuple[CandidateCell, ...]
    witness_baselines: tuple[WitnessBaseline, ...]
    material_class: MaterialClass
    failed_attempts: tuple[FailedCandidateAttempt, ...] = ()
    roster: CandidateRoster | None = None
    witness_configuration: WitnessConfiguration | None = None
    manifest: EvaluationManifest | None = None
    sample_accounting: PrivateSampleAccounting | None = None
    limitations: RunLimitations = field(default_factory=RunLimitations)

    def __post_init__(self) -> None:
        if not self.candidates or not self.acts:
            raise MatrixRefusal("a measurement run requires candidates and acts")
        if not isinstance(self.material_class, MaterialClass):
            raise MatrixRefusal("measurement run needs an explicit material class")
        if not isinstance(self.limitations, RunLimitations):
            raise MatrixRefusal("measurement run needs a closed run-limitation declaration")
        if self.roster is not None:
            self.roster.validate()
            if self.candidates != self.roster.identities():
                raise MatrixRefusal(
                    "declared run identities differ from its sealed candidate roster"
                )
            if self.witness_configuration is None or self.manifest is None:
                raise MatrixRefusal(
                    "a declared roster run requires sealed witness configuration and manifest"
                )
            if self.sample_accounting is None:
                raise MatrixRefusal("a declared roster run requires private sample accounting")
        candidate_keys = [identity.candidate_key for identity in self.candidates]
        slots = [identity.public_slot for identity in self.candidates]
        act_ids = [act.opaque_act_id for act in self.acts]
        if len(set(candidate_keys)) != len(candidate_keys) or len(set(slots)) != len(slots):
            raise MatrixRefusal("a measurement run has duplicate candidate identities or slots")
        if len(set(act_ids)) != len(act_ids):
            raise MatrixRefusal("a measurement run has duplicate opaque act IDs")
        witness_signature = tuple(
            (item.private_source_id, item.public_source_index) for item in self.acts[0].testimonia
        )
        for act in self.acts:
            actual_signature = tuple(
                (item.private_source_id, item.public_source_index) for item in act.testimonia
            )
            if actual_signature != witness_signature:
                raise MatrixRefusal(
                    "every act must carry the same sealed Testimonium sources in the same order"
                )
            if self.witness_configuration is not None:
                self.witness_configuration.require_act(act)
        if self.manifest is not None:
            if self.sample_accounting is None:
                raise MatrixRefusal("a manifest-bound run requires private sample accounting")
            self.sample_accounting.require_complete_for(self.manifest)
            self.manifest.require_run_acts(act.manifest_binding() for act in self.acts)
        if self.witness_configuration is not None:
            self.witness_configuration.require_distinct_from_candidates(self.candidates)
        acts_by_id = {act.opaque_act_id: act for act in self.acts}
        identities_by_key = {identity.candidate_key: identity for identity in self.candidates}
        for cell in self.cells:
            if cell.opaque_act_id != cell.perlectio.opaque_act_id:
                raise MatrixRefusal("candidate cell and Perlectio name different acts")
            identity = identities_by_key.get(cell.perlectio.identity.candidate_key)
            if identity is None or cell.perlectio.identity != identity:
                raise MatrixRefusal(
                    "candidate cell carries a resolved identity different from its run"
                )
            act = acts_by_id.get(cell.opaque_act_id)
            if act is None:
                raise MatrixRefusal("candidate cell names an act outside its run")
            dossier = dossier_for(act, cell.perlectio.condition)
            if cell.perlectio.dossier_sha256 != dossier.wire_sha256:
                raise MatrixRefusal("candidate cell does not bind the run's exact dossier")
            if cell.perlectio.image_present is not (
                dossier.image is not None
            ) or cell.perlectio.testimonia_count != len(dossier.testimonia):
                raise MatrixRefusal("Perlectio input counts differ from its retained dossier")
            if cell.perlectio.status in (OutputStatus.COMPLETE, OutputStatus.TRUNCATED):
                if cell.perlectio.text != cell.raw_response_text:
                    raise MatrixRefusal("Perlectio text differs from its retained raw response")
            elif cell.raw_response_text is not None:
                # Checked on raw_response_text, not against Perlectio.text: that is
                # already None for a non-reading status, so comparing the two here
                # would pass whatever stray text the cell retained.
                raise MatrixRefusal("a non-reading cell retains stray raw response text")
            response = CandidateResponse(
                status=cell.perlectio.status,
                text=cell.raw_response_text,
                elapsed_ms=cell.perlectio.elapsed_ms,
                cost_usd=cell.perlectio.cost_usd,
                observed_prompt_sha256=cell.perlectio.prompt_format_sha256,
                observed_dossier_sha256=cell.perlectio.dossier_sha256,
                observed_delivery_sha256=cell.perlectio.delivery_sha256,
            )
            if cell.perlectio.dissent != _dissent_for(
                response, anonymous_testimonia(act), self.profile
            ):
                raise MatrixRefusal("Perlectio dissent differs from the retained witness evidence")
            expected_score = _score_for_act(
                act,
                status=cell.perlectio.status,
                text=cell.raw_response_text,
                profile=self.profile,
            )
            if cell.score != expected_score:
                raise MatrixRefusal("candidate cell score differs from its retained evidence")
        expected = {
            (identity.candidate_key, act.opaque_act_id, condition)
            for identity in self.candidates
            for act in self.acts
            for condition in ALL_CONDITIONS
        }
        completed = {
            (
                cell.perlectio.identity.candidate_key,
                cell.opaque_act_id,
                cell.perlectio.condition,
            )
            for cell in self.cells
        }
        failed = {
            (item.identity.candidate_key, item.opaque_act_id, item.condition)
            for item in self.failed_attempts
        }
        if completed & failed:
            raise MatrixRefusal("a planned cell is both a Perlectio and a failed attempt")
        if completed | failed != expected or len(self.cells) + len(self.failed_attempts) != len(
            expected
        ):
            raise MatrixRefusal(
                "candidate matrix does not account for every planned read exactly once"
            )
        for failure in self.failed_attempts:
            act = acts_by_id.get(failure.opaque_act_id)
            identity = identities_by_key.get(failure.identity.candidate_key)
            if act is None or identity != failure.identity:
                raise MatrixRefusal("failed attempt names evidence outside its run")
            dossier = dossier_for(act, failure.condition)
            if failure.dossier_sha256 != dossier.wire_sha256:
                raise MatrixRefusal("failed attempt does not bind the run's exact dossier")
        expected_baselines = {
            (act.opaque_act_id, item.public_source_index)
            for act in self.acts
            for item in act.testimonia
        }
        actual_baselines = {
            (baseline.opaque_act_id, baseline.public_source_index)
            for baseline in self.witness_baselines
        }
        if actual_baselines != expected_baselines or len(self.witness_baselines) != len(
            expected_baselines
        ):
            raise MatrixRefusal("every Testimonium baseline must be accounted for exactly once")
        for baseline in self.witness_baselines:
            act = acts_by_id[baseline.opaque_act_id]
            testimonium = next(
                item
                for item in act.testimonia
                if item.public_source_index == baseline.public_source_index
            )
            expected_score = _score_for_act(
                act,
                status=testimonium.status,
                text=testimonium.text,
                profile=self.profile,
            )
            if baseline.status is not testimonium.status or baseline.score != expected_score:
                raise MatrixRefusal(
                    "Testimonium baseline differs from the retained witness evidence"
                )

    def derived_limitation_codes(self) -> frozenset[PublicLimitationCode]:
        """Derive the closed public limitation set from retained run evidence.

        A failed attempt yielded no proved Perlectio, so it is a candidate non-answer
        for public limitation purposes even though it separately makes the run
        unpublishable. An ``INVALID_RESPONSE`` failure is additionally malformed:
        the adapter returned a value, but not a response shape this instrument can
        measure. A truncated cell is a reading that stopped early rather than a
        non-answer, so it carries its own code. No caller declaration participates
        in any of these classifications.
        """

        codes: set[PublicLimitationCode] = set()
        if any(act.ground_truth.gaps for act in self.acts):
            codes.add(PublicLimitationCode.CHECKED_REFERENCE_GAPS_PRESENT)
        if any(act.ground_truth.status is not ReferenceStatus.CHECKED for act in self.acts):
            codes.add(PublicLimitationCode.NONSCOREABLE_SELECTED_ACTS_PRESENT)
        if self.failed_attempts or any(
            cell.perlectio.status
            in (
                OutputStatus.NO_READABLE_TEXT,
                OutputStatus.REFUSED,
                OutputStatus.MISSING,
                OutputStatus.UNAVAILABLE,
            )
            for cell in self.cells
        ):
            codes.add(PublicLimitationCode.CANDIDATE_NONANSWERS_PRESENT)
        if any(cell.perlectio.status is OutputStatus.MALFORMED for cell in self.cells) or any(
            failure.kind is FailedAttemptKind.INVALID_RESPONSE for failure in self.failed_attempts
        ):
            codes.add(PublicLimitationCode.MALFORMED_CANDIDATE_RESPONSES_PRESENT)
        if any(cell.perlectio.status is OutputStatus.TRUNCATED for cell in self.cells):
            codes.add(PublicLimitationCode.TRUNCATED_READINGS_PRESENT)
        return frozenset(codes)

    def require_publishable(self) -> None:
        """Refuse fixture/partial evidence before the public redaction boundary."""

        if self.limitations.disclosure_state is LimitationDisclosureState.UNPUBLISHABLE:
            raise MatrixRefusal(
                "a run with a limitation outside the closed public vocabulary cannot publish"
            )
        if self.failed_attempts:
            raise MatrixRefusal("a run with unproved or invalid candidate attempts cannot publish")
        if any(
            not testimonium.delivery_confirmed
            for act in self.acts
            for testimonium in act.testimonia
        ):
            raise MatrixRefusal("a run with unconfirmed Attestator deliveries cannot publish")
        if not any(act.ground_truth.status is ReferenceStatus.CHECKED for act in self.acts):
            raise MatrixRefusal(
                "a publishable run requires at least one checked CER/WER denominator"
            )
        if self.material_class is MaterialClass.SYNTHETIC:
            raise MatrixRefusal("a synthetic fixture run cannot become a public finding")
        if self.roster is None or self.witness_configuration is None or self.manifest is None:
            raise MatrixRefusal("only a sealed declared-roster run can become a public finding")
        if self.sample_accounting is None:
            raise MatrixRefusal("only a declared run with private sample accounting can publish")
        derived_codes = self.derived_limitation_codes()
        declared_codes = frozenset(self.limitations.codes)
        if self.limitations.disclosure_state is LimitationDisclosureState.CLEAR:
            if derived_codes:
                derived = ", ".join(sorted(code.value for code in derived_codes))
                raise MatrixRefusal(
                    "a clear run limitation declaration does not match retained evidence; "
                    f"derived codes: {derived}"
                )
        elif declared_codes != derived_codes:
            omitted = derived_codes - declared_codes
            unsupported = declared_codes - derived_codes
            differences = []
            if omitted:
                differences.append("omitted: " + ", ".join(sorted(code.value for code in omitted)))
            if unsupported:
                differences.append(
                    "unsupported: " + ", ".join(sorted(code.value for code in unsupported))
                )
            raise MatrixRefusal(
                "run limitation declaration does not match retained evidence ("
                + "; ".join(differences)
                + ")"
            )
        # A `malformed` cell is a predeclared response state (README section 7),
        # and it carries no wall time or cost because there was no measurable
        # response to time. Requiring them of it refused the whole run with a
        # message about timers, sending the reader after a broken clock when the
        # real cause was one unreadable model answer.
        unmeasured = [
            cell
            for cell in self.cells
            if cell.perlectio.status is not OutputStatus.MALFORMED
            and (cell.perlectio.elapsed_ms is None or cell.perlectio.cost_usd is None)
        ]
        if unmeasured:
            first = unmeasured[0]
            # Name which of the two is actually missing. "reported neither" was
            # wrong whenever an adapter recorded wall time but no cost, and it
            # sent the reader looking for the wrong absent measurement.
            absent = " and ".join(
                name
                for name, value in (
                    ("wall time", first.perlectio.elapsed_ms),
                    ("cost", first.perlectio.cost_usd),
                )
                if value is None
            )
            raise MatrixRefusal(
                "a publishable run must measure wall time and cost for every candidate cell; "
                f"{len(unmeasured)} cell(s) did not, the first being public slot "
                f"{first.perlectio.identity.public_slot} on "
                f"{first.perlectio.condition.value}, which reported no {absent}"
            )
        self.roster.validate()

    def condition_aggregates(self) -> tuple[ConditionAggregate, ...]:
        groups: dict[tuple[int, Condition], list[CandidateCell]] = defaultdict(list)
        for cell in self.cells:
            groups[(cell.perlectio.identity.public_slot, cell.perlectio.condition)].append(cell)
        return tuple(
            ConditionAggregate(slot, condition, _aggregate_candidates(cells))
            for (slot, condition), cells in sorted(
                groups.items(), key=lambda item: (item[0][0], item[0][1])
            )
        )

    def witness_aggregates(self) -> tuple[WitnessAggregate, ...]:
        groups: dict[int, list[WitnessBaseline]] = defaultdict(list)
        for baseline in self.witness_baselines:
            groups[baseline.public_source_index].append(baseline)
        return tuple(
            WitnessAggregate(index, _aggregate_witnesses(rows))
            for index, rows in sorted(groups.items())
        )

    def condition_deltas(self) -> tuple[CandidateConditionDeltas, ...]:
        aggregates = self.condition_aggregates()
        grouped = _condition_index(aggregates)
        values: list[CandidateConditionDeltas] = []
        # Every planned candidate, not every candidate that produced an aggregate.
        # A candidate whose reads *all* failed contributes no aggregate at all, so
        # deriving the slots from `aggregates` dropped it from the deltas in
        # silence rather than refusing — the same loss the refusal below exists to
        # prevent, one level up.
        for slot in sorted(identity.public_slot for identity in self.candidates):
            # A condition whose every planned read became a failed attempt
            # contributes no cells, so `condition_aggregates` emits no group for
            # it at all and the subscript below would raise a bare `KeyError`
            # out of a method whose callers hold on `MatrixRefusal`.
            missing = [
                condition for condition in ALL_CONDITIONS if (slot, condition) not in grouped
            ]
            if missing:
                raise MatrixRefusal(
                    f"condition deltas need every condition for public slot {slot}; this "
                    f"matrix is missing {', '.join(sorted(item.value for item in missing))}"
                )
            nuda = grouped[(slot, Condition.LECTIO_NUDA)].metrics
            primed = grouped[(slot, Condition.WITNESS_PRIMED)].metrics
            image_absent = grouped[(slot, Condition.IMAGE_ABSENT_CONTROL)].metrics
            if any(
                value is None
                for value in (
                    nuda.cer,
                    nuda.wer,
                    primed.cer,
                    primed.wer,
                    image_absent.cer,
                    image_absent.wer,
                )
            ):
                raise MatrixRefusal("condition deltas require checked CER and WER denominators")
            values.append(
                CandidateConditionDeltas(
                    public_slot=slot,
                    priming_cer_delta=nuda.cer - primed.cer,
                    priming_wer_delta=nuda.wer - primed.wer,
                    image_cer_delta=image_absent.cer - primed.cer,
                    image_wer_delta=image_absent.wer - primed.wer,
                )
            )
        return tuple(values)

    def compare_to_base(
        self, *, compared_public_slot: int, base_public_slot: int
    ) -> PairwiseConditionDeltas:
        """Expose the predeclared checkpoint-vs-base evidence without making a decision."""

        if compared_public_slot == base_public_slot:
            raise MatrixRefusal("a comparative delta requires two different candidate slots")
        grouped = _condition_index(self.condition_aggregates())
        # Read every operand before subtracting any of them. An unscored
        # aggregate's `cer` is `None`, and `None - None` raises `TypeError`,
        # which the `KeyError` handler below does not catch — so subtracting
        # first put this method's own denominator refusal out of reach.
        try:
            operands = {
                condition: (
                    grouped[(base_public_slot, condition)].metrics.cer,
                    grouped[(compared_public_slot, condition)].metrics.cer,
                )
                for condition in ALL_CONDITIONS
            }
        except KeyError as error:
            raise MatrixRefusal(
                "comparison names a candidate slot not present in this full matrix"
            ) from error
        if any(value is None for pair in operands.values() for value in pair):
            raise MatrixRefusal("pairwise deltas require checked CER denominators")
        nuda = operands[Condition.LECTIO_NUDA][0] - operands[Condition.LECTIO_NUDA][1]
        primed = operands[Condition.WITNESS_PRIMED][0] - operands[Condition.WITNESS_PRIMED][1]
        image_absent = (
            operands[Condition.IMAGE_ABSENT_CONTROL][0]
            - operands[Condition.IMAGE_ABSENT_CONTROL][1]
        )
        return PairwiseConditionDeltas(
            compared_public_slot=compared_public_slot,
            base_public_slot=base_public_slot,
            nuda_cer_advantage=nuda,
            primed_cer_advantage=primed,
            image_absent_cer_advantage=image_absent,
            witness_only_cer_advantage=primed - nuda,
        )


def _condition_index(
    aggregates: Iterable[ConditionAggregate],
) -> dict[tuple[int, Condition], ConditionAggregate]:
    return {(aggregate.public_slot, aggregate.condition): aggregate for aggregate in aggregates}


def _aggregate_candidates(cells: Iterable[CandidateCell]) -> AggregateMetrics:
    return _aggregate_rows(
        (
            cell.perlectio.status,
            cell.score,
            cell.perlectio.dissent,
            cell.perlectio.elapsed_ms,
            cell.perlectio.cost_usd,
        )
        for cell in cells
    )


def _aggregate_witnesses(rows: Iterable[WitnessBaseline]) -> AggregateMetrics:
    # Testimonia do not have a dissent comparison or cost/elapsed observation in this
    # instrument.  They remain direct accuracy baselines only.
    return _aggregate_rows(
        (row.status, row.score, DissentSummary(0, 0, 0), None, None) for row in rows
    )


def _aggregate_rows(
    rows: Iterable[
        tuple[OutputStatus, ActScore | None, DissentSummary, float | None, float | None]
    ],
) -> AggregateMetrics:
    values = tuple(rows)
    if not values:
        raise MatrixRefusal("cannot aggregate no rows")
    statuses = [value[0] for value in values]
    scores = [value[1] for value in values if value[1] is not None]
    dissents = [value[2] for value in values]
    elapsed = [value[3] for value in values if value[3] is not None]
    costs = [value[4] for value in values if value[4] is not None]
    return AggregateMetrics(
        cell_count=len(values),
        cer_errors=sum(score.cer.edits.errors for score in scores),
        cer_reference_units=sum(score.cer.reference_units for score in scores),
        cer_matches=sum(score.cer.edits.matches for score in scores),
        wer_errors=sum(score.wer.edits.errors for score in scores),
        wer_reference_units=sum(score.wer.reference_units for score in scores),
        wer_matches=sum(score.wer.edits.matches for score in scores),
        complete_count=sum(status is OutputStatus.COMPLETE for status in statuses),
        truncated_count=sum(status is OutputStatus.TRUNCATED for status in statuses),
        no_readable_text_count=sum(status is OutputStatus.NO_READABLE_TEXT for status in statuses),
        refused_count=sum(status is OutputStatus.REFUSED for status in statuses),
        missing_count=sum(status is OutputStatus.MISSING for status in statuses),
        unavailable_count=sum(status is OutputStatus.UNAVAILABLE for status in statuses),
        malformed_count=sum(status is OutputStatus.MALFORMED for status in statuses),
        dissent_compared=sum(item.compared for item in dissents),
        dissent_departed=sum(item.departed for item in dissents),
        dissent_unavailable=sum(item.unavailable for item in dissents),
        elapsed_observed_count=len(elapsed),
        elapsed_total_ms=fsum(elapsed),
        cost_observed_count=len(costs),
        cost_total_usd=fsum(costs),
    )


def _preflight_act(
    act: EvaluationAct, profile: NormalizationProfile, *, require_human_adjudication: bool
) -> None:
    """Validate an act without using its reference status to end its life."""

    if act.ground_truth.status is ReferenceStatus.CHECKED:
        # The gap-excised text, not the raw text: an act whose readable ink
        # normalizes away has no valid CER/WER denominator. It is still invalid
        # as a checked reference; blank and unresolved references take the
        # separate, unscored path below.
        if not isinstance(act.ground_truth.text, str) or not normalize_text(
            act.ground_truth.scoreable_text, profile
        ):
            raise MatrixRefusal("evaluation act has no scoreable checked text after normalization")
    if (
        require_human_adjudication
        and act.ground_truth.status is ReferenceStatus.CHECKED
        and len(act.ground_truth.independent_draft_sha256s) != 2
    ):
        raise MatrixRefusal(
            "declared run reference lacks two independent transcription drafts before adjudication"
        )
    if not act.image.payload:
        raise MatrixRefusal("evaluation act has no image payload")
    if not act.testimonia:
        raise MatrixRefusal("evaluation act has no Testimonia")


def _score_for_act(
    act: EvaluationAct,
    *,
    status: OutputStatus,
    text: str | None,
    profile: NormalizationProfile,
) -> ActScore | None:
    """Score only against checked ink; every other reference still gets read."""

    if act.ground_truth.status is not ReferenceStatus.CHECKED:
        return None
    return score_response(
        act.ground_truth.scoreable_text,
        status=status,
        text=text,
        profile=profile,
    )


def _failed_attempt(
    identity: ResolvedIdentity,
    *,
    dossier,
    request,
    kind: FailedAttemptKind,
    detail: str,
) -> FailedCandidateAttempt:
    return FailedCandidateAttempt(
        identity=identity,
        opaque_act_id=dossier.opaque_act_id,
        condition=dossier.condition,
        prompt_format_sha256=request.prompt_format_sha256,
        dossier_sha256=dossier.wire_sha256,
        delivery_sha256=request.delivery_sha256,
        kind=kind,
        detail=detail,
    )


def _dissent_for(
    response: CandidateResponse,
    testimonia: tuple[DossierTestimonium, ...],
    profile: NormalizationProfile,
) -> DissentSummary:
    """Compare after a candidate text is fixed; no witness can influence that text.

    A cell with no reading in it has no dissent, which is the shape
    `pipeline/4_perlector/run.py` already settles for the real stage. Recording a
    refusal as departure from every witness would be true as a string comparison
    and backwards as a measure: dissent is the parroting instrument, so a
    candidate that refused every act would score as maximally independent. The
    refusal is not lost -- it is counted in this cell's response state.
    """

    if not testimonia:
        return DissentSummary(compared=0, departed=0, unavailable=0)
    if response.status not in (OutputStatus.COMPLETE, OutputStatus.TRUNCATED):
        return DissentSummary(compared=0, departed=0, unavailable=0)
    hypothesis = normalize_text(hypothesis_for_status(response.status, response.text), profile)
    compared = 0
    departed = 0
    unavailable = 0
    for testimonium in testimonia:
        if testimonium.status not in (OutputStatus.COMPLETE, OutputStatus.TRUNCATED):
            unavailable += 1
            continue
        compared += 1
        if normalize_text(testimonium.text or "", profile) != hypothesis:
            departed += 1
    return DissentSummary(compared=compared, departed=departed, unavailable=unavailable)


def _perlectio_for(
    identity: ResolvedIdentity,
    response: CandidateResponse,
    *,
    dossier,
    comparison_testimonia: tuple[DossierTestimonium, ...],
    delivery_sha256: str,
    profile: NormalizationProfile,
) -> Perlectio:
    # Lectio nuda sees no Testimonia in its dossier, but its reading is still
    # compared against them afterwards: without that, the nuda/primed dissent
    # instrument has no baseline.
    dissent = _dissent_for(response, comparison_testimonia, profile)
    return Perlectio(
        identity=identity,
        opaque_act_id=dossier.opaque_act_id,
        condition=dossier.condition,
        status=response.status,
        text=(
            response.text
            if response.status in (OutputStatus.COMPLETE, OutputStatus.TRUNCATED)
            else None
        ),
        dossier_sha256=dossier.wire_sha256,
        prompt_format_sha256=response.observed_prompt_sha256,
        delivery_sha256=delivery_sha256,
        image_present=dossier.image is not None,
        testimonia_count=len(dossier.testimonia),
        dissent=dissent,
        elapsed_ms=float(response.elapsed_ms) if response.elapsed_ms is not None else None,
        cost_usd=float(response.cost_usd) if response.cost_usd is not None else None,
    )


def _preflight_witness_configuration(
    acts: tuple[EvaluationAct, ...], configuration: WitnessConfiguration | None
) -> None:
    """Require equal witness denominators before any candidate receives a dossier."""

    first = tuple((item.private_source_id, item.public_source_index) for item in acts[0].testimonia)
    for act in acts:
        actual = tuple(
            (item.private_source_id, item.public_source_index) for item in act.testimonia
        )
        if actual != first:
            raise MatrixRefusal(
                "every act must retain the same Testimonium sources in sealed order; "
                "a missing witness needs an unavailable stub"
            )
        if configuration is not None:
            configuration.require_act(act)


def _execute_matrix(
    participants: Iterable[tuple[Candidate, ResolvedIdentity]],
    acts: Iterable[EvaluationAct],
    *,
    prompt_registry: PromptRegistry,
    profile: NormalizationProfile,
    authorization: RunAuthorization,
    roster: CandidateRoster | None,
    witness_configuration: WitnessConfiguration | None,
    manifest: EvaluationManifest | None,
    sample_accounting: PrivateSampleAccounting | None,
    limitations: RunLimitations,
) -> MeasurementRun:
    """Preflight the entire matrix, then call each already-frozen participant."""

    participant_values = tuple(participants)
    act_values = tuple(acts)
    if not participant_values or not act_values:
        raise MatrixRefusal("matrix requires at least one candidate and one act")
    if any(not isinstance(candidate, Candidate) for candidate, _ in participant_values):
        raise MatrixRefusal("every matrix participant must implement the Candidate protocol")
    identities = tuple(identity for _, identity in participant_values)
    for identity in identities:
        validate_perlector_candidate(identity)
    candidate_keys = [identity.candidate_key for identity in identities]
    candidate_slots = [identity.public_slot for identity in identities]
    if len(set(candidate_keys)) != len(candidate_keys) or len(set(candidate_slots)) != len(
        candidate_slots
    ):
        raise MatrixRefusal("matrix candidates must have distinct resolved identities and slots")
    act_ids = [act.opaque_act_id for act in act_values]
    if len(set(act_ids)) != len(act_ids):
        raise MatrixRefusal("matrix acts must have distinct opaque IDs")
    for act in act_values:
        _preflight_act(
            act,
            profile,
            require_human_adjudication=(
                roster is not None and authorization.material_class is not MaterialClass.SYNTHETIC
            ),
        )
    _preflight_witness_configuration(act_values, witness_configuration)
    if witness_configuration is not None:
        witness_configuration.require_distinct_from_candidates(identities)
    if manifest is not None:
        if sample_accounting is None:
            raise MatrixRefusal("a manifest-bound matrix requires private sample accounting")
        sample_accounting.require_complete_for(manifest)
        manifest.require_run_acts(act.manifest_binding() for act in act_values)
    require_authorized_delivery(
        identities,
        act_values,
        authorization,
        manifest=manifest,
    )

    prepared: dict[tuple[str, Condition, str], tuple[object, object]] = {}
    for act in act_values:
        for condition in ALL_CONDITIONS:
            dossier = dossier_for(act, condition)
            for identity in identities:
                # The closed prompt registry resolves before a single read call.
                request = prompt_registry.request_for(identity, dossier)
                prepared[(act.opaque_act_id, condition, identity.candidate_key)] = (
                    dossier,
                    request,
                )

    cells: list[CandidateCell] = []
    failed_attempts: list[FailedCandidateAttempt] = []
    for act in act_values:
        comparison_testimonia = anonymous_testimonia(act)
        for condition in ALL_CONDITIONS:
            for candidate, identity in participant_values:
                dossier, request = prepared[(act.opaque_act_id, condition, identity.candidate_key)]
                try:
                    response = candidate.read(request)
                except Exception as error:
                    if type(error) is MeasurementRefusal:
                        # The adapter received its delivery and produced a
                        # response; that response is simply unmeasurable (too
                        # long, an unpaired surrogate, an excessive
                        # combining-mark run). README section 7 predeclares
                        # `malformed` as a named response state for exactly
                        # this -- it is scored, not a reason to discard every
                        # cell already paid for.
                        response = CandidateResponse(
                            status=OutputStatus.MALFORMED,
                            text=None,
                            elapsed_ms=None,
                            cost_usd=None,
                            observed_prompt_sha256=request.prompt_format_sha256,
                            observed_dossier_sha256=dossier.wire_sha256,
                            observed_delivery_sha256=request.delivery_sha256,
                        )
                    else:
                        failed_attempts.append(
                            _failed_attempt(
                                identity,
                                dossier=dossier,
                                request=request,
                                kind=FailedAttemptKind.ADAPTER_EXCEPTION,
                                detail=f"{type(error).__name__}: {error}",
                            )
                        )
                        continue
                if not isinstance(response, CandidateResponse):
                    failed_attempts.append(
                        _failed_attempt(
                            identity,
                            dossier=dossier,
                            request=request,
                            kind=FailedAttemptKind.INVALID_RESPONSE,
                            detail="candidate adapter did not return CandidateResponse",
                        )
                    )
                    continue
                if response.observed_prompt_sha256 != request.prompt_format_sha256:
                    failed_attempts.append(
                        _failed_attempt(
                            identity,
                            dossier=dossier,
                            request=request,
                            kind=FailedAttemptKind.PROMPT_RECEIPT_MISMATCH,
                            detail="adapter did not observe the declared prompt-format bytes",
                        )
                    )
                    continue
                if response.observed_dossier_sha256 != dossier.wire_sha256:
                    failed_attempts.append(
                        _failed_attempt(
                            identity,
                            dossier=dossier,
                            request=request,
                            kind=FailedAttemptKind.DOSSIER_RECEIPT_MISMATCH,
                            detail="adapter did not observe the exact common dossier bytes",
                        )
                    )
                    continue
                if response.observed_delivery_sha256 != request.delivery_sha256:
                    failed_attempts.append(
                        _failed_attempt(
                            identity,
                            dossier=dossier,
                            request=request,
                            kind=FailedAttemptKind.DELIVERY_RECEIPT_MISMATCH,
                            detail="adapter did not observe the sealed delivery envelope",
                        )
                    )
                    continue
                perlectio = _perlectio_for(
                    identity,
                    response,
                    dossier=dossier,
                    comparison_testimonia=comparison_testimonia,
                    delivery_sha256=request.delivery_sha256,
                    profile=profile,
                )
                cells.append(
                    CandidateCell(
                        opaque_act_id=act.opaque_act_id,
                        perlectio=perlectio,
                        raw_response_text=response.text,
                        score=_score_for_act(
                            act,
                            status=response.status,
                            text=response.text,
                            profile=profile,
                        ),
                    )
                )

    witness_baselines = tuple(
        WitnessBaseline(
            opaque_act_id=act.opaque_act_id,
            public_source_index=testimonium.public_source_index,
            status=testimonium.status,
            score=_score_for_act(
                act,
                status=testimonium.status,
                text=testimonium.text,
                profile=profile,
            ),
        )
        for act in act_values
        for testimonium in act.testimonia
    )
    return MeasurementRun(
        profile=profile,
        candidates=identities,
        acts=act_values,
        cells=tuple(cells),
        witness_baselines=witness_baselines,
        material_class=authorization.material_class,
        failed_attempts=tuple(failed_attempts),
        roster=roster,
        witness_configuration=witness_configuration,
        manifest=manifest,
        sample_accounting=sample_accounting,
        limitations=limitations,
    )


def run_matrix(
    candidates: Iterable[Candidate],
    acts: Iterable[EvaluationAct],
    *,
    prompt_registry: PromptRegistry,
    profile: NormalizationProfile,
    authorization: RunAuthorization,
    limitations: RunLimitations | None = None,
) -> MeasurementRun:
    """Synthetic-only exercise entry point for the fake candidate interface.

    A real claim may only enter through :func:`run_declared_roster_matrix`.
    Keeping this lightweight path is useful for scoring and interface tests, but
    its resulting run is explicitly non-publishable.
    """

    if authorization.material_class is not MaterialClass.SYNTHETIC:
        raise MatrixRefusal("real material must use the sealed declared-roster run entry point")
    return _execute_matrix(
        tuple((candidate, candidate.identity) for candidate in candidates),
        acts,
        prompt_registry=prompt_registry,
        profile=profile,
        authorization=authorization,
        roster=None,
        witness_configuration=None,
        manifest=None,
        sample_accounting=None,
        limitations=RunLimitations() if limitations is None else limitations,
    )


def run_declared_roster_matrix(
    candidates: Iterable[Candidate],
    acts: Iterable[EvaluationAct],
    *,
    roster: CandidateRoster,
    witness_configuration: WitnessConfiguration,
    manifest: EvaluationManifest,
    prompt_registry: PromptRegistry,
    profile: NormalizationProfile,
    authorization: RunAuthorization,
    sample_accounting: PrivateSampleAccounting | None = None,
    limitations: RunLimitations | None = None,
) -> MeasurementRun:
    """The only sealed entry point for a declared Spec 05 candidate matrix."""

    roster.validate()
    protocol_sha256 = require_predeclared_protocol()
    if manifest.protocol_sha256 != protocol_sha256:
        raise MatrixRefusal(
            "declared run manifest does not bind the exact predeclared Spec 05 protocol"
        )
    canonical_profile = require_canonical_profile(profile)
    accounting = (
        PrivateSampleAccounting.all_scoreable(manifest)
        if sample_accounting is None
        else sample_accounting
    )
    accounting.require_complete_for(manifest)
    authorization.require_declared_run_plan(
        protocol_sha256=manifest.protocol_sha256,
        manifest_sha256=manifest.manifest_sha256,
        candidate_roster_sha256=roster.digest,
        witness_configuration_sha256=witness_configuration.digest,
        prompt_registry_sha256=prompt_registry.snapshot_sha256(roster.identities()),
        normalization_profile_id=canonical_profile.profile_id,
        normalization_profile_sha256=canonical_profile.digest,
        private_sample_accounting_sha256=accounting.digest,
    )
    candidate_values = tuple(candidates)
    participants = tuple((candidate, candidate.identity) for candidate in candidate_values)
    # Typed before it is compared, because the dict equality below calls the
    # *left* operand's __eq__ first: a caller-supplied object gets first say on
    # whether it "is" the sealed roster identity that gates external delivery.
    if any(not isinstance(identity, ResolvedIdentity) for _, identity in participants):
        raise MatrixRefusal(
            "every matrix participant must declare a checked ResolvedIdentity, not a stand-in"
        )
    expected = {identity.candidate_key: identity for identity in roster.identities()}
    actual = {identity.candidate_key: identity for _, identity in participants}
    if actual != expected or len(candidate_values) != 3:
        raise MatrixRefusal(
            "real Spec 05 run must supply exactly the sealed three-candidate roster"
        )
    act_values = tuple(acts)
    manifest.require_run_acts(act.manifest_binding() for act in act_values)
    return _execute_matrix(
        participants,
        act_values,
        prompt_registry=prompt_registry,
        profile=canonical_profile,
        authorization=authorization,
        roster=roster,
        witness_configuration=witness_configuration,
        manifest=manifest,
        sample_accounting=accounting,
        limitations=RunLimitations() if limitations is None else limitations,
    )
