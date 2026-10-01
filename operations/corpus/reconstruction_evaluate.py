"""Score the Armarium's reconstructions apart from the diplomatic reading.

A reconstruction joins an act cut by a page break: the head act's delivered
text, one line break, the tail act's (`reconstructions.jsonl` in the sealed
export bundle). It is not an act and never enters the diplomatic score
(`evaluate.py`); this report counts and scores it on its own:

- every continuation join by status, and each one not reconstructed by reason;
- departures: the characters by which a reconstruction differs from its two
  delivered literals joined by one line break, per reconstruction and per act
  it joins. The join adds nothing, so any departure is a defect;
- accuracy against reference truth where it has both acts: the reference texts
  of the records the head and tail acts are paired with (by IoU, as
  `evaluate.py` pairs them) are joined the same way and scored with the sealed
  scorer. A reconstruction with a side the reference does not have is counted
  by which side it has, and not scored.

The report carries counts and identifiers, never text. It reads the sealed tree
read-only and returns nothing to the pipeline.
"""

from __future__ import annotations

import argparse
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence
from zipfile import ZipFile

from rapidfuzz.distance import Levenshtein

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import ContractError
from common.runtree.store import RunTree
from common.stage import verify_final_seal
from operations.spike_perlector.models import OutputStatus
from operations.spike_perlector.normalization import GRAPHEMIC_V1
from operations.spike_perlector.scoring import score_response

from . import CorpusRefusal
from .cache import write_new_file
from .compare import (
    ReadOnlyRunTree,
    compare_page_geometry,
    load_exemplar_page_shas,
    load_pipeline_reading_acts,
)
from .evaluate import _established_text_hashes, hypotheses_from_export, load_reference_pages

SCHEMA = "recordgold-reconstruction-evaluation.v1"
RECONSTRUCTED = "reconstructed"

RECONSTRUCTION_EVALUATION_REFUSAL_REASONS = frozenset(
    {
        "comparison-refused",
        "export-refused",
        "malformed-record",
        "no-export",
        "output-exists",
        "output-in-run-tree",
    }
)


class Refusal(CorpusRefusal):
    reasons = RECONSTRUCTION_EVALUATION_REFUSAL_REASONS


