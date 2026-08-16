"""Create and validate append-only R7a gold records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.contracts.errors import ContractError, SchemaRefusal

from .core import (
    adjudicate,
    bind_instrument,
    ingest_manual_pick,
    read_json,
    read_transcription_text,
    sample_stratified,
    transcribe,
    validate_corpus,
    validate_record,
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
    verify.add_argument("--catalog", required=True)
    verify.add_argument("--plan", required=True)
    corpus = commands.add_parser("validate-corpus")
    corpus.add_argument("directory")
    corpus.add_argument("--run")
    validate = commands.add_parser("validate")
    validate.add_argument("record")
    validate.add_argument("--run")
    args = parser.parse_args(argv)
    if args.command == "sample":
        for record in sample_stratified(args.run, read_json(args.catalog), read_json(args.plan)):
            write_append_only(Path(args.output_dir) / f"{record['sample_digest']}.json", record)
    elif args.command == "ingest-manual":
        write_append_only(args.output, ingest_manual_pick(args.run, read_json(args.pick)))
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
        verify_stratified_selection(
            _records_in(args.directory),
            args.run,
            read_json(args.catalog),
            read_json(args.plan),
        )
    elif args.command == "validate-corpus":
        validate_corpus(_records_in(args.directory), args.run)
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
