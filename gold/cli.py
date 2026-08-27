"""Create and validate append-only R7a gold records."""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from common.contracts.errors import ContractError, SchemaRefusal

from .core import (
    DRAW_SCHEMA,
    SAMPLE_SCHEMA,
    _portable_name,
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


@dataclass(frozen=True)
class _CorpusDirectory:
    """One opened directory inode, optionally carrying its publication lock."""

    path: Path
    descriptor: int


def _open_corpus_directory(root: Path) -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise SchemaRefusal(
            "safe gold-directory access requires O_NOFOLLOW and O_DIRECTORY support"
        )
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY | no_follow | directory | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as error:
        raise SchemaRefusal(
            f"{root} is not a directory of gold records that can be opened without following links"
        ) from error
    try:
        details = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(details.st_mode):
        os.close(descriptor)
        raise SchemaRefusal(f"{root} is not a directory of gold records")
    return descriptor


def _records_in(directory: str | Path | _CorpusDirectory) -> list[dict[str, object]]:
    """Read regular, non-symlink records from one directory inode in stable order."""
    if isinstance(directory, _CorpusDirectory):
        corpus = directory
        owns_descriptor = False
    else:
        root = Path(directory)
        corpus = _CorpusDirectory(root, _open_corpus_directory(root))
        owns_descriptor = True
    try:
        try:
            names = [
                name
                for name in os.listdir(corpus.descriptor)
                if _portable_name(name).endswith(".json")
            ]
        except OSError as error:
            raise SchemaRefusal(
                f"the gold-record directory {corpus.path} could not be listed through its "
                "opened directory descriptor"
            ) from error
        seen: dict[str, str] = {}
        for name in names:
            portable = _portable_name(name)
            prior = seen.setdefault(portable, name)
            if prior != name:
                raise SchemaRefusal(
                    f"the gold-record directory {corpus.path} contains names that collide "
                    "by case or Unicode normalization; it is not portable to default APFS"
                )
        records = [
            read_json(
                name,
                directory_descriptor=corpus.descriptor,
                display_path=corpus.path / name,
            )
            for name in sorted(names)
        ]
    except BaseException:
        if owns_descriptor:
            try:
                os.close(corpus.descriptor)
            except OSError:
                # Preserve the refusal that stopped collection validation.
                pass
        raise
    else:
        if owns_descriptor:
            os.close(corpus.descriptor)
    return records


@contextmanager
def _locked_corpus(directory: str | Path) -> Iterator[_CorpusDirectory]:
    """Serialize validation and publication for one gold-record directory.

    A collection rule cannot be enforced by checking before an unlocked write:
    two writers can both validate the same old directory and then publish records
    whose union is contradictory. Lock the directory itself so no lock artifact
    becomes part of the corpus or needs its own cleanup and recovery semantics.
    """
    root = Path(directory)
    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor = _open_corpus_directory(root)
    except OSError as error:
        raise SchemaRefusal(
            f"the gold-record directory {root} could not be opened for a publication "
            "lock. Another writer therefore cannot be excluded, so publishing could "
            "create contradictory immutable records. Correct the directory path or "
            "permissions and retry; no gold record was written"
        ) from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise SchemaRefusal(
            f"the gold-record directory {root} could not acquire its publication lock. "
            "Another writer therefore cannot be excluded, so publishing could create "
            "contradictory immutable records. Use a local filesystem that supports "
            "advisory locks and retry; no gold record was written"
        ) from error
    try:
        opened = os.fstat(descriptor)
        try:
            named = os.stat(root, follow_symlinks=False)
        except OSError as error:
            raise SchemaRefusal(
                f"the gold-record directory {root} changed while its publication lock was "
                "being acquired; no redirected path was used"
            ) from error
        if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise SchemaRefusal(
                f"the gold-record directory {root} was replaced while its publication lock "
                "was being acquired; no redirected path was used"
            )
        yield _CorpusDirectory(root, descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            # Closing releases `flock`; if cleanup itself fails, preserve the
            # security refusal that already stopped publication.
            pass
        raise
    else:
        os.close(descriptor)


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
    adjudication.add_argument("--run")
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
        with _locked_corpus(args.output_dir) as output:
            existing = _records_in(output)
            # Validate the state the whole command would create before publishing
            # its first immutable byte. Closure is waived here for the same
            # reason as every other publication path: an act is legitimately
            # open while two readings are being collected, and adding a sample
            # neither closes nor threatens one. Closure is the collection
            # gate's rule -- `validate-corpus` still enforces it. This also makes an interrupted identical
            # run resumable: repeated identical records are reuse, and the union
            # includes every member the retained draw says is still missing.
            validate_corpus([*existing, draw, *selected], args.run, require_closure=False)
            # The draw is the membership authority, so it is published FIRST: an
            # interrupted run then leaves a draw whose members are partly missing --
            # which verify-sampling refuses by name -- never orphan samples with no
            # membership record to check them against.
            write_append_only(
                output.path / f"draw-{draw['self_hash']}.json",
                draw,
                directory_descriptor=output.descriptor,
            )
            for record in selected:
                write_append_only(
                    output.path / f"{record['sample_digest']}.json",
                    record,
                    directory_descriptor=output.descriptor,
                )
    elif args.command == "ingest-manual":
        record = ingest_manual_pick(args.run, read_json(args.pick))
        output = Path(args.output)
        with _locked_corpus(output.parent) as corpus:
            existing = _records_in(corpus)
            # A stratum is a collection fact, not a property R0 can derive from one
            # pick. Reconcile the destination corpus before publishing so a second
            # spelling of the same page is refused rather than counted twice -- any
            # second spelling, not only one that also restratifies the page, since
            # `sample_digest` binds `selection_basis` and a restated wording alone
            # mints a second individually valid sample of one hand-picked page.
            validate_corpus([*existing, record], args.run, require_closure=False)
            write_append_only(output, record, directory_descriptor=corpus.descriptor)
    elif args.command == "bind-instrument":
        output = Path(args.output)
        record = bind_instrument(
            read_json(args.sample), args.act_identity, args.protocol_digest, args.run
        )
        with _locked_corpus(output.parent) as corpus:
            # Reconcile against the corpus this record joins before publishing an
            # immutable byte. Closure is waived because an open custody chain is
            # the normal state here; every other collection rule still refuses.
            existing = _records_in(corpus)
            validate_corpus([*existing, record], args.run, require_closure=False)
            write_append_only(output, record, directory_descriptor=corpus.descriptor)
    elif args.command == "transcribe":
        output = Path(args.output)
        record = transcribe(
            read_json(args.sample),
            args.act_identity,
            args.transcriber,
            read_transcription_text(args.text_file),
            args.run,
        )
        with _locked_corpus(output.parent) as corpus:
            # Reconcile against the corpus this record joins before publishing an
            # immutable byte. Closure is waived because an open custody chain is
            # the normal state here; every other collection rule still refuses.
            existing = _records_in(corpus)
            validate_corpus([*existing, record], args.run, require_closure=False)
            write_append_only(output, record, directory_descriptor=corpus.descriptor)
    elif args.command == "adjudicate":
        output = Path(args.output)
        record = adjudicate(
            read_json(args.first),
            read_json(args.second),
            adjudicator=args.adjudicator,
            text=(read_transcription_text(args.text_file) if args.text_file is not None else None),
        )
        with _locked_corpus(output.parent) as corpus:
            # Reconcile against the corpus this record joins before publishing an
            # immutable byte. Closure is waived because an open custody chain is
            # the normal state here; every other collection rule still refuses.
            existing = _records_in(corpus)
            validate_corpus([*existing, record], args.run, require_closure=False)
            write_append_only(output, record, directory_descriptor=corpus.descriptor)
    elif args.command == "verify-sampling":
        if (args.catalog is None) != (args.plan is None):
            raise SchemaRefusal("--catalog and --plan must be supplied together")
        records = _records_in(args.directory)
        draws = [record for record in records if record.get("schema") == DRAW_SCHEMA]
        samples = [record for record in records if record.get("schema") == SAMPLE_SCHEMA]
        if len(draws) > 1:
            raise SchemaRefusal("the record directory contains more than one sampling draw")
        if draws:
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
                "checks a corpus, not an empty directory. Put one corpus's JSON records "
                "in that directory and retry"
            )
        validate_corpus(corpus_records, args.run)
    else:
        validate_record(read_json(args.record), args.run)
    return 0


if __name__ == "__main__":
    # CLI refusals are operator-facing messages with exit 2; in-process callers
    # still receive the exception from `main()`.
    try:
        raise SystemExit(main())
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2) from error
