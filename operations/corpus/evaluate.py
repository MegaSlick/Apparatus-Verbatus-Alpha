"""Score what a sealed run actually exported against reference truth, denominator whole.

`compare.py` owns the join and the scoring: it pairs sealed proposal regions
with reference boxes by IoU and scores each pair with the sealed normaliser and
scorer. It deliberately takes the hypotheses -- `{act_id: (status, text)}` --
from its caller, and until this module there was no caller that built them from
a real run: the bench runner emits `not-run`, and a mathematically correct scorer
handed the wrong texts scores the wrong thing (independent audit of 2026-09-10,
measurement gap). This module is that caller, and it takes the texts from one
place only: the Armarium export the run itself sealed, each delivered text
re-digested against the Archetypus record that established it, so a score can
never be computed over a text the pipeline did not publish.

**The denominator is kept whole.** Every reference record ends in exactly one
row: matched and scored, missed on a page the run sealed, or not attempted
because the run never sealed its page. Every proposed act ends counted by its
export category -- delivered, held, refused, confirmed blank, excluded -- and a
held or missing act is scored as an empty hypothesis against its reference,
never omitted and never given a perfect score. An unmatched pipeline act is
reported, not scored, because RecordGold annotates records only and may not
have labelled what the pipeline found. Splits, merges and ambiguous overlaps
are what the IoU matrix shows: the full matrix travels in each page's
comparison record, so a reader can see which pairs were eligible and which the
assignment chose.

**Two aggregate rates, each labelled by what it counts.** The matched-pairs rate
is the arithmetic of the pairs the assignment made: a record the pipeline never
found contributes to neither side of that fraction, so failing to find an act
cannot improve it and cannot worsen it either. GOALS 1 says a missed act is
worse than a poorly read act, so the second rate counts every missed record's
reference units as deletions -- the same treatment a held act already gets --
and that is the number a capture failure actually moves. Neither rate counts a
not-attempted record: the run never sealed that page, so it is a gap in the
trial's coverage rather than a reading the pipeline got wrong, and
`reference_records_not_attempted` is where a reader sees it.

**What this report measures and what it only states.** The run's own facts come
from the sealed tree: the export digest, the run and config digests, and whether
this was a fixture run, which is read from the export payload's own identity
field (`fixture_id` on a fixture run, `submission_id` on a real one, never both,
never neither) rather than from a flag the operator could omit. `code_ref` is a
caller's declaration; it is checked against the commit this checkout has out
when there is one, and `code_ref_check` names what the check found rather than
implying one happened. A reference ledger, when passed, is verified: every
reference page's `self_hash` must appear in it.

**Outside the establishment path, by construction.** `operations/corpus/` may
not import `pipeline/` and nothing here writes a run tree; the reference text
is read here and only here, after the run is sealed, and through the read-only
wrapper that refuses every write outright. This module never trains, never
tunes, never selects a reading.

A report built over the synthetic fixture is labelled a fixture result, in the
record and in the summary a person reads. It proves the driver; it says nothing
about RecordGold reading quality.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    digest_of,
    is_sha256,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import ContractError
from common.contracts.stages import ARCHETYPUS
from common.runtree.store import RunTree
from common.stage import verify_final_seal
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import (
    GRAPHEMIC_V1,
    PROFILES,
    character_units,
    word_units,
)

from . import CorpusRefusal
from .compare import (
    ReadOnlyRunTree,
    compare_page,
    count_excluded_designator_artifacts,
    load_exemplar_page_shas,
    load_pipeline_proposal_acts,
)
from .local_admission import load_local_admission_ledger, validate_local_admission_ledger
from .reference import validate_reference_page

SCHEMA = "recordgold-evaluation.v1"
FIXTURE_LABEL = (
    "fixture result: scored over synthetic fixture pages and fixture model answers; "
    "this is a proof of the evaluation driver, not a RecordGold reading-quality claim"
)
LIVE_LABEL = (
    "scored over the run's own sealed export against third-party expert records "
    "(records-only completeness); accuracy of the model, not of the pipeline's accounting"
)

EVALUATION_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "no-export",
        "export-text-mismatch",
        "unknown-export-category",
        "reference-page-collision",
        "reference-page-invalid",
        "reference-page-not-in-ledger",
        "reference-ledger-invalid",
        "missing-input-file",
        "comparison-refused",
        "ambiguous-run-identity",
        "unestablished-delivered-act",
        "output-exists",
        "wrong-schema",
        "self-hash-mismatch",
    }
)

# The Armarium's terminal categories, each mapped to the scorer's response state.
# A delivered act is the only one whose text is scored; every other category is
# an empty hypothesis against checked ink -- counted, never dropped, never perfect.
_CATEGORY_STATUS: dict[str, OutputStatus] = {
    "delivered": OutputStatus.COMPLETE,
    "held-for-review": OutputStatus.UNAVAILABLE,
    "refused-with-reason": OutputStatus.REFUSED,
    "confirmed-blank": OutputStatus.NO_READABLE_TEXT,
    "excluded-with-approval": OutputStatus.MISSING,
}

# One normalisation profile for both halves of the missed-inclusive fraction and
# for `compare_page` itself, passed explicitly rather than left to two defaults
# that could drift apart (independent audit of 2026-09-11, round 2 item 15). The
# report records which one ran.
PROFILE = GRAPHEMIC_V1

_MATCHED_SCOPE = (
    "matched pairs only: a reference record the assignment never paired contributes to "
    "neither side of this fraction"
)
_MISSED_SCOPE = (
    "matched pairs plus every missed record counted as wholly deleted; not-attempted "
    "records are excluded from both rates and counted separately"
)

_EDIT_FIELDS = (
    "reference_units",
    "hypothesis_units",
    "matches",
    "substitutions",
    "insertions",
    "deletions",
)

_RATE_FIELDS = frozenset({"numerator", "denominator"})
_UNIT_FIELDS = frozenset(set(_EDIT_FIELDS) | {"rate"})
_AGGREGATE_SCOPE_FIELDS = frozenset({"scope", "cer", "wer"})
_AGGREGATE_FIELDS = frozenset(
    {"normalization_profile_id", "matched_pairs_only", "including_missed_records"}
)
_CODE_REF_CHECK_FIELDS = frozenset({"state", "checkout_head"})
# One closed row shape for all three outcomes. A missed or not-attempted record
# carries `None` where a scored one carries a measurement, so a reader is never
# left to infer which questions a row was even asked -- the same repair the
# admission ledger's rows needed (independent audit of 2026-09-11, finding 9).
_RECORD_ROW_FIELDS = frozenset(
    {
        "physical_act_id",
        "record_id",
        "page_ordinal",
        "page_sha256",
        "outcome",
        "pipeline_act_id",
        "export_category",
        "text_status",
        "status",
        "cer",
        "wer",
        "note",
    }
)
_RUN_FIELDS = frozenset(
    {
        "run_id",
        "config_digest",
        "sealed_config_digests",
        "export_sha256",
        "export_status",
        "scenario",
    }
)
_DENOMINATOR_FIELDS = frozenset(
    {
        "run_pages_sealed",
        "run_pages_compared",
        "run_pages_without_reference",
        "reference_pages_not_in_run",
        "proposal_regions",
        "proposed_acts",
        "exported_acts_by_category",
        "excluded_designator_artifacts",
        "reference_records_scored",
        "reference_records_scored_by_export_category",
        "reference_records_missed",
        "reference_records_not_attempted",
        "pipeline_acts_unmatched",
    }
)
_CORPUS_FIELDS = frozenset(
    {
        "reference_pages",
        "reference_records",
        "reference_ledger_sha256",
        "reference_ledger_verified",
        "reference_page_self_hashes",
        "splits",
    }
)
_TOP_FIELDS = frozenset(
    {
        "schema",
        "fixture",
        "label",
        "code_ref",
        "code_ref_check",
        "run",
        "corpus",
        "denominators",
        "aggregate",
        "pages",
        "records",
        "unmatched_pipeline_acts",
        "pages_without_reference",
        "self_hash",
    }
)


def hypotheses_from_export(
    export_payload: Mapping[str, Any], established_text_hashes: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """Every exported act's scoring hypothesis, bound to the text the run established.

    `established_text_hashes` is `{act_id: text_hash}` read from the Archetypus
    records of the same tree; a delivered text that does not re-digest to its
    record's hash is refused by name (`export-text-mismatch`), and a delivered
    act with no Archetypus record at all is refused too. The export's category
    travels beside the derived status so the report can count by what the
    pipeline said, not only by what the scorer did with it.
    """
    hypotheses: dict[str, dict[str, Any]] = {}
    for name in ("delivered", "non_delivered"):
        rows = export_payload.get(name)
        if not isinstance(rows, list):
            raise CorpusRefusal(f"malformed-record: the export has no {name} list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("act_id"), str):
                raise CorpusRefusal(f"malformed-record: an export {name} row has no act_id")
            act_id = row["act_id"]
            if act_id in hypotheses:
                raise CorpusRefusal(f"malformed-record: act {act_id!r} appears twice in the export")
            category = row.get("category")
            if category not in _CATEGORY_STATUS:
                raise CorpusRefusal(
                    f"unknown-export-category: act {act_id!r} carries {category!r}, not one of "
                    f"{sorted(_CATEGORY_STATUS)}"
                )
            status = _CATEGORY_STATUS[category]
            text = None
            text_status = row.get("text_status")
            if category == "delivered":
                text = row.get("text")
                if not isinstance(text, str):
                    raise CorpusRefusal(
                        f"malformed-record: delivered act {act_id!r} carries no literal text"
                    )
                expected = established_text_hashes.get(act_id)
                if expected is None:
                    raise CorpusRefusal(
                        f"unestablished-delivered-act: {act_id!r} is delivered but no Archetypus "
                        "record established it"
                    )
                # The Archetypus seals `text_hash = digest_of(text)`: the canonical
                # JSON digest of the string, not of its raw bytes.
                actual = digest_of(text)
                if actual != expected:
                    raise CorpusRefusal(
                        f"export-text-mismatch: delivered act {act_id!r} text digests to {actual}, "
                        f"its Archetypus record established {expected}; the export is not scored"
                    )
                if text_status == "no_readable_text":
                    status = OutputStatus.NO_READABLE_TEXT
            hypotheses[act_id] = {
                "status": status,
                "text": text,
                "category": category,
                "text_status": text_status,
                "reason": row.get("reason"),
            }
    return hypotheses


def _established_text_hashes(tree: ReadOnlyRunTree) -> dict[str, str]:
    """Every Archetypus record's `text_hash`, read with the lineage check left on.

    `verify_inputs=False` is for a caller whose own boundary owns lineage. This
    caller verifies the Armarium seal, not the Archetypus one, so it has no such
    boundary and leaves the default alone (independent audit of 2026-09-11,
    finding 13).
    """
    hashes: dict[str, str] = {}
    for entry in tree.build_manifest(ARCHETYPUS)["artifacts"]:
        if entry["kind"] != "archetypus":
            continue
        record = tree.read_artifact(ARCHETYPUS, "archetypus", entry["artifact_id"])
        text_hash = record.get("payload", {}).get("text_hash")
        if not is_sha256(text_hash):
            raise CorpusRefusal(
                f"malformed-record: Archetypus record {record['subject_id']!r} has no text_hash"
            )
        if record["subject_id"] in hashes:
            raise CorpusRefusal(
                f"malformed-record: two Archetypus records establish {record['subject_id']!r}"
            )
        hashes[record["subject_id"]] = text_hash
    return hashes


def _rate(edits: Mapping[str, int]) -> dict[str, int]:
    errors = edits["substitutions"] + edits["insertions"] + edits["deletions"]
    return {"numerator": errors, "denominator": edits["reference_units"]}


def _accumulate(total: dict[str, int], part: Mapping[str, int]) -> None:
    for key in _EDIT_FIELDS:
        total[key] = total.get(key, 0) + part[key]


def _wholly_deleted(units: int) -> dict[str, int]:
    """The edit counts of a reference nothing was proposed against."""
    return {
        "reference_units": units,
        "hypothesis_units": 0,
        "matches": 0,
        "substitutions": 0,
        "insertions": 0,
        "deletions": units,
    }


def _scope(scope: str, cer: Mapping[str, int], wer: Mapping[str, int]) -> dict[str, Any]:
    return {
        "scope": scope,
        "cer": {**cer, "rate": _rate(cer)} if cer.get("reference_units") else None,
        "wer": {**wer, "rate": _rate(wer)} if wer.get("reference_units") else None,
    }


def _is_commit(value: str) -> bool:
    return len(value) in (40, 64) and all(character in "0123456789abcdef" for character in value)


def _checkout_commit() -> str | None:
    """The commit this checkout has out, read from `.git` alone, or `None`.

    Stdlib only and no subprocess: a scoring module has no business shelling out.
    A worktree's `.git` is a file naming the real git directory, and a branch's
    tip may be loose or packed, so both routes are followed. Anything unexpected
    returns `None`, which the report records as "no checkout found" rather than
    as agreement.
    """
    for directory in Path(__file__).resolve().parents:
        marker = directory / ".git"
        if not marker.exists():
            continue
        git_dir = marker
        if marker.is_file():
            content = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not content.startswith("gitdir:"):
                return None
            git_dir = Path(content.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (directory / git_dir).resolve()
        head_file = git_dir / "HEAD"
        if not head_file.is_file():
            return None
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            # Detached HEAD: the commit itself. 40 hex for a sha1 repository, 64
            # for a sha256 one; anything else is not a commit and is not guessed at.
            return head if _is_commit(head) else None
        ref = head.split(":", 1)[1].strip()
        # A linked worktree's refs live in the common directory, which
        # `commondir` names relative to the worktree's own git directory.
        roots = [git_dir]
        commondir = git_dir / "commondir"
        if commondir.is_file():
            common = Path(commondir.read_text(encoding="utf-8", errors="replace").strip())
            roots.append(common if common.is_absolute() else (git_dir / common).resolve())
        for root in roots:
            loose = root / ref
            if loose.is_file():
                tip = loose.read_text(encoding="utf-8", errors="replace").strip()
                return tip if _is_commit(tip) else None
            packed = root / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref and _is_commit(parts[0]):
                        return parts[0]
        return None
    return None


def _code_ref_check(code_ref: str) -> dict[str, Any]:
    """What checking `code_ref` against this checkout actually found.

    A declaration is not a measurement, and the report says which this is
    (GOVERNANCE 10; independent audit of 2026-09-11, finding 11).
    """
    head = _checkout_commit()
    if head is None:
        return {"state": "no-checkout-found", "checkout_head": None}
    # Any honest abbreviation of the head, not the three lengths this module
    # happened to think of: git abbreviates to whatever is unambiguous, and this
    # repository's own commit tables use ten (independent audit of 2026-09-11,
    # round 2 item 3). Seven is git's own floor, below which a prefix names too
    # much.
    matches = len(code_ref) >= 7 and head.startswith(code_ref)
    state = "matches-checkout" if matches else "differs-from-checkout"
    return {"state": state, "checkout_head": head}


def run_is_fixture(export_payload: Mapping[str, Any]) -> bool:
    """Whether the run was a fixture run, from the export's own sealed identity.

    The Armarium seals exactly one of `fixture_id` (fixture route) and
    `submission_id` (real ingress), and refuses a manifest naming both or
    neither. Reading that is a measurement; a `--fixture` flag the operator could
    omit is not, and omitting it published a fixture score under the live label
    (independent audit of 2026-09-11, finding 11).
    """
    has_fixture = bool(
        isinstance(export_payload.get("fixture_id"), str) and export_payload["fixture_id"].strip()
    )
    has_submission = bool(
        isinstance(export_payload.get("submission_id"), str)
        and export_payload["submission_id"].strip()
    )
    if has_fixture == has_submission:
        raise CorpusRefusal(
            "ambiguous-run-identity: the export names "
            f"{'both a fixture and a submission' if has_fixture else 'neither a fixture nor a submission'}"
            "; a run's export must be identified by exactly one, and nothing can be labelled "
            "until it is"
        )
    return has_fixture


def evaluate_run(
    tree: RunTree,
    reference_pages: list[dict[str, Any]],
    *,
    code_ref: str,
    reference_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One `recordgold-evaluation.v1` report for one sealed run against reference pages.

    `reference_ledger` is a `recordgold-local-admission.v1` body. It is validated
    here rather than taken on the caller's word, its digest is derived from the
    body itself, and every reference page must appear in it by `self_hash` -- so
    `reference_ledger_verified` can only be true of a ledger, and
    `reference_ledger_sha256` can only be the digest of the ledger that was
    checked (independent audit of 2026-09-11, round 2 item 6).
    """
    if not isinstance(code_ref, str) or not code_ref:
        raise CorpusRefusal("malformed-record: an evaluation must name the code it ran under")
    reference_ledger_sha256 = None
    if reference_ledger is not None:
        try:
            reference_ledger = validate_local_admission_ledger(dict(reference_ledger))
        except CorpusRefusal as error:
            raise CorpusRefusal(
                f"reference-ledger-invalid: the named reference ledger does not validate: {error}"
            ) from error
        reference_ledger_sha256 = digest_bytes(canonical_bytes(dict(reference_ledger)))
    references: dict[str, dict[str, Any]] = {}
    for page in reference_pages:
        try:
            page = validate_reference_page(page)
        except CorpusRefusal as error:
            raise CorpusRefusal(
                f"reference-page-invalid: a reference page does not validate: {error}"
            ) from error
        digest = page["page"]["sha256"]
        if digest in references:
            raise CorpusRefusal(
                f"reference-page-collision: two reference pages carry page sha256 {digest}"
            )
        references[digest] = page

    if reference_ledger is not None:
        in_ledger = {
            row["reference_page_self_hash"]
            for row in reference_ledger["rows"]
            if row["decision"] == "admitted"
        }
        missing = sorted(
            page["self_hash"] for page in references.values() if page["self_hash"] not in in_ledger
        )
        if missing:
            raise CorpusRefusal(
                f"reference-page-not-in-ledger: {len(missing)} reference page(s) do not appear in "
                f"the named admission ledger, first {missing[0]}; the ledger names truth this "
                "report was not given"
            )

    read_only = ReadOnlyRunTree(tree)
    try:
        export_record = verify_final_seal(read_only)
    except ContractError as error:
        raise CorpusRefusal(
            f"no-export: the run has no verified Armarium export to score ({error})"
        ) from error
    export_payload = export_record["payload"]
    export_sha256 = digest_bytes(canonical_bytes(export_record))
    fixture = run_is_fixture(export_payload)

    run = read_only.read_run()
    hypotheses = hypotheses_from_export(export_payload, _established_text_hashes(read_only))
    page_shas = load_exemplar_page_shas(read_only)
    pipeline_acts = load_pipeline_proposal_acts(read_only)
    excluded = count_excluded_designator_artifacts(read_only)
    sha_by_act = {act["act_id"]: act["page_sha256"] for act in pipeline_acts}

    # One page's bytes sealed at two ordinals would compare that page twice and
    # count its reference acts twice, which surfaces later as a denominator that
    # does not reconcile -- a correct outcome under a message that names nothing
    # (independent audit of 2026-09-11, finding 22). Name the digest instead.
    repeated = sorted(
        {sha for sha in page_shas.values() if list(page_shas.values()).count(sha) > 1}
        & set(references)
    )
    if repeated:
        raise CorpusRefusal(
            f"malformed-record: the run seals page sha256 {repeated[0]} at more than one ordinal, "
            "so its reference records would be scored more than once"
        )

    by_category: dict[str, int] = {}
    for hypothesis in hypotheses.values():
        by_category[hypothesis["category"]] = by_category.get(hypothesis["category"], 0) + 1
    proposed_without_export_row = sorted(set(sha_by_act) - set(hypotheses))
    if proposed_without_export_row:
        raise CorpusRefusal(
            "malformed-record: proposal acts with no export row: "
            f"{proposed_without_export_row}; the export does not account for every act"
        )

    comparisons: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    unmatched_pipeline: list[dict[str, Any]] = []
    pages_without_reference: list[dict[str, Any]] = []
    cer_total: dict[str, int] = {}
    wer_total: dict[str, int] = {}
    cer_with_missed: dict[str, int] = {}
    wer_with_missed: dict[str, int] = {}
    compared_shas: set[str] = set()
    for ordinal in sorted(page_shas):
        sha = page_shas[ordinal]
        reference = references.get(sha)
        if reference is None:
            pages_without_reference.append({"ordinal": ordinal, "page_sha256": sha})
            continue
        compared_shas.add(sha)
        acts_on_page = [act for act in pipeline_acts if act["page_sha256"] == sha]
        try:
            comparison = compare_page(
                reference,
                acts_on_page,
                {
                    act["act_id"]: (
                        hypotheses[act["act_id"]]["status"],
                        hypotheses[act["act_id"]]["text"],
                    )
                    for act in acts_on_page
                },
                profile=PROFILE,
                excluded_region_counts=excluded,
            )
        except CorpusRefusal as error:
            raise CorpusRefusal(
                f"comparison-refused: page {ordinal} ({sha}) could not be compared: {error}"
            ) from error
        comparisons.append({"ordinal": ordinal, "comparison": comparison})
        matched = {pair["reference_physical_act_id"]: pair for pair in comparison["matched_pairs"]}
        for act in reference["acts"]:
            pair = matched.get(act["physical_act_id"])
            if pair is None:
                # GOALS 1: the act nobody found. It scores nothing in the
                # matched-pairs rate and its whole reference in the other.
                _accumulate(
                    cer_with_missed,
                    _wholly_deleted(len(character_units(act["text"], PROFILE))),
                )
                _accumulate(wer_with_missed, _wholly_deleted(len(word_units(act["text"], PROFILE))))
                records.append(
                    {
                        "physical_act_id": act["physical_act_id"],
                        "record_id": act["record_id"],
                        "page_ordinal": ordinal,
                        "page_sha256": sha,
                        "outcome": "missed",
                        "pipeline_act_id": None,
                        "export_category": None,
                        "text_status": None,
                        "status": None,
                        "cer": None,
                        "wer": None,
                        "note": (
                            "no pipeline act matched this record on a page the run sealed; "
                            "counted as wholly deleted in the including-missed aggregate"
                        ),
                    }
                )
                continue
            hypothesis = hypotheses[pair["pipeline_act_id"]]
            _accumulate(cer_total, pair["cer"])
            _accumulate(wer_total, pair["wer"])
            _accumulate(cer_with_missed, pair["cer"])
            _accumulate(wer_with_missed, pair["wer"])
            records.append(
                {
                    "physical_act_id": act["physical_act_id"],
                    "record_id": act["record_id"],
                    "page_ordinal": ordinal,
                    "page_sha256": sha,
                    "outcome": "scored",
                    "pipeline_act_id": pair["pipeline_act_id"],
                    "export_category": hypothesis["category"],
                    "text_status": hypothesis["text_status"],
                    "status": pair["status"],
                    "cer": {**pair["cer"], "rate": _rate(pair["cer"])},
                    "wer": {**pair["wer"], "rate": _rate(pair["wer"])},
                    "note": None,
                }
            )
        for row in comparison["unmatched_pipeline_acts"]:
            hypothesis = hypotheses[row["act_id"]]
            unmatched_pipeline.append(
                {
                    "act_id": row["act_id"],
                    "page_ordinal": ordinal,
                    "export_category": hypothesis["category"],
                    "text_status": hypothesis["text_status"],
                    "note": (
                        "no reference record overlaps this act at the threshold; RecordGold is "
                        "records-only, so this is reported, not scored as a false positive"
                    ),
                }
            )
    not_attempted: list[dict[str, Any]] = []
    for sha, reference in sorted(references.items()):
        if sha in compared_shas:
            continue
        for act in reference["acts"]:
            not_attempted.append(
                {
                    "physical_act_id": act["physical_act_id"],
                    "record_id": act["record_id"],
                    "page_ordinal": None,
                    "page_sha256": sha,
                    "outcome": "not-attempted",
                    "pipeline_act_id": None,
                    "export_category": None,
                    "text_status": None,
                    "status": None,
                    "cer": None,
                    "wer": None,
                    "note": "the run sealed no page with this digest",
                }
            )
    records.extend(not_attempted)

    scored = [row for row in records if row["outcome"] == "scored"]
    scored_by_category: dict[str, int] = {}
    for row in scored:
        scored_by_category[row["export_category"]] = (
            scored_by_category.get(row["export_category"], 0) + 1
        )
    aggregate = {
        "normalization_profile_id": PROFILE.profile_id,
        "matched_pairs_only": _scope(_MATCHED_SCOPE, cer_total, wer_total),
        "including_missed_records": _scope(_MISSED_SCOPE, cer_with_missed, wer_with_missed),
    }
    splits = sorted({page["split"] for page in references.values()})
    splits_present = sorted(
        {split for page in references.values() for split in page["splits_present"]}
    )
    report = {
        "schema": SCHEMA,
        "fixture": fixture,
        "label": FIXTURE_LABEL if fixture else LIVE_LABEL,
        "code_ref": code_ref,
        "code_ref_check": _code_ref_check(code_ref),
        "run": {
            "run_id": run["run_id"],
            "config_digest": run["config_digest"],
            "sealed_config_digests": run.get("sealed_config_digests"),
            "export_sha256": export_sha256,
            "export_status": export_payload.get("aggregate", {}).get("status"),
            "scenario": export_payload.get("scenario"),
        },
        "corpus": {
            "reference_pages": len(references),
            "reference_records": sum(len(page["acts"]) for page in references.values()),
            "reference_ledger_sha256": reference_ledger_sha256,
            "reference_ledger_verified": reference_ledger is not None,
            "reference_page_self_hashes": sorted(page["self_hash"] for page in references.values()),
            # Which split was scored, and every split those pages carry records
            # from. A score against `train` or against held-out `test` must never
            # read like a score against `val` (finding 12).
            "splits": {"scored": splits, "present_on_pages": splits_present},
        },
        "denominators": {
            "run_pages_sealed": len(page_shas),
            "run_pages_compared": len(compared_shas),
            "run_pages_without_reference": len(pages_without_reference),
            "reference_pages_not_in_run": len(references) - len(compared_shas),
            # One row per sealed proposal region; a continuation act has one per
            # page it spans, so the distinct act count sits beside it.
            "proposal_regions": len(pipeline_acts),
            "proposed_acts": len(set(sha_by_act)),
            "exported_acts_by_category": dict(sorted(by_category.items())),
            "excluded_designator_artifacts": excluded,
            "reference_records_scored": len(scored),
            "reference_records_scored_by_export_category": dict(sorted(scored_by_category.items())),
            "reference_records_missed": sum(1 for row in records if row["outcome"] == "missed"),
            "reference_records_not_attempted": len(not_attempted),
            "pipeline_acts_unmatched": len(unmatched_pipeline),
        },
        "aggregate": aggregate,
        "pages": comparisons,
        "records": records,
        "unmatched_pipeline_acts": unmatched_pipeline,
        "pages_without_reference": pages_without_reference,
    }
    report["self_hash"] = self_hash(report)
    return validate_evaluation(report)


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _validate_units(value: Any, what: str) -> None:
    units = _closed(value, _UNIT_FIELDS, what)
    for field in _EDIT_FIELDS:
        if not isinstance(units[field], int) or isinstance(units[field], bool) or units[field] < 0:
            raise CorpusRefusal(f"malformed-record: {what}.{field} must be a count")
    rate = _closed(units["rate"], _RATE_FIELDS, f"{what}.rate")
    if rate != _rate(units):
        raise CorpusRefusal(f"malformed-record: {what}.rate is not the rate of its own edits")
    if rate["denominator"] <= 0:
        raise CorpusRefusal(f"malformed-record: {what}.rate divides by a non-positive denominator")


