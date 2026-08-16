"""Create and validate append-only R7a gold records."""

from __future__ import annotations

import argparse
from pathlib import Path

from .core import (
    bind_instrument,
    ingest_manual_pick,
    read_json,
    sample_stratified,
    validate_layout,
    validate_measurement,
    validate_padding,
    validate_sample,
    write_append_only,
)


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
            bind_instrument(read_json(args.sample), args.act_identity, args.protocol_digest),
        )
    else:
        record = read_json(args.record)
        schema = record.get("schema") if isinstance(record, dict) else None
        validators = {
            "gold-page-sample.v1": validate_sample,
            "gold-page-layout.v1": validate_layout,
            "gold-padding-rectangles.v1": validate_padding,
            "gold-instrument-membership.v1": validate_measurement,
        }
        if schema not in validators:
            parser.error("record schema is not a gold schema")
        if schema == "gold-page-sample.v1":
            validate_sample(record, args.run)
        else:
            validators[schema](record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
