"""Score the Coniector's reconstructions apart from the diplomatic reading.

A reconstruction is the Coniector's labelled, unconfirmed layer beneath a
delivered act (or a join of delivered acts across a page break): the act's
diplomatic reading with the model's departures applied (`coniector.jsonl` in
the sealed export bundle, the rows `sources.json` names under
`reconstructions`). It is not an act and never enters the diplomatic score
(`evaluate.py`); this report counts and scores it on its own:

- every continuation join by status and reason (code never joins text, so each
  is `not-reconstructed`);
- every reconstruction row shown, by unit (`act` or `join`), by maker, made or
  not made (and why), with its departures, the characters by which its text
  differs from its delivered literals joined by one line break, and its flags;
- accuracy against reference truth where every act of a made row is paired
  with a reference record (by IoU, as `evaluate.py` pairs them): the joined
  reference texts scored with the sealed scorer against the reconstruction and,
  on the same records, against the diplomatic literals, so the report states
  what the reconstruction changed. A row with only some or none of its acts in
  the reference is counted by which, and not scored.

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
from common.reading_annotations import read_doubt_marks
from common.runtree.store import RunTree
from common.stage import verify_final_seal

from . import CorpusRefusal
from .cache import write_new_file
from .compare import (
    ReadOnlyRunTree,
    compare_page_geometry,
    load_exemplar_page_shas,
    load_pipeline_reading_acts,
)
from .evaluate import (
    _established_text_hashes,
    hypotheses_from_export,
    load_reference_ledger,
    load_reference_pages,
)
from .local_admission import validate_local_admission_ledger
from .normalization import GRAPHEMIC_V1, character_units, within_text_bounds, word_units
from .scoring import REFERENCE_TEXT_OUT_OF_BOUNDS, TEXT_OUT_OF_BOUNDS, OutputStatus, score_response

SCHEMA = "recordgold-reconstruction-evaluation.v3"
# The Armarium's member for the Coniector's rows (`pipeline/7_armarium/coniector_layer.py`).
CONIECTOR_MEMBER = "coniector.jsonl"

RECONSTRUCTION_EVALUATION_REFUSAL_REASONS = frozenset(
    {
        "comparison-refused",
        "export-refused",
        "malformed-record",
        "no-export",
        "output-exists",
        "output-in-run-tree",
        "reference-ledger-invalid",
        "reference-page-not-in-ledger",
    }
)


class Refusal(CorpusRefusal):
    reasons = RECONSTRUCTION_EVALUATION_REFUSAL_REASONS


def bundle_reconstructions(
    tree: ReadOnlyRunTree, export_payload: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[list[str]], list[dict[str, Any]] | None]:
    """The continuation joins, the rows `sources.json` names, and the Coniector's rows.

    The rows are `None` when the bundle carries no `coniector.jsonl` (JSONL was
    not a selected format, or nothing was shown).
    """
    bundle = export_payload.get("bundle")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("reference"), dict):
        raise Refusal("malformed-record: the export names no bundle")
    data = tree.read_bytes(bundle["reference"]["relative_path"])
    if digest_bytes(data) != bundle.get("sha256"):
        raise Refusal("malformed-record: the export bundle does not match its digest")
    with ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        sources = json.loads(archive.read("sources.json"))
        joins = sources.get("continuation_joins") or []
        shown = sources.get("reconstructions") or []
        rows = (
            [
                json.loads(line)
                for line in archive.read(CONIECTOR_MEMBER).splitlines()
                if line.strip()
            ]
            if CONIECTOR_MEMBER in names
            else None
        )
    return joins, shown, rows


def _score(reference: str, text: str) -> dict[str, int]:
    scored = score_response(
        reference, status=OutputStatus.COMPLETE, text=text, profile=GRAPHEMIC_V1
    )
    return {
        "cer_errors": scored.cer.edits.errors,
        "cer_units": scored.cer.reference_units,
        "wer_errors": scored.wer.edits.errors,
        "wer_units": scored.wer.reference_units,
    }


def _wholly_deleted(reference: str) -> dict[str, int]:
    """A reference's units charged as errors, for a text that could not be measured."""
    cer_units = len(character_units(reference, GRAPHEMIC_V1))
    wer_units = len(word_units(reference, GRAPHEMIC_V1))
    return {
        "cer_errors": cer_units,
        "cer_units": cer_units,
        "wer_errors": wer_units,
        "wer_units": wer_units,
    }


def _add(total: dict[str, int], score: Mapping[str, int]) -> None:
    for key in total:
        total[key] += score[key]