def validate_evaluation(report: Any) -> dict[str, Any]:
    """Refuse an evaluation that is not exactly `recordgold-evaluation.v1`.

    This is the artifact a person reads as the measurement, and it was the one
    record in this package that nothing held to a shape (independent audit of
    2026-09-11, finding 10).

    **Closed exactly where a name is read**, and the docstring says so rather
    than claiming more (round 2 item 5): the top level, `run`, `corpus`,
    `corpus.splits`, `denominators`, `code_ref_check`, `aggregate` with both of
    its rate blocks and every rate inside them, and every record row. The three
    lists -- `pages`, `unmatched_pipeline_acts`, `pages_without_reference` --
    are counted against the denominators that claim them rather than shaped
    here, because each page's comparison is `compare.py`'s own family and
    carries its own self-hash. The record is self-hashed, and the record
    denominator and the scored-category histogram are reconciled against the
    rows that produced them.
    """
    report = _closed(report, _TOP_FIELDS, "evaluation report")
    if report["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {report['schema']!r}")
    if not verify_self_hash(report):
        raise CorpusRefusal("self-hash-mismatch: the report does not hash to its own self_hash")
    if not isinstance(report["fixture"], bool):
        raise CorpusRefusal("malformed-record: fixture must be a boolean")
    expected_label = FIXTURE_LABEL if report["fixture"] else LIVE_LABEL
    if report["label"] != expected_label:
        raise CorpusRefusal(
            "malformed-record: the label does not match the run's own sealed identity"
        )
    if not isinstance(report["code_ref"], str) or not report["code_ref"]:
        raise CorpusRefusal("malformed-record: an evaluation must name the code it ran under")
    check = _closed(report["code_ref_check"], _CODE_REF_CHECK_FIELDS, "code_ref_check")
    if check["state"] not in ("matches-checkout", "differs-from-checkout", "no-checkout-found"):
        raise CorpusRefusal(f"malformed-record: code_ref_check names state {check['state']!r}")
    if (check["checkout_head"] is None) != (check["state"] == "no-checkout-found"):
        raise CorpusRefusal(
            "malformed-record: code_ref_check carries a checkout head exactly when one was found"
        )

    corpus = _closed(report["corpus"], _CORPUS_FIELDS, "the corpus block")
    if corpus["reference_ledger_sha256"] is not None and not is_sha256(
        corpus["reference_ledger_sha256"]
    ):
        raise CorpusRefusal("malformed-record: reference_ledger_sha256 is not a sha256")
    if not isinstance(corpus["reference_ledger_verified"], bool):
        raise CorpusRefusal("malformed-record: reference_ledger_verified must be a boolean")
    hashes = corpus["reference_page_self_hashes"]
    if not isinstance(hashes, list) or len(hashes) != corpus["reference_pages"]:
        raise CorpusRefusal(
            "malformed-record: reference_page_self_hashes does not name every reference page"
        )
    splits = _closed(corpus["splits"], frozenset({"scored", "present_on_pages"}), "corpus.splits")
    for name, value in sorted(splits.items()):
        if not isinstance(value, list) or value != sorted(set(value)):
            raise CorpusRefusal(
                f"malformed-record: corpus.splits.{name} must be a sorted, deduplicated list"
            )
    if not splits["scored"]:
        raise CorpusRefusal("malformed-record: an evaluation must name the split(s) it scored")

    aggregate = _closed(report["aggregate"], _AGGREGATE_FIELDS, "the aggregate")
    # A closed vocabulary, not the current constant: a report sealed under a
    # profile this module no longer scores with is a historically correct
    # record, and a validator that refused it would be refusing the past. An
    # unrecognised profile is a different thing and is refused.
    if aggregate["normalization_profile_id"] not in PROFILES:
        raise CorpusRefusal(
            f"malformed-record: the aggregate names normalisation profile "
            f"{aggregate['normalization_profile_id']!r}, which is not a declared profile"
        )
    for name in ("matched_pairs_only", "including_missed_records"):
        block = _closed(aggregate[name], _AGGREGATE_SCOPE_FIELDS, f"aggregate.{name}")
        if not isinstance(block["scope"], str) or not block["scope"]:
            raise CorpusRefusal(f"malformed-record: aggregate.{name} states no scope")
        for unit in ("cer", "wer"):
            if block[unit] is not None:
                _validate_units(block[unit], f"aggregate.{name}.{unit}")

    # Closed before a single name is looked up: indexing an open record by name
    # raises `KeyError`, which is not a refusal (CodeRabbit on 497034d2).
    _closed(report["run"], _RUN_FIELDS, "the run block")
    totals = _closed(report["denominators"], _DENOMINATOR_FIELDS, "the denominators")
    outcomes = {"scored": 0, "missed": 0, "not-attempted": 0}
    for row in report["records"]:
        row = _closed(row, _RECORD_ROW_FIELDS, "a record row")
        if row["outcome"] not in outcomes:
            raise CorpusRefusal(
                f"malformed-record: a record row carries outcome {row['outcome']!r}, not one "
                f"of {sorted(outcomes)}"
            )
        if (row["outcome"] == "scored") != (row["cer"] is not None):
            raise CorpusRefusal(
                f"malformed-record: record row {row['record_id']!r} carries a rate exactly when "
                "it was scored"
            )
        if row["outcome"] == "scored":
            for unit in ("cer", "wer"):
                _validate_units(row[unit], f"record {row['record_id']!r} {unit}")
        outcomes[row["outcome"]] += 1
    if (
        outcomes["scored"] != totals["reference_records_scored"]
        or outcomes["missed"] != totals["reference_records_missed"]
        or outcomes["not-attempted"] != totals["reference_records_not_attempted"]
    ):
        raise CorpusRefusal(
            "malformed-record: the record rows disagree with the denominators that count them"
        )
    if sum(outcomes.values()) != corpus["reference_records"]:
        raise CorpusRefusal(
            "malformed-record: the evaluation's record denominator does not reconcile: "
            f"{sum(outcomes.values())} row(s) for {corpus['reference_records']} reference record(s)"
        )
    by_category = totals["reference_records_scored_by_export_category"]
    if not isinstance(by_category, dict) or any(
        not isinstance(name, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        for name, count in by_category.items()
    ):
        raise CorpusRefusal(
            "malformed-record: reference_records_scored_by_export_category is not a mapping of counts"
        )
    if sum(by_category.values()) != outcomes["scored"]:
        raise CorpusRefusal(
            "malformed-record: the scored-by-category histogram does not sum to the scored count"
        )
    if len(report["unmatched_pipeline_acts"]) != totals["pipeline_acts_unmatched"]:
        raise CorpusRefusal("malformed-record: pipeline_acts_unmatched does not count its own rows")
    if len(report["pages_without_reference"]) != totals["run_pages_without_reference"]:
        raise CorpusRefusal(
            "malformed-record: run_pages_without_reference does not count its own rows"
        )
    return report


def _rate_line(name: str, value: Mapping[str, Any] | None) -> str:
    if value is None:
        return f"{name}: nothing scored"
    rate = Fraction(value["rate"]["numerator"], value["rate"]["denominator"])
    return (
        f"{name}: {value['rate']['numerator']}/{value['rate']['denominator']} "
        f"= {float(rate):.4f} (S {value['substitutions']} I {value['insertions']} "
        f"D {value['deletions']} over {value['reference_units']} reference units)"
    )


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    """The report in the words a person reads first; the record carries the rest."""
    totals = report["denominators"]
    corpus = report["corpus"]
    lines = [
        f"Evaluation of run {report['run']['run_id']} ({report['run']['export_status']} export) "
        f"under code {report['code_ref']} ({report['code_ref_check']['state']})",
        report["label"],
        f"reference: {corpus['reference_pages']} page(s), "
        f"{corpus['reference_records']} record(s), split(s) "
        f"{'/'.join(corpus['splits']['scored'])} (records present on those pages from "
        f"{'/'.join(corpus['splits']['present_on_pages'])}); ledger "
        f"{'verified' if corpus['reference_ledger_verified'] else 'not given'}",
        f"run pages sealed {totals['run_pages_sealed']}, compared {totals['run_pages_compared']}, "
        f"without reference {totals['run_pages_without_reference']}; reference pages not in run "
        f"{totals['reference_pages_not_in_run']}",
        f"proposed acts {totals['proposed_acts']} ({totals['proposal_regions']} proposal "
        f"region(s)) by export category {totals['exported_acts_by_category']}",
        f"reference records scored {totals['reference_records_scored']} "
        f"(by export category {totals['reference_records_scored_by_export_category']}), missed "
        f"{totals['reference_records_missed']}, not attempted "
        f"{totals['reference_records_not_attempted']}; pipeline acts unmatched "
        f"{totals['pipeline_acts_unmatched']} (reported, not scored)",
    ]
    for block, prefix in (
        ("matched_pairs_only", "matched pairs only"),
        ("including_missed_records", "including missed records"),
    ):
        lines.append(f"{prefix} — {report['aggregate'][block]['scope']}")
        for name in ("cer", "wer"):
            lines.append(f"  {_rate_line(name.upper(), report['aggregate'][block][name])}")
    return lines


def write_report(report: Mapping[str, Any], path: str | Path) -> Path:
    """Write a validated report, refusing to overwrite and refusing an off-shape one."""
    validate_evaluation(dict(report))
    path = Path(path)
    if path.exists():
        raise CorpusRefusal(f"output-exists: {path} already exists; a report is never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(dict(report)))
    return path


def load_reference_pages(path: str | Path) -> list[dict[str, Any]]:
    """One `reference-pages.jsonl`, refused by name rather than by traceback.

    A missing file and a file that is not UTF-8 each refused under this module's
    own vocabulary, as a line that is not JSON already was (independent audit of
    2026-09-11, round 2 item 7).
    """
    path = Path(path)
    if not path.is_file():
        raise CorpusRefusal(f"missing-input-file: {path} is not a file")
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise CorpusRefusal(f"malformed-record: {path} is not UTF-8: {error}") from error
    pages = []
    for number, line in enumerate(body.splitlines(), start=1):
        if line.strip():
            try:
                pages.append(json.loads(line))
            except ValueError as error:
                raise CorpusRefusal(
                    f"malformed-record: line {number} of {path} is not JSON"
                ) from error
    return pages


def load_reference_ledger(path: str | Path) -> dict[str, Any]:
    """One admission ledger, refused by name rather than by traceback."""
    path = Path(path)
    if not path.is_file():
        raise CorpusRefusal(f"missing-input-file: {path} is not a file")
    try:
        return load_local_admission_ledger(path)
    except CorpusRefusal as error:
        raise CorpusRefusal(
            f"reference-ledger-invalid: the named reference ledger does not validate: {error}"
        ) from error
    except ValueError as error:
        raise CorpusRefusal(f"malformed-record: {path} is not JSON: {error}") from error


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reference-pages", required=True, help="reference-pages.jsonl")
    parser.add_argument(
        "--reference-ledger",
        help=(
            "the admission ledger the pages came from; every reference page must appear in it "
            "by self_hash"
        ),
    )
    parser.add_argument(
        "--code-ref", required=True, help="the commit the run and this score ran under"
    )
    parser.add_argument("--output", required=True, help="new file for the evaluation record")
    args = parser.parse_args(argv)
    ledger = load_reference_ledger(args.reference_ledger) if args.reference_ledger else None
    report = evaluate_run(
        RunTree(Path(args.run_root), args.run_id),
        load_reference_pages(args.reference_pages),
        code_ref=args.code_ref,
        reference_ledger=ledger,
    )
    write_report(report, args.output)
    for line in summary_lines(report):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script over real runs
    raise SystemExit(main())
