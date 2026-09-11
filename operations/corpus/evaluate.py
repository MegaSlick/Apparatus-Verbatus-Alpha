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

**Outside the establishment path, by construction.** `operations/corpus/` may
not import `pipeline/` and nothing here writes a run tree; the reference text
is read here and only here, after the run is sealed. This module never trains,
never tunes, never selects a reading.

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
)
from common.contracts.errors import ContractError
from common.contracts.stages import ARCHETYPUS
from common.runtree.store import RunTree
from common.stage import verify_final_seal
from operations.spike_perlector.models import OutputStatus

from . import CorpusRefusal
from .compare import (
    ReadOnlyRunTree,
    compare_page,
    count_excluded_designator_artifacts,
    load_exemplar_page_shas,
    load_pipeline_proposal_acts,
)
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
        "unestablished-delivered-act",
        "output-exists",
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


def _established_text_hashes(tree: RunTree) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for entry in tree.build_manifest(ARCHETYPUS, verify_inputs=False)["artifacts"]:
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
    for key in (
        "reference_units",
        "hypothesis_units",
        "matches",
        "substitutions",
        "insertions",
        "deletions",
    ):
        total[key] = total.get(key, 0) + part[key]


def evaluate_run(
    tree: RunTree,
    reference_pages: list[dict[str, Any]],
    *,
    code_ref: str,
    reference_ledger_sha256: str | None,
    fixture: bool,
) -> dict[str, Any]:
    """One `recordgold-evaluation.v1` report for one sealed run against reference pages."""
    if not isinstance(code_ref, str) or not code_ref:
        raise CorpusRefusal("malformed-record: an evaluation must name the code it ran under")
    if reference_ledger_sha256 is not None and not is_sha256(reference_ledger_sha256):
        raise CorpusRefusal("malformed-record: reference_ledger_sha256 is not a sha256")
    references: dict[str, dict[str, Any]] = {}
    for page in reference_pages:
        page = validate_reference_page(page)
        digest = page["page"]["sha256"]
        if digest in references:
            raise CorpusRefusal(
                f"reference-page-collision: two reference pages carry page sha256 {digest}"
            )
        references[digest] = page

    try:
        export_record = verify_final_seal(tree)
    except ContractError as error:
        raise CorpusRefusal(
            f"no-export: the run has no verified Armarium export to score ({error})"
        ) from error
    export_payload = export_record["payload"]
    export_sha256 = digest_bytes(canonical_bytes(export_record))
    run = tree.read_run()

    read_only = ReadOnlyRunTree(tree)
    hypotheses = hypotheses_from_export(export_payload, _established_text_hashes(tree))
    page_shas = load_exemplar_page_shas(read_only)
    pipeline_acts = load_pipeline_proposal_acts(read_only)
    excluded = count_excluded_designator_artifacts(read_only)
    sha_by_act = {act["act_id"]: act["page_sha256"] for act in pipeline_acts}

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
    compared_shas: set[str] = set()
    for ordinal in sorted(page_shas):
        sha = page_shas[ordinal]
        reference = references.get(sha)
        if reference is None:
            pages_without_reference.append({"ordinal": ordinal, "page_sha256": sha})
            continue
        compared_shas.add(sha)
        acts_on_page = [act for act in pipeline_acts if act["page_sha256"] == sha]
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
            excluded_region_counts=excluded,
        )
        comparisons.append({"ordinal": ordinal, "comparison": comparison})
        matched = {pair["reference_physical_act_id"]: pair for pair in comparison["matched_pairs"]}
        for act in reference["acts"]:
            pair = matched.get(act["physical_act_id"])
            if pair is None:
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
                    }
                )
                continue
            hypothesis = hypotheses[pair["pipeline_act_id"]]
            _accumulate(cer_total, pair["cer"])
            _accumulate(wer_total, pair["wer"])
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
                    "page_sha256": sha,
                    "outcome": "not-attempted",
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
        "cer": {**cer_total, "rate": _rate(cer_total)} if cer_total else None,
        "wer": {**wer_total, "rate": _rate(wer_total)} if wer_total else None,
    }
    report = {
        "schema": SCHEMA,
        "fixture": bool(fixture),
        "label": FIXTURE_LABEL if fixture else LIVE_LABEL,
        "code_ref": code_ref,
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
            "reference_page_self_hashes": sorted(page["self_hash"] for page in references.values()),
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
    totals = report["denominators"]
    if (
        totals["reference_records_scored"]
        + totals["reference_records_missed"]
        + totals["reference_records_not_attempted"]
        != report["corpus"]["reference_records"]
    ):
        raise CorpusRefusal(
            "malformed-record: the evaluation's record denominator does not reconcile"
        )
    report["self_hash"] = self_hash(report)
    return report


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    """The report in the words a person reads first; the record carries the rest."""
    totals = report["denominators"]
    lines = [
        f"Evaluation of run {report['run']['run_id']} ({report['run']['export_status']} export) "
        f"under code {report['code_ref']}",
        report["label"],
        f"reference: {report['corpus']['reference_pages']} page(s), "
        f"{report['corpus']['reference_records']} record(s)",
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
    for name in ("cer", "wer"):
        value = report["aggregate"][name]
        if value is None:
            lines.append(f"{name.upper()}: nothing scored")
        else:
            rate = Fraction(value["rate"]["numerator"], value["rate"]["denominator"])
            lines.append(
                f"{name.upper()}: {value['rate']['numerator']}/{value['rate']['denominator']} "
                f"= {float(rate):.4f} (S {value['substitutions']} I {value['insertions']} "
                f"D {value['deletions']} over {value['reference_units']} reference units)"
            )
    return lines


def write_report(report: Mapping[str, Any], path: str | Path) -> Path:
    path = Path(path)
    if path.exists():
        raise CorpusRefusal(f"output-exists: {path} already exists; a report is never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(dict(report)))
    return path


def load_reference_pages(path: str | Path) -> list[dict[str, Any]]:
    pages = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                pages.append(json.loads(line))
            except ValueError as error:
                raise CorpusRefusal(
                    f"malformed-record: line {number} of {path} is not JSON"
                ) from error
    return pages


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reference-pages", required=True, help="reference-pages.jsonl")
    parser.add_argument("--reference-ledger", help="the admission ledger the pages came from")
    parser.add_argument(
        "--code-ref", required=True, help="the commit the run and this score ran under"
    )
    parser.add_argument("--output", required=True, help="new file for the evaluation record")
    parser.add_argument("--fixture", action="store_true", help="label the result a fixture result")
    args = parser.parse_args(argv)
    ledger_sha256 = (
        digest_bytes(Path(args.reference_ledger).read_bytes()) if args.reference_ledger else None
    )
    report = evaluate_run(
        RunTree(Path(args.run_root), args.run_id),
        load_reference_pages(args.reference_pages),
        code_ref=args.code_ref,
        reference_ledger_sha256=ledger_sha256,
        fixture=args.fixture,
    )
    write_report(report, args.output)
    for line in summary_lines(report):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script over real runs
    raise SystemExit(main())
