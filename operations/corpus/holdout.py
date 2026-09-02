"""The hold-out ledger: which pages and records the `test` split protects.

Built from the row snapshot alone — `SPEC.md` §5.4's strongest mechanism, ahead of
"the fetcher defaults to `val`" and "the submission builder refuses by name": an
identifier ledger derivable before a single byte is fetched, so a page can never
even be requested under the wrong split by a builder that forgot a flag.

Every IIIF identifier carrying at least one `test`-split record is *held*.
`refuse_held_out_page` is the predicate a later unit (the submission builder, U3)
calls before writing a page into a submission folder — it never returns a reading,
never picks among candidates, it only says whether a page may proceed, so it
refuses rather than answers. Two distinct refusals, both closed vocabulary from
`SPEC.md` §5.1: `holdout-page` for a page that is nothing but held-out material,
and the stronger `cross-split-page` for a page that also carries a non-held
split's records — the case `SPEC.md` §5.4 names explicitly ("a page carrying test
records cannot be used for calibration without exposing held-out material").

Measured from the real snapshot (§5.5's "before a byte is fetched" promise): the
three splits are **page-disjoint** in this export — no identifier in `val` or
`train` also carries a `test` record — so `cross-split-page` never fires against
real data today. It is still load-bearing, not decorative: `SPEC.md` §6 names the
disjointness as unverified until measured, and this file's own tests exercise it
against a synthetic cross-split page precisely because the real corpus cannot.

"Append-only" (`SPEC.md` §5.1) means this ledger is only ever *derived*, never
hand-edited: rebuilding it from one row snapshot is deterministic and idempotent
— the same snapshot always produces byte-identical bytes and the same self-hash —
and shrinking or growing the hold-out set means producing a new row snapshot, not
patching this file in place.
"""

import json
from pathlib import Path
from typing import Any

from common.contracts.canonical import canonical_bytes, is_sha256, self_hash, verify_self_hash

from . import CorpusRefusal
from .plan import parse_record_url
from .rows import CORPUS_ID, validate_snapshot

SCHEMA = "recordgold-holdout.v1"
HELD_SPLIT = "test"

HOLDOUT_REFUSAL_REASONS = frozenset(
    {
        "holdout-page",
        "cross-split-page",
        "malformed-record",
        "wrong-schema",
        "wrong-corpus",
        "self-hash-mismatch",
    }
)

_ENTRY_FIELDS = frozenset({"identifier", "record_ids"})
_TOP_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "source_row_snapshot_self_hash",
        "held_identifiers",
        "held_record_ids",
        "entries",
        "self_hash",
    }
)


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def build_holdout(rows: list[dict[str, Any]], source_row_snapshot_self_hash: str) -> dict[str, Any]:
    """Derive the hold-out ledger from row-snapshot dicts alone — no fetch, no plan."""
    entries: dict[str, set[str]] = {}
    for row in rows:
        if row.get("split") != HELD_SPLIT:
            continue
        record_id = row["record_id"]
        parsed = parse_record_url(row["record_url"])
        entries.setdefault(parsed.identifier, set()).add(record_id)

    held_identifiers = sorted(entries)
    held_record_ids = sorted(
        {record_id for record_ids in entries.values() for record_id in record_ids}
    )

    body = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "source_row_snapshot_self_hash": source_row_snapshot_self_hash,
        "held_identifiers": held_identifiers,
        "held_record_ids": held_record_ids,
        "entries": [
            {"identifier": identifier, "record_ids": sorted(entries[identifier])}
            for identifier in held_identifiers
        ],
    }
    body["self_hash"] = self_hash(body)
    return validate_holdout(body)


