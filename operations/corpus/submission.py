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
fetched; a page with no supplied bytes is refused `page-not-fetched`; two
identifiers whose fetched bytes are byte-identical are refused `duplicate-page-bytes`
on the second occurrence, per §5.1's dedupe-before-submit rule — a merged page is
unrecoverable at the Exemplar boundary (`HANDOFF.md:177-183`), so this is the last
place it can be caught cheaply. None of these refusals abort the whole build:
following rule 7, every refusal is recorded by name and the build proceeds around
it, the same shape `plan.py` already uses for a malformed row.

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

import os
from pathlib import Path
from typing import Any, NamedTuple

from common.contracts.canonical import digest_bytes, is_sha256
from operations.submit import gate, submit

from . import CorpusRefusal
from .holdout import refuse_held_out_page, validate_holdout
from .plan import validate_plan
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
        "malformed-record",
    }
)

DEFAULT_MAX_PAGES_PER_SHARD = 1000

_IMAGE_SUFFIX = ".jpg"


class FetchedPage(NamedTuple):
    """One already-cached page body, named by content — this unit's own contract.

    U2 (`operations/corpus/{fetch,cache}.py`) is the tracked producer of the
    `private/corpora/recordgold/cache/<response-sha256>.jpg` files `SPEC.md`
    §5.1 lays out, and it does not exist yet in this worktree. Rather than block
    on it or invent a fetcher of its own — out of scope for this unit, and the
    brief is explicit that a missing sibling gets worked around in this file and
    named, not silently assumed — this module takes the one fact it actually
    needs from a fetch: a local, already-cached JPEG file plus the response
    metadata `sidecar.build_sidecar`'s `iiif` block requires. Once U2 lands, its
    cache read is expected to construct exactly these tuples; nothing here reads
    `private/corpora/recordgold/cache/` directly, so no coupling to U2's
    internal layout is baked in.
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
    for root, _dirnames, filenames in os.walk(folder, followlinks=False):
        root_path = Path(root)
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


def _group_key(page: dict[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(sorted(page["splits_present"])),
        page["source"],
        page["volume"],
        page["designation"],
    )


def partition_into_shards(
    admitted: list[tuple[dict[str, Any], FetchedPage]],
    max_pages_per_shard: int = DEFAULT_MAX_PAGES_PER_SHARD,
) -> list[list[tuple[dict[str, Any], FetchedPage]]]:
    """Sort admitted pages by `(split, source, volume, designation)` and cap each slice.

    `SPEC.md` §5.2's `<=1000 pages` is the sealed cap (`common/stage.py`); this is
    a plain slice of the deterministically sorted list, not a bin-packing search —
    exact for page counts far below the cap, and it never mixes an ordering
    surprise into which pages land together.
    """
    if max_pages_per_shard <= 0:
        raise CorpusRefusal("malformed-record: max_pages_per_shard must be a positive integer")
    ordered = sorted(admitted, key=lambda pair: _group_key(pair[0]))
    return [
        ordered[start : start + max_pages_per_shard]
        for start in range(0, len(ordered), max_pages_per_shard)
    ]


def _admit_pages(
    plan: dict[str, Any],
    holdout: dict[str, Any],
    fetched_pages: dict[str, FetchedPage],
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
        seen_response_sha256[fetched.response_sha256] = identifier

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
    rows_by_id = {row["record_id"]: row for row in snapshot["rows"]}

    submissions_root = Path(submissions_root)
    sidecars_root = Path(sidecars_root)
    ledger_root = Path(ledger_root)

    policy = gate.load_policy(policy_path)
    roots = gate.approved_storage_roots(policy)
    gate.require_approved_storage_location(submissions_root, roots, "RecordGold submissions root")
    gate.require_approved_storage_location(sidecars_root, roots, "RecordGold sidecars root")
    gate.require_approved_storage_location(ledger_root, roots, "RecordGold submission ledger root")
    if gate.same_or_inside(submissions_root, sidecars_root) or gate.same_or_inside(
        sidecars_root, submissions_root
    ):
        raise CorpusRefusal(
            "malformed-record: the sidecars root must not be the submissions root or nest "
            "inside it — SPEC.md 5.1 requires sidecars outside the submission folder"
        )

    admitted, refusals = _admit_pages(plan, holdout, fetched_pages)
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
