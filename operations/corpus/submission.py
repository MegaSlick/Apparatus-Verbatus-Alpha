"""The submission builder: cached page bytes, hard-linked, sealed, and admitted.

`SPEC.md` §5.2 fixes the shape a Door admission needs and what must never appear in
it. This module builds that shape from three already-validated U1 artifacts (a
fetch plan, the row snapshot it was built from, and the hold-out ledger) plus one
input this unit supplies its own contract for, because no fetcher (U2) exists yet
in this worktree: `FetchedPage`, one already-cached page body per IIIF identifier,
named by content and never re-copied — see its docstring below for why this is a
locally-defined interface and not a defect worked around.

**Refusal order, in the sequence §5.2 and §5.4 name:** a page in the hold-out
ledger is refused by name (`holdout-page` or the stronger `cross-split-page`,
`holdout.refuse_held_out_page`) before this module ever asks whether the page was
fetched; a page carrying an unsafe `source`/`volume`/`designation` path segment is
refused `unsafe-identifier-segment` before anything is joined into a filesystem
path; a page naming a record absent from the row snapshot is refused
`record-not-in-row-snapshot` before any byte of it is written; a page with no
supplied bytes is refused `page-not-fetched`; two identifiers whose fetched bytes
are byte-identical are refused `duplicate-page-bytes` on the second occurrence,
per §5.1's dedupe-before-submit rule — a merged page is unrecoverable at the
Exemplar boundary (`HANDOFF.md:177-183`), so this is the last place it can be
caught cheaply — and a page is only registered against later duplicates once it
is fully admitted, so a page refused for an unrelated reason can never be named as
the "original" of someone else's `duplicate-page-bytes` refusal; and a page whose
decoded pixels disagree with the IIIF response's declared dimensions is refused
`dimension-mismatch`, because `record_url`'s `x,y,w,h` is defined in the declared
frame. None of these refusals abort the whole build: following rule 7, every
refusal is recorded by name and the build proceeds around it, the same shape
`plan.py` already uses for a malformed row.

**The plan, snapshot, and hold-out ledger must all be bound to the same row
snapshot.** Each of the three carries (or, for the snapshot, is) a
`source_row_snapshot_self_hash`/`self_hash`; `build_submission` refuses
`mismatched-row-snapshot` unless all three agree, because a hold-out ledger
derived from a different snapshot cannot be trusted to protect this plan's
held-out pages — §5.4's strongest mechanism is only as strong as the binding
that guarantees it was computed over the same rows.

**The submission folder carries images only, and this is checked, not assumed.**
`operations/submit/inventory.py` inventories every regular file under a submitted
folder and `submit.build_manifest` names each one as a submitted source
(`SPEC.md` §5.1: "the single most likely build mistake"). `refuse_non_image_files`
is the standing guard against that: it is called on every folder this module
writes, and it is also the function a caller (or a test) can run against a folder
someone else has touched, independent of a build.

**Sidecars never enter the submission folder.** They are written under a sibling
root (`sidecars/<shard-id>/...`), never under `submissions/<shard-id>/...` — two
disjoint trees rather than one tree with a filter, so there is no filename
convention standing between an accidental sidecar and a refused Door admission.

**Partitioning.** `SPEC.md` §5.2 caps a submission at 1000 pages and names
`(split, source, volume)` as the partition key; `content_aware_shards` is
explicitly not this problem (§4: "Unit 8's, it reasons about triage split pairs
... a RecordGold submission has none of"). This module sorts admitted pages by
that exact key (plus `designation`, for a fully deterministic order) and slices
the sorted list into shards of at most `max_pages_per_shard` — simple, exact for
this corpus's page counts (§5.6: val ≈ 225-315 pages, comfortably under the cap
in one shard), and it never invents a triage manifest to get there.
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any, NamedTuple

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from operations.submit import gate, submit

from . import CorpusRefusal
from .fetch import validate_fetch_log
from .holdout import load_holdout, refuse_held_out_page, validate_holdout
from .plan import _unsafe_segment, load_plan, validate_plan
from .rows import validate_snapshot
from .sidecar import build_sidecar, write_sidecar

SUBMISSION_REFUSAL_REASONS = frozenset(
    {
        "holdout-page",
        "cross-split-page",
        "page-not-fetched",
        "duplicate-page-bytes",
        "response-sha256-mismatch",
        "unrecognized-page-extension",
        "unexpected-file-in-submission-folder",
        "record-not-in-row-snapshot",
        "unsafe-identifier-segment",
        "dimension-mismatch",
        "mismatched-row-snapshot",
        "malformed-record",
    }
)

DEFAULT_MAX_PAGES_PER_SHARD = 1000

_IMAGE_SUFFIX = ".jpg"


class FetchedPage(NamedTuple):
    """One already-cached page body, named by content — this unit's own contract.

    U2 (`operations/corpus/{fetch,cache}.py`) is the tracked producer of the
    `private/corpora/recordgold/cache/<response-sha256>.jpg` files `SPEC.md`
    §5.1 lays out. It did not exist when this module was first built, so this
    stayed a locally-defined interface rather than a coupling to U2's cache
    layout: the one fact this module actually needs from a fetch is a local,
    already-cached JPEG file plus the response metadata `sidecar.build_sidecar`'s
    `iiif` block requires — nothing here reads
    `private/corpora/recordgold/cache/` directly.

    U2 exists now, and `integrate.fetched_pages_from_log` is its cache read: it
    turns a sealed `recordgold-fetch-log.v1` plus `cache_root` into exactly these
    tuples, verifying each cache file against the digest its own log entry
    declares. That coupling lives in `integrate.py`, not here, on purpose —
    see that module's docstring for why the boundary stayed put instead of
    collapsing into this file once U2 landed.
    """

    cache_path: Path
    info_url: str
    image_url: str
    size_parameter: str
    response_sha256: str
    bytes: int
    http_status: int
    fetched_at_utc: str
    declared_width: int
    declared_height: int
    width: int
    height: int


def refuse_non_image_files(folder: Path) -> None:
    """Refuse a submission folder that carries anything but `.jpg` regular files.

    `SPEC.md` §5.1: a sidecar or a stray file such as `.DS_Store` left inside the
    submission folder is named by `inventory.py` as a submitted source and the
    Door refuses it `unrecognized-format` — a failure this guard turns into a
    named, pre-submission refusal instead. Every entry is walked without
    following a symlink, the same discipline `inventory.py` uses for untrusted
    submitted material.
    """
    folder = Path(folder)
    for root, dirnames, filenames in os.walk(folder, followlinks=False):
        root_path = Path(root)
        for dirname in dirnames:
            candidate = root_path / dirname
            if candidate.is_symlink():
                raise CorpusRefusal(
                    f"unexpected-file-in-submission-folder: {candidate} is a symlinked "
                    "directory, which a submission folder may never carry"
                )
        for filename in filenames:
            candidate = root_path / filename
            if candidate.is_symlink():
                raise CorpusRefusal(
                    f"unexpected-file-in-submission-folder: {candidate} is a symlink, "
                    "which a submission folder may never carry"
                )
            if candidate.suffix.lower() != _IMAGE_SUFFIX:
                raise CorpusRefusal(
                    f"unexpected-file-in-submission-folder: {candidate} is not a "
                    f"{_IMAGE_SUFFIX!r} image; the submission folder carries images only"
                )


def _sort_key(page: dict[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(sorted(page["splits_present"])),
        page["source"],
        page["volume"],
        page["designation"],
    )


def _partition_key(page: dict[str, Any]) -> tuple[Any, ...]:
    """`(split, source, volume)` — the partition key `SPEC.md` §5.2 names.

    Deliberately excludes `designation`: that varies per page within a volume and
    would make every "group" exactly one page, defeating partitioning entirely.
    """
    return (tuple(sorted(page["splits_present"])), page["source"], page["volume"])


def partition_into_shards(
    admitted: list[tuple[dict[str, Any], FetchedPage]],
    max_pages_per_shard: int = DEFAULT_MAX_PAGES_PER_SHARD,
) -> list[list[tuple[dict[str, Any], FetchedPage]]]:
    """Sort admitted pages by `(split, source, volume, designation)` and cap each slice.

    `SPEC.md` §5.2's `<=1000 pages` is the sealed cap (`common/stage.py`) and names
    `(split, source, volume)` as the partition key: a shard boundary never crosses
    that group, so every shard carries pages from exactly one split and one
    source. Within a group this is a plain slice of the deterministically sorted
    list, not a bin-packing search — exact for page counts far below the cap.
    """
    if max_pages_per_shard <= 0:
        raise CorpusRefusal("malformed-record: max_pages_per_shard must be a positive integer")
    ordered = sorted(admitted, key=lambda pair: _sort_key(pair[0]))
    shards: list[list[tuple[dict[str, Any], FetchedPage]]] = []
    group_start = 0
    while group_start < len(ordered):
        group_key = _partition_key(ordered[group_start][0])
        group_end = group_start
        while group_end < len(ordered) and _partition_key(ordered[group_end][0]) == group_key:
            group_end += 1
        for start in range(group_start, group_end, max_pages_per_shard):
            shards.append(ordered[start : min(start + max_pages_per_shard, group_end)])
        group_start = group_end
    return shards


def _unsafe_page_segment(page: dict[str, Any]) -> str | None:
    """The first unsafe path segment `_page_relative_path` would carry, if any.

    `page["source"]` is third-party parquet data (`rows.py` checks only that it is
    non-empty) that this module joins straight into a filesystem path alongside
    `volume` and `designation`, which `plan.py`'s identifier parser has already
    screened. `source` never passes through that parser, so it is screened here,
    with the exact rule `plan._unsafe_segment` uses — split on `/` first, exactly
    as `volume` already is, because `Path(page["source"], ...)` treats an embedded
    `/` in a single caller-supplied string as more path components, not literal
    text: `"../../escaped"` is unsafe precisely because it *is* two `..`
    components once split, not because the whole string equals `".."`.
    """
    for segment in (
        *page["source"].split("/"),
        *page["volume"].split("/"),
        page["designation"],
    ):
        if _unsafe_segment(segment):
            return segment
    return None


def _admit_pages(
    plan: dict[str, Any],
    holdout: dict[str, Any],
    fetched_pages: dict[str, FetchedPage],
    rows_by_id: dict[str, dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], FetchedPage]], list[dict[str, Any]]]:
    admitted: list[tuple[dict[str, Any], FetchedPage]] = []
    refusals: list[dict[str, Any]] = []
    seen_response_sha256: dict[str, str] = {}

    for page in plan["pages"]:
        identifier = page["identifier"]

        try:
            refuse_held_out_page(holdout, identifier, page["splits_present"])
        except CorpusRefusal as error:
            reason = str(error).split(":", 1)[0]
            refusals.append({"identifier": identifier, "reason": reason, "detail": str(error)})
            continue

        unsafe = _unsafe_page_segment(page)
        if unsafe is not None:
            detail = (
                f"unsafe-identifier-segment: page {identifier!r} carries the unsafe path "
                f"segment {unsafe!r}; a submission path is never built from it"
            )
            refusals.append(
                {"identifier": identifier, "reason": "unsafe-identifier-segment", "detail": detail}
            )
            continue

        missing_record_id = next(
            (
                record["record_id"]
                for record in page["records"]
                if record["record_id"] not in rows_by_id
            ),
            None,
        )
        if missing_record_id is not None:
            detail = (
                f"record-not-in-row-snapshot: page {identifier!r} record "
                f"{missing_record_id!r} is not present in the row snapshot the plan was "
                "built from"
            )
            refusals.append(
                {
                    "identifier": identifier,
                    "reason": "record-not-in-row-snapshot",
                    "detail": detail,
                }
            )
            continue

        fetched = fetched_pages.get(identifier)
        if fetched is None:
            detail = f"page-not-fetched: no fetched page bytes were supplied for {identifier!r}"
            refusals.append(
                {"identifier": identifier, "reason": "page-not-fetched", "detail": detail}
            )
            continue

        if not is_sha256(fetched.response_sha256):
            raise CorpusRefusal(
                f"malformed-record: fetched page {identifier!r} carries a response_sha256 "
                "that is not a lowercase sha256 hex digest"
            )

        actual_digest = digest_bytes(Path(fetched.cache_path).read_bytes())
        if actual_digest != fetched.response_sha256:
            detail = (
                f"response-sha256-mismatch: {identifier!r} cache file digests to "
                f"{actual_digest!r}, declared {fetched.response_sha256!r}"
            )
            refusals.append(
                {"identifier": identifier, "reason": "response-sha256-mismatch", "detail": detail}
            )
            continue

        if fetched.response_sha256 in seen_response_sha256:
            detail = (
                f"duplicate-page-bytes: {identifier!r} shares response bytes with "
                f"{seen_response_sha256[fetched.response_sha256]!r}"
            )
            refusals.append(
                {"identifier": identifier, "reason": "duplicate-page-bytes", "detail": detail}
            )
            continue

        if not page["designation"].lower().endswith(_IMAGE_SUFFIX):
            detail = (
                f"unrecognized-page-extension: {identifier!r} designation "
                f"{page['designation']!r} does not end in {_IMAGE_SUFFIX!r}"
            )
            refusals.append(
                {
                    "identifier": identifier,
                    "reason": "unrecognized-page-extension",
                    "detail": detail,
                }
            )
            continue

        if fetched.width != fetched.declared_width or fetched.height != fetched.declared_height:
            detail = (
                f"dimension-mismatch: {identifier!r} decoded {fetched.width}x{fetched.height} "
                f"but the IIIF response declared {fetched.declared_width}x"
                f"{fetched.declared_height}; record_url regions are in the declared frame"
            )
            refusals.append(
                {"identifier": identifier, "reason": "dimension-mismatch", "detail": detail}
            )
            continue

        # Registered only once a page is fully admitted: a page refused for any
        # reason above must never make an unrelated later page's identical bytes
        # look like a duplicate of a page that was never actually submitted.
        seen_response_sha256[fetched.response_sha256] = identifier
        admitted.append((page, fetched))

    return admitted, refusals


def _sidecar_for_page(
    page: dict[str, Any], fetched: FetchedPage, rows_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    records = []
    for record in page["records"]:
        record_id = record["record_id"]
        row = rows_by_id.get(record_id)
        if row is None:
            raise CorpusRefusal(
                f"record-not-in-row-snapshot: page {page['identifier']!r} record "
                f"{record_id!r} is not present in the row snapshot the plan was built from"
            )
        records.append(
            {
                "record_id": record_id,
                "split": record["split"],
                "region": record["region"],
                "text": row["text"],
                "text_sha256": row["text_sha256"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "parish": row["parish"],
            }
        )
    iiif = {
        "identifier": page["identifier"],
        "info_url": page["info_url"],
        "image_url": fetched.image_url,
        "size_parameter": fetched.size_parameter,
        "response_sha256": fetched.response_sha256,
        "bytes": fetched.bytes,
        "http_status": fetched.http_status,
        "fetched_at_utc": fetched.fetched_at_utc,
        "declared_width": fetched.declared_width,
        "declared_height": fetched.declared_height,
    }
    page_facts = {
        "sha256": fetched.response_sha256,
        "width": fetched.width,
        "height": fetched.height,
    }
    return build_sidecar(
        source=page["source"],
        volume_id=page["volume"],
        designation=page["designation"],
        iiif=iiif,
        page=page_facts,
        splits_present=page["splits_present"],
        records=records,
    )


def _page_relative_path(page: dict[str, Any]) -> Path:
    return Path(page["source"], *page["volume"].split("/"), page["designation"])


def _sidecar_relative_path(page: dict[str, Any]) -> Path:
    stem = page["designation"][: -len(_IMAGE_SUFFIX)]
    return Path(page["source"], *page["volume"].split("/"), f"{stem}.json")


def _link_page_bytes(cache_path: Path, target: Path) -> None:
    """Hard-link `cache_path` into the submission tree — no second copy of the bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(cache_path, target)
    except FileExistsError:
        existing = digest_bytes(target.read_bytes())
        expected = digest_bytes(Path(cache_path).read_bytes())
        if existing != expected:
            raise CorpusRefusal(
                f"malformed-record: {target} already exists with different content than "
                f"{cache_path}; a submission tree is never overwritten"
            ) from None
        # Identical bytes already linked at this exact path — an idempotent rebuild.