def validate_holdout(holdout: Any) -> dict[str, Any]:
    """Refuse a ledger that is not exactly `recordgold-holdout.v1`, closed and self-consistent."""
    holdout = _closed(holdout, _TOP_FIELDS, "hold-out ledger")
    if holdout["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {holdout['schema']!r}")
    if holdout["corpus_id"] != CORPUS_ID:
        raise CorpusRefusal(f"wrong-corpus: expected {CORPUS_ID!r}, got {holdout['corpus_id']!r}")
    if not is_sha256(holdout["source_row_snapshot_self_hash"]):
        raise CorpusRefusal(
            "malformed-record: source_row_snapshot_self_hash must be a lowercase sha256 hex digest"
        )

    identifiers = holdout["held_identifiers"]
    if not isinstance(identifiers, list) or identifiers != sorted(set(identifiers)):
        raise CorpusRefusal(
            "malformed-record: held_identifiers must be a sorted, deduplicated list"
        )

    entries = holdout["entries"]
    if (
        not isinstance(entries, list)
        or [entry.get("identifier") for entry in entries if isinstance(entry, dict)] != identifiers
    ):
        raise CorpusRefusal(
            "malformed-record: entries must list held_identifiers, in the same order"
        )

    seen_record_ids: set[str] = set()
    for entry in entries:
        entry = _closed(entry, _ENTRY_FIELDS, "hold-out entry")
        record_ids = entry["record_ids"]
        if (
            not isinstance(record_ids, list)
            or not record_ids
            or record_ids != sorted(set(record_ids))
        ):
            raise CorpusRefusal(
                f"malformed-record: entry {entry['identifier']!r} record_ids must be a "
                "sorted, deduplicated, non-empty list"
            )
        seen_record_ids.update(record_ids)

    if holdout["held_record_ids"] != sorted(seen_record_ids):
        raise CorpusRefusal(
            "malformed-record: held_record_ids does not match the union of every entry's record_ids"
        )

    if not verify_self_hash(holdout):
        raise CorpusRefusal(
            "self-hash-mismatch: hold-out ledger self_hash does not verify against its own content"
        )
    return holdout


def refuse_held_out_page(
    holdout: dict[str, Any], identifier: str, splits_present: list[str]
) -> None:
    """Refuse a page a builder must not use, by name — or return, allowing it.

    `splits_present` is the caller's own claim about the page (e.g. a fetch-plan
    entry's `splits_present`, or a sidecar's); this function does not recompute
    it from anything, it only decides what the claim means against the ledger.
    """
    if identifier not in set(holdout["held_identifiers"]):
        return
    other_splits = sorted(set(splits_present) - {HELD_SPLIT})
    if other_splits:
        raise CorpusRefusal(
            f"cross-split-page: identifier {identifier!r} is held for the {HELD_SPLIT!r} "
            f"split but also carries {other_splits} — it cannot be used for calibration "
            "without exposing held-out material"
        )
    raise CorpusRefusal(
        f"holdout-page: identifier {identifier!r} is held for the {HELD_SPLIT!r} split"
    )


def load_holdout(path: str | Path) -> dict[str, Any]:
    """Read a hold-out ledger written by `main`, refusing one that fails to validate.

    A builder must never read an unverified ledger: this is the only tracked way
    to load a `recordgold-holdout.v1` file back, and it runs the ledger through
    `validate_holdout` before returning it.
    """
    return validate_holdout(json.loads(Path(path).read_bytes()))


def main(snapshot_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    """Build the hold-out ledger from a validated row snapshot on disk and write it out.

    The only tracked producer of `holdout.json`: without this, the artifact can
    only be regenerated by a throwaway script outside the repository. Writes
    `canonical_bytes`, not `json.dumps`, so the file on disk is a stable function
    of the ledger's content.
    """
    snapshot = validate_snapshot(json.loads(Path(snapshot_path).read_bytes()))
    holdout = build_holdout(snapshot["rows"], snapshot["self_hash"])
    Path(output_path).write_bytes(canonical_bytes(holdout))
    return holdout


if __name__ == "__main__":
    import sys

    main(sys.argv[1], sys.argv[2])