def reconstruction_report(
    *,
    joins: Sequence[Mapping[str, Any]],
    shown: Sequence[Sequence[str]],
    rows: Sequence[Mapping[str, Any]] | None,
    delivered_texts: Mapping[str, str],
    references_by_act: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """The report body for one run's joins and Coniector rows.

    `shown` is each row's `act_ids` as `sources.json` names them; `rows` the
    `coniector.jsonl` rows, or `None` when the export packaged none as JSONL:
    the joins and the shown rows are still counted and `measured` says the
    rows could not be. `delivered_texts` is `{act_id: text}` of the export's
    delivered acts, each bound to the Archetypus record that established it;
    `references_by_act` is `{act_id: reference act}` for every act the IoU
    assignment paired with a reference record.
    """
    measured = rows is not None or not shown
    rows = list(rows or [])
    if measured and sorted(tuple(row.get("act_ids") or ()) for row in rows) != sorted(
        tuple(act_ids) for act_ids in shown
    ):
        raise Refusal(
            "malformed-record: the Coniector rows are not exactly the reconstructions the "
            "sources name"
        )
    out_rows = []
    by_unit: Counter[str] = Counter()
    by_maker: Counter[str] = Counter()
    not_made: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    sides_count: Counter[str] = Counter()
    reconstruction_score = {"cer_errors": 0, "cer_units": 0, "wer_errors": 0, "wer_units": 0}
    diplomatic_score = dict(reconstruction_score)
    for row in sorted(rows, key=lambda item: (item["unit"], list(item["act_keys"]))):
        act_ids = list(row["act_ids"])
        if not all(act_id in delivered_texts for act_id in act_ids):
            raise Refusal(
                f"malformed-record: a reconstruction stands beneath {act_ids}, which the export "
                "did not all deliver"
            )
        for act_id, piece in zip(act_ids, row["diplomatic_raw_pieces"], strict=True):
            if read_doubt_marks(piece)[0] != delivered_texts[act_id]:
                raise Refusal(
                    f"malformed-record: the reconstruction beneath {act_id} departs from a text "
                    "other than the one delivered"
                )
        diplomatic = "\n".join(delivered_texts[act_id] for act_id in act_ids)
        made = row["made"] is True
        text = row["reconstruction_text"] if made else None
        by_unit[row["unit"]] += 1
        by_maker[row["maker"]["kind"]] += 1
        not_made.update(reason["code"] for reason in row["not_made"])
        flags.update(flag["code"] for flag in row["flags"])
        references = [references_by_act.get(act_id) for act_id in act_ids]
        paired = sum(reference is not None for reference in references)
        sides = "all" if paired == len(act_ids) else "some" if paired else "none"
        # A reconstruction or literal beyond the scoring bounds is named, and
        # neither scored nor compared with its literals; where the reference has
        # every act, both sides are charged as wholly deleted. A joined
        # reference beyond the bounds is named and charged to neither side.
        readable = not made or all(
            within_text_bounds(item, GRAPHEMIC_V1) for item in (text, diplomatic)
        )
        unmeasured = None if readable else TEXT_OUT_OF_BOUNDS
        reference_text = None
        if made and sides == "all":
            reference_text = "\n".join(reference["text"] for reference in references)
            if not within_text_bounds(reference_text, GRAPHEMIC_V1):
                unmeasured = REFERENCE_TEXT_OUT_OF_BOUNDS
            elif unmeasured is not None:
                _add(reconstruction_score, _wholly_deleted(reference_text))
                _add(diplomatic_score, _wholly_deleted(reference_text))
        score = None
        if reference_text is not None and unmeasured is None:
            score = {
                "reconstruction": _score(reference_text, text),
                "diplomatic": _score(reference_text, diplomatic),
            }
            _add(reconstruction_score, score["reconstruction"])
            _add(diplomatic_score, score["diplomatic"])
        if made:
            sides_count[sides] += 1
        out_rows.append(
            {
                "unit": row["unit"],
                "act_ids": act_ids,
                "act_keys": list(row["act_keys"]),
                "maker": row["maker"]["kind"],
                "made": made,
                "not_made": sorted(reason["code"] for reason in row["not_made"]),
                "departures": len(row["departures"]),
                "departed_characters": (
                    Levenshtein.distance(text, diplomatic) if made and readable else None
                ),
                "flags": sorted(flag["code"] for flag in row["flags"]),
                "reference_record_ids": [
                    reference["record_id"] if reference else None for reference in references
                ],
                "reference_sides": sides,
                "score": score,
                "unmeasured": unmeasured,
            }
        )
    by_status = Counter(join.get("status") for join in joins)
    by_reason = Counter(join.get("not_reconstructed_reason") for join in joins)
    made_rows = [row for row in out_rows if row["made"]]
    compared_rows = [row for row in made_rows if row["departed_characters"] is not None]
    return {
        "schema": SCHEMA,
        "joins": {
            "total": len(joins),
            "by_status": dict(sorted(by_status.items())),
            "by_reason": dict(sorted(by_reason.items())),
        },
        "reconstructions": {
            "measured": measured,
            "shown": len(shown),
            "total": len(out_rows),
            "by_unit": dict(sorted(by_unit.items())),
            "by_maker": dict(sorted(by_maker.items())),
            "made": len(made_rows),
            "not_made_by_code": dict(sorted(not_made.items())),
            "unmeasured": sum(1 for row in made_rows if row["unmeasured"] is not None),
            "departures": sum(row["departures"] for row in made_rows),
            "departing": sum(1 for row in compared_rows if row["departed_characters"]),
            "departed_characters": sum(row["departed_characters"] for row in compared_rows),
            "flags_by_code": dict(sorted(flags.items())),
        },
        "reference": {
            "made_by_sides": dict(sorted(sides_count.items())),
            "reconstruction": reconstruction_score,
            "diplomatic": diplomatic_score,
        },
        "rows": out_rows,
    }


def _check_in_ledger(
    reference_pages: Sequence[Mapping[str, Any]], ledger: Mapping[str, Any]
) -> str:
    """The ledger's digest, once each reference page is one it admitted, byte for byte."""
    try:
        ledger = validate_local_admission_ledger(dict(ledger))
    except CorpusRefusal as error:
        raise Refusal(f"reference-ledger-invalid: {error}") from error
    admitted = {canonical_bytes(page) for page in ledger["reference_pages"]}
    for page in reference_pages:
        if canonical_bytes(dict(page)) not in admitted:
            raise Refusal(
                f"reference-page-not-in-ledger: the reference page for {page['page']['sha256']} "
                "is not one the named admission ledger carries"
            )
    return digest_bytes(canonical_bytes(dict(ledger)))


def evaluate_run(
    tree: RunTree,
    reference_pages: list[dict[str, Any]],
    *,
    reference_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One self-hashed reconstruction report for a sealed run against reference pages.

    With `reference_ledger`, a `recordgold-local-admission.v1` body, every
    reference page must be one the ledger carries, and the report names the
    ledger's digest; without it the report says the pages were not verified.
    """
    reference_ledger_sha256 = (
        _check_in_ledger(reference_pages, reference_ledger)
        if reference_ledger is not None
        else None
    )
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
    joins, shown, rows = bundle_reconstructions(read_only, export_payload)

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
        "reference_ledger_sha256": reference_ledger_sha256,
        "reference_ledger_verified": reference_ledger is not None,
        **reconstruction_report(
            joins=joins,
            shown=shown,
            rows=rows,
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
        f"joins {joins['total']}: {joins['by_status']}; by reason {joins['by_reason']}",
        f"reconstructions measured {made['measured']}: {made['total']} shown "
        f"{made['by_unit']}, made {made['made']}, not made {made['not_made_by_code']}; "
        f"{made['departures']} departure(s), {made['departed_characters']} character(s)",
        f"against reference ({reference['made_by_sides']}): reconstruction "
        f"{reference['reconstruction']}; diplomatic {reference['diplomatic']}",
        "reference ledger " + ("verified" if report["reference_ledger_verified"] else "not given"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reference-pages", type=Path, required=True)
    parser.add_argument(
        "--reference-ledger",
        type=Path,
        help="the admission ledger the pages came from; every reference page must be one it carries",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    tree = RunTree(args.run_root, args.run_id)
    output = args.out.resolve()
    if output.is_relative_to(tree.root.resolve()):
        raise Refusal("output-in-run-tree: the report must be written outside the run tree")
    try:
        ledger = load_reference_ledger(args.reference_ledger) if args.reference_ledger else None
    except CorpusRefusal as error:
        raise Refusal(f"reference-ledger-invalid: {error}") from error
    report = evaluate_run(tree, load_reference_pages(args.reference_pages), reference_ledger=ledger)
    if not write_new_file(args.out, canonical_bytes(report)):
        raise Refusal(f"output-exists: {args.out}")
    for line in summary_lines(report):
        print(line)
    # Reconstructions the bundle did not package as JSONL were not measured.
    return 0 if report["reconstructions"]["measured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