def build_shard(
    pages: list[tuple[dict[str, Any], FetchedPage]],
    rows_by_id: dict[str, dict[str, Any]],
    *,
    shard_folder: Path,
    shard_sidecar_dir: Path,
) -> list[Path]:
    """Write one shard's images (hard-linked) and sidecars, then guard the folder.

    Returns the list of image paths written, sorted. Raises before any submit
    call if the folder ends up carrying anything but images — including a file
    planted there after this function returns, if the caller re-checks it.
    """
    written: list[Path] = []
    for page, fetched in pages:
        image_target = shard_folder / _page_relative_path(page)
        _link_page_bytes(Path(fetched.cache_path), image_target)
        written.append(image_target)

        sidecar = _sidecar_for_page(page, fetched, rows_by_id)
        sidecar_target = shard_sidecar_dir / _sidecar_relative_path(page)
        write_sidecar(sidecar_target, sidecar)

    refuse_non_image_files(shard_folder)
    return sorted(written)


def build_submission(
    plan: dict[str, Any],
    snapshot: dict[str, Any],
    holdout: dict[str, Any],
    fetched_pages: dict[str, FetchedPage],
    *,
    submissions_root: Path,
    sidecars_root: Path,
    ledger_root: Path,
    shard_prefix: str,
    max_pages_per_shard: int = DEFAULT_MAX_PAGES_PER_SHARD,
    policy_path: Path = gate.DEFAULT_POLICY_PATH,
) -> dict[str, Any]:
    """Build every shard from `plan`, sealing each through the real submit door.

    `plan`, `snapshot`, and `holdout` are re-validated here rather than trusted as
    already-checked: this is the boundary where a caller's artifact becomes real
    filesystem writes and an external `submit()` call, and nothing downstream of
    that boundary may run against an unverified ledger.
    """
    plan = validate_plan(plan)
    snapshot = validate_snapshot(snapshot)
    holdout = validate_holdout(holdout)
    if plan["source_row_snapshot_self_hash"] != snapshot["self_hash"]:
        raise CorpusRefusal(
            "mismatched-row-snapshot: the fetch plan was built from row snapshot "
            f"{plan['source_row_snapshot_self_hash']!r}, not the supplied snapshot "
            f"{snapshot['self_hash']!r} — a plan and a snapshot must be bound to the "
            "same row snapshot"
        )
    if holdout["source_row_snapshot_self_hash"] != snapshot["self_hash"]:
        raise CorpusRefusal(
            "mismatched-row-snapshot: the hold-out ledger was built from row snapshot "
            f"{holdout['source_row_snapshot_self_hash']!r}, not the supplied snapshot "
            f"{snapshot['self_hash']!r} — a ledger derived from a different snapshot cannot "
            "be trusted to protect this plan's held-out pages"
        )
    rows_by_id = {row["record_id"]: row for row in snapshot["rows"]}

    submissions_root = Path(submissions_root)
    sidecars_root = Path(sidecars_root)
    ledger_root = Path(ledger_root)

    policy = gate.load_policy(policy_path)
    roots = gate.approved_storage_roots(policy)
    gate.require_approved_storage_location(submissions_root, roots, "RecordGold submissions root")
    gate.require_approved_storage_location(sidecars_root, roots, "RecordGold sidecars root")
    gate.require_approved_storage_location(ledger_root, roots, "RecordGold submission ledger root")
    abs_submissions_root = Path(os.path.abspath(submissions_root))
    abs_sidecars_root = Path(os.path.abspath(sidecars_root))
    nested_by_identity = gate.same_or_inside(
        submissions_root, sidecars_root
    ) or gate.same_or_inside(sidecars_root, submissions_root)
    # `gate.same_or_inside` decides by inode and answers False for either root
    # before it exists — exactly the state on a first build, since both are only
    # created later by `mkdir` inside `_link_page_bytes`/`write_sidecar`. A plain
    # spelling comparison of the already-`abspath`'d paths answers before either
    # directory is on disk.
    nested_by_spelling = abs_submissions_root == abs_sidecars_root or (
        abs_sidecars_root in abs_submissions_root.parents
        or abs_submissions_root in abs_sidecars_root.parents
    )
    if nested_by_identity or nested_by_spelling:
        raise CorpusRefusal(
            "malformed-record: the sidecars root must not be the submissions root or nest "
            "inside it — SPEC.md 5.1 requires sidecars outside the submission folder"
        )

    admitted, refusals = _admit_pages(plan, holdout, fetched_pages, rows_by_id)
    shards = partition_into_shards(admitted, max_pages_per_shard)

    shard_reports: list[dict[str, Any]] = []
    for index, shard_pages in enumerate(shards, start=1):
        shard_id = f"{shard_prefix}-{index:04d}"
        shard_folder = submissions_root / shard_id
        shard_sidecar_dir = sidecars_root / shard_id
        written = build_shard(
            shard_pages,
            rows_by_id,
            shard_folder=shard_folder,
            shard_sidecar_dir=shard_sidecar_dir,
        )
        manifest_out = ledger_root / f"{shard_id}.manifest.json"
        manifest = submit.submit(shard_folder, manifest_out, policy_path=policy_path)
        shard_reports.append(
            {
                "shard_id": shard_id,
                "folder": str(shard_folder),
                "sidecar_dir": str(shard_sidecar_dir),
                "manifest_path": str(manifest_out),
                "manifest_self_hash": manifest["self_hash"],
                "page_identifiers": sorted(page["identifier"] for page, _fetched in shard_pages),
                "image_count": len(written),
            }
        )

    return {
        "schema": "recordgold-submission-report.v1",
        "shards": shard_reports,
        "refusals": sorted(refusals, key=lambda entry: (entry["reason"], entry["identifier"])),
        "admitted_page_count": len(admitted),
        "refused_page_count": len(refusals),
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Build a real submission from the CLI — no throwaway script, no network.

    Loads and validates every ledger this module needs through its own tracked
    loader (`rows.validate_snapshot`, `plan.load_plan`, `holdout.load_holdout`,
    `fetch.validate_fetch_log`), builds `FetchedPage`s from the fetch log via
    `integrate.fetched_pages_from_log` — imported here, not at module load time,
    so `submission.py` never carries a circular import back to `integrate.py`
    merely to run this CLI — and calls `build_submission`. Every refusal
    `build_submission` and `fetched_pages_from_log` raise is a `CorpusRefusal`
    named by its own leading token; this function catches none of them; a bad
    ledger stops the run rather than producing a silently partial submission.

    The finished report — shards written, pages admitted, every refusal by name
    — is written to `<ledger_root>/<shard_prefix>-submission-report.json`
    (`canonical_bytes`, so it is a stable function of its content) and also
    printed, so an operator sees the outcome without opening a file.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Path to a recordgold-rows.v1 file.")
    parser.add_argument("--plan", required=True, help="Path to a recordgold-fetch-plan.v1 file.")
    parser.add_argument("--holdout", required=True, help="Path to a recordgold-holdout.v1 file.")
    parser.add_argument(
        "--fetch-log", required=True, help="Path to a recordgold-fetch-log.v1 file (U2's output)."
    )
    parser.add_argument(
        "--cache-root", required=True, help="U2's cache root: cache/<response-sha256>.jpg."
    )
    parser.add_argument("--submissions-root", required=True)
    parser.add_argument("--sidecars-root", required=True)
    parser.add_argument("--ledger-root", required=True)
    parser.add_argument(
        "--shard-prefix", required=True, help="e.g. 'val' — shards land as <prefix>-0001, ..."
    )
    args = parser.parse_args(argv)

    # Deferred: `integrate.py` imports `FetchedPage` from this module, so
    # importing it back at this module's top level would be circular. By the
    # time this function runs, `submission.py` has already finished defining
    # everything `integrate.py` needs, so the import resolves cleanly here.
    from .integrate import fetched_pages_from_log

    snapshot = validate_snapshot(json.loads(Path(args.snapshot).read_bytes()))
    plan = load_plan(args.plan)
    holdout = load_holdout(args.holdout)
    fetch_log = validate_fetch_log(json.loads(Path(args.fetch_log).read_bytes()))
    fetched_pages = fetched_pages_from_log(fetch_log, Path(args.cache_root))

    ledger_root = Path(args.ledger_root)
    report = build_submission(
        plan,
        snapshot,
        holdout,
        fetched_pages,
        submissions_root=Path(args.submissions_root),
        sidecars_root=Path(args.sidecars_root),
        ledger_root=ledger_root,
        shard_prefix=args.shard_prefix,
    )

    report_path = ledger_root / f"{args.shard_prefix}-submission-report.json"
    ledger_root.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(canonical_bytes(report))
    print(json.dumps(report, indent=2, sort_keys=True))
    return report


if __name__ == "__main__":
    main()