def bundle_reconstructions(
    tree: ReadOnlyRunTree, export_payload: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The continuation joins and reconstructions of the sealed export bundle."""
    bundle = export_payload.get("bundle")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("reference"), dict):
        raise Refusal("malformed-record: the export names no bundle")
    data = tree.read_bytes(bundle["reference"]["relative_path"])
    if digest_bytes(data) != bundle.get("sha256"):
        raise Refusal("malformed-record: the export bundle does not match its digest")
    with ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        joins = json.loads(archive.read("sources.json")).get("continuation_joins") or []
        rows = (
            [
                json.loads(line)
                for line in archive.read("reconstructions.jsonl").splitlines()
                if line.strip()
            ]
            if "reconstructions.jsonl" in names
            else []
        )
    return joins, rows


def reconstruction_report(
    *,
    joins: Sequence[Mapping[str, Any]],
    reconstructions: Sequence[Mapping[str, Any]],
    delivered_texts: Mapping[str, str],
    references_by_act: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The report body for one run's joins and reconstructions.

    `delivered_texts` is `{act_id: text}` of the export's delivered acts, each
    already bound to the Archetypus record that established it;
    `references_by_act` is `{act_id: reference act}` for every act the IoU
    assignment paired with a reference record.
    """
    reconstructed_joins = {join["join_id"] for join in joins if join.get("status") == RECONSTRUCTED}
    if {row["join_id"] for row in reconstructions} != reconstructed_joins or len(
        reconstructions
    ) != len(reconstructed_joins):
        raise Refusal(
            "malformed-record: the reconstructions are not exactly the joins marked reconstructed"
        )
    rows = []
    departures_by_act: Counter[str] = Counter()
    gold_sides: Counter[str] = Counter()
    cer = {"errors": 0, "units": 0}
    wer = {"errors": 0, "units": 0}
    for record in sorted(reconstructions, key=lambda row: row["join_id"]):
        head, tail = record["head_act_id"], record["tail_act_id"]
        if head not in delivered_texts or tail not in delivered_texts:
            raise Refusal(
                f"malformed-record: reconstruction {record['join_id']!r} joins an act the "
                "export did not deliver"
            )
        diplomatic = delivered_texts[head] + "\n" + delivered_texts[tail]
        departures = Levenshtein.distance(record["reconstructed_text"], diplomatic)
        departures_by_act[head] += departures
        departures_by_act[tail] += departures
        head_ref, tail_ref = references_by_act.get(head), references_by_act.get(tail)
        sides = (
            "both"
            if head_ref and tail_ref
            else "head-only"
            if head_ref
            else "tail-only"
            if tail_ref
            else "none"
        )
        gold_sides[sides] += 1
        score = None
        if sides == "both":
            scored = score_response(
                head_ref["text"] + "\n" + tail_ref["text"],
                status=OutputStatus.COMPLETE,
                text=record["reconstructed_text"],
                profile=GRAPHEMIC_V1,
            )
            score = {
                "cer_errors": scored.cer.edits.errors,
                "cer_units": scored.cer.reference_units,
                "wer_errors": scored.wer.edits.errors,
                "wer_units": scored.wer.reference_units,
            }
            cer["errors"] += score["cer_errors"]
            cer["units"] += score["cer_units"]
            wer["errors"] += score["wer_errors"]
            wer["units"] += score["wer_units"]
        rows.append(
            {
                "join_id": record["join_id"],
                "head_act_id": head,
                "tail_act_id": tail,
                "head_page_ordinal": record["head_page_ordinal"],
                "tail_page_ordinal": record["tail_page_ordinal"],
                "departures": departures,
                "head_record_id": head_ref["record_id"] if head_ref else None,
                "tail_record_id": tail_ref["record_id"] if tail_ref else None,
                "reference_sides": sides,
                "score": score,
            }
        )
    by_status = Counter(join.get("status") for join in joins)
    by_reason = Counter(
        join.get("not_reconstructed_reason")
        for join in joins
        if join.get("status") != RECONSTRUCTED
    )
    return {
        "schema": SCHEMA,
        "joins": {
            "total": len(joins),
            "by_status": dict(sorted(by_status.items())),
            "not_reconstructed_by_reason": dict(sorted(by_reason.items())),
        },
        "reconstructions": {
            "total": len(rows),
            "acts_joined": len(departures_by_act),
            "departing": sum(1 for row in rows if row["departures"]),
            "departures": sum(row["departures"] for row in rows),
            "departures_by_act": dict(sorted(departures_by_act.items())),
        },
        "reference": {
            "by_sides": dict(sorted(gold_sides.items())),
            "cer": cer,
            "wer": wer,
        },
        "rows": rows,
    }


def evaluate_run(tree: RunTree, reference_pages: list[dict[str, Any]]) -> dict[str, Any]:
    """One self-hashed reconstruction report for a sealed run against reference pages."""
    read_only = ReadOnlyRunTree(tree)
    try:
        export_record = verify_final_seal(read_only)
    except ContractError as error:
        raise Refusal(f"no-export: the run has no verified Armarium export ({error})") from error
    export_payload = export_record["payload"]
    try:
        hypotheses = hypotheses_from_export(export_payload, _established_text_hashes(read_only))
    except CorpusRefusal as error:
        raise Refusal(f"export-refused: the export's texts could not be bound: {error}") from error
    delivered = {
        act_id: hypothesis["text"]
        for act_id, hypothesis in hypotheses.items()
        if hypothesis["category"] == "delivered"
    }
    joins, reconstructions = bundle_reconstructions(read_only, export_payload)

    references = {page["page"]["sha256"]: page for page in reference_pages}
    references_by_act: dict[str, Mapping[str, Any]] = {}
    try:
        acts = load_pipeline_reading_acts(read_only)
        sealed = set(load_exemplar_page_shas(read_only).values())
    except CorpusRefusal as error:
        raise Refusal(
            f"comparison-refused: the run's act regions could not be read: {error}"
        ) from error
    for sha in sorted(sealed & set(references)):
        page = references[sha]
        try:
            geometry = compare_page_geometry(
                page, [act for act in acts if act["page_sha256"] == sha]
            )
        except CorpusRefusal as error:
            raise Refusal(f"comparison-refused: page {sha} could not be paired: {error}") from error
        by_id = {act["physical_act_id"]: act for act in page["acts"]}
        for pair in geometry["matched_pairs"]:
            references_by_act[pair["pipeline_act_id"]] = by_id[pair["reference_physical_act_id"]]

    body = {
        "run_id": tree.run_id,
        "export_sha256": digest_bytes(canonical_bytes(export_record)),
        **reconstruction_report(
            joins=joins,
            reconstructions=reconstructions,
            delivered_texts=delivered,
            references_by_act=references_by_act,
        ),
    }
    body["self_hash"] = self_hash(body)
    return body


def summary_lines(report: Mapping[str, Any]) -> list[str]:
    """A short, count-only summary of a report."""
    joins, made, reference = report["joins"], report["reconstructions"], report["reference"]
    return [
        f"joins {joins['total']}: {joins['by_status']}; not reconstructed "
        f"{joins['not_reconstructed_by_reason']}",
        f"reconstructions {made['total']} over {made['acts_joined']} act(s); departing "
        f"{made['departing']} ({made['departures']} character(s))",
        f"against reference: {reference['by_sides']}; cer {reference['cer']}; "
        f"wer {reference['wer']}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reference-pages", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    tree = RunTree(args.run_root, args.run_id)
    output = args.out.resolve()
    if output.is_relative_to(tree.root.resolve()):
        raise Refusal("output-in-run-tree: the report must be written outside the run tree")
    report = evaluate_run(tree, load_reference_pages(args.reference_pages))
    if not write_new_file(args.out, canonical_bytes(report)):
        raise Refusal(f"output-exists: {args.out}")
    for line in summary_lines(report):
        print(line)
    # Any departure means the export changed a delivered text it only joins.
    return 0 if report["reconstructions"]["departing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
