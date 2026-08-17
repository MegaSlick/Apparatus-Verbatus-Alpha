"""Create and validate append-only R7a gold records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.contracts.errors import ContractError, SchemaRefusal

from .core import (
    DRAW_SCHEMA,
    SAMPLE_SCHEMA,
    adjudicate,
    bind_instrument,
    build_sampling_draw,
    ingest_manual_pick,
    read_json,
    read_transcription_text,
    transcribe,
    validate_corpus,
    validate_record,
    verify_recorded_draw,
    verify_stratified_selection,
    write_append_only,
)


def _records_in(directory: str) -> list[dict[str, object]]:
    """Every gold record written under one directory, in a stable order."""
    root = Path(directory)
    if not root.is_dir():
        raise SchemaRefusal(f"{directory} is not a directory of gold records")
    return [read_json(path) for path in sorted(root.glob("*.json"))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--run", required=True)
    sample.add_argument("--catalog", required=True)
    sample.add_argument("--plan", required=True)
    sample.add_argument("--output-dir", required=True)
    manual = commands.add_parser("ingest-manual")
    manual.add_argument("--run", required=True)
    manual.add_argument("--pick", required=True)
    manual.add_argument("--output", required=True)
    bind = commands.add_parser("bind-instrument")
    bind.add_argument("--sample", required=True)
    bind.add_argument("--act-identity", required=True)
    bind.add_argument("--protocol-digest", required=True)
    bind.add_argument("--output", required=True)
    bind.add_argument("--run")
    transcription = commands.add_parser("transcribe")
    transcription.add_argument("--sample", required=True)
    transcription.add_argument("--act-identity", required=True)
    transcription.add_argument("--transcriber", required=True)
    transcription.add_argument("--text-file", required=True)
    transcription.add_argument("--output", required=True)
    transcription.add_argument("--run")
    adjudication = commands.add_parser("adjudicate")
    adjudication.add_argument("--first", required=True)
    adjudication.add_argument("--second", required=True)
    adjudication.add_argument("--output", required=True)
    adjudication.add_argument("--adjudicator")
    adjudication.add_argument("--text-file")
    verify = commands.add_parser("verify-sampling")
    verify.add_argument("directory")
    verify.add_argument("--run", required=True)
    verify.add_argument("--catalog")
    verify.add_argument("--plan")
    corpus = commands.add_parser("validate-corpus")
    corpus.add_argument("directory")
    corpus.add_argument("--run")
    validate = commands.add_parser("validate")
    validate.add_argument("record")
    validate.add_argument("--run")
    args = parser.parse_args(argv)
    if args.command == "sample":
        draw, selected = build_sampling_draw(
            args.run, read_json(args.catalog), read_json(args.plan)
        )
        # The draw is the membership authority, so it is published FIRST: an
        # interrupted run then leaves a draw whose members are partly missing --
        # which verify-sampling refuses by name -- never orphan samples with no
        # membership record to check them against.
        write_append_only(Path(args.output_dir) / f"draw-{draw['self_hash']}.json", draw)
        for record in selected:
            write_append_only(Path(args.output_dir) / f"{record['sample_digest']}.json", record)
    elif args.command == "ingest-manual":
        record = ingest_manual_pick(args.run, read_json(args.pick))
        output = Path(args.output)
        existing = _records_in(str(output.parent)) if output.parent.is_dir() else []
        # A stratum is a collection fact, not a property R0 can derive from one
        # pick. Reconcile the destination corpus before publishing so a second
        # spelling of the same page is refused rather than counted twice.
        validate_corpus([*existing, record], args.run)
        write_append_only(output, record)
    elif args.command == "bind-instrument":
        write_append_only(
            args.output,
            bind_instrument(
                read_json(args.sample), args.act_identity, args.protocol_digest, args.run
            ),
        )
    elif args.command == "transcribe":
        write_append_only(
            args.output,
            transcribe(
                read_json(args.sample),
                args.act_identity,
                args.transcriber,
                read_transcription_text(args.text_file),
                args.run,
            ),
        )
    elif args.command == "adjudicate":
        write_append_only(
            args.output,
            adjudicate(
                read_json(args.first),
                read_json(args.second),
                adjudicator=args.adjudicator,
                text=(
                    read_transcription_text(args.text_file) if args.text_file is not None else None
                ),
            ),
        )
    elif args.command == "verify-sampling":
        # One pairing rule for both paths, asked before any verification runs.
        if (args.catalog is None) != (args.plan is None):
            raise SchemaRefusal("--catalog and --plan must be supplied together")
        records = _records_in(args.directory)
        draws = [record for record in records if record.get("schema") == DRAW_SCHEMA]
        samples = [record for record in records if record.get("schema") == SAMPLE_SCHEMA]
        if len(draws) > 1:
            raise SchemaRefusal("the record directory contains more than one sampling draw")
        if len(draws) == 1:
            verify_recorded_draw(samples, draws[0], args.run)
            if args.catalog is not None:
                verify_stratified_selection(
                    samples,
                    args.run,
                    read_json(args.catalog),
                    read_json(args.plan),
                )
        else:
            if args.catalog is None:
                raise SchemaRefusal(
                    "no recorded sampling draw exists; both --catalog and --plan are required "
                    "to verify legacy sample records"
                )
            verify_stratified_selection(
                samples,
                args.run,
                read_json(args.catalog),
                read_json(args.plan),
            )
    elif args.command == "validate-corpus":
        corpus_records = _records_in(args.directory)
        # "I found nothing to check" must never wear the words "this corpus is
        # consistent": an empty or wrong directory is refused by name.
        if not corpus_records:
            raise SchemaRefusal(
                f"{args.directory} holds no gold records to validate; validate-corpus "
                "proves a corpus, not an empty directory"
            )
        validate_corpus(corpus_records, args.run)
    else:
        validate_record(read_json(args.record), args.run)
    return 0


if __name__ == "__main__":
    # A refusal is a statement to the operator, not a stack trace: the named
    # `SchemaRefusal` this module raises everywhere reaches the terminal as its
    # message and exit 2, exactly as `pipeline/orchestrator/run.py` does it.
    # `main()` still raises, so callers in-process keep the exception.
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
