"""Admit the RecordGold pages and records already on this machine as reference truth.

**The producer of these sets is outside this repository and unverified.** Each
set is a directory holding a `pages/` folder, a `page_manifest.jsonl`, a
`gold.jsonl` and a `fetch_receipt.json` naming their digests. `fetch.py` did not
write them: it writes a content-addressed cache, a `fetch-log.json` and a
`refusals.json`, and none of those four names. The only identity the material
carries is the receipt's own schema string, `recordgold_full_page_fetch_v1`,
which appears nowhere else in this repository and is therefore a label rather
than a contract this tree can check. So this module trusts the receipt for one
thing only -- that `gold.jsonl` and `page_manifest.jsonl` are the exact bytes it
names -- and measures everything else against the stored pixels, against the
`record_url` the row itself carries, and, when the caller supplies one, against
the sealed `recordgold-rows.v1` snapshot. No sealed snapshot is tracked in this
repository (it lives under `private/corpora/recordgold/rows/`, gitignored by
design), so unless `--row-snapshot` names one, the receipt is the only witness
to the corpus facts and the ledger says so in `row_snapshot`.

Forty of the records are stated in a 180-degree IIIF view: `record_url` carries
`/180/`, `source_bbox` is the box in that rotated frame, and `bbox` is the same
box carried into the stored page's frame by `(W - x - w, H - y - h, w, h)`.
`plan.py`'s fetch-time parser refuses those rows by name, correctly, because at
fetch time it holds no page dimensions to convert with. This module does hold
them -- the stored page is decoded and measured before any record on it is
admitted -- so it carries the box across and records every fact of the crossing:
the original URL, the rotation, the original box, the transformed box, the
page's digest and dimensions. Nothing is treated as unrotated that was not; a
rotation outside `0`/`180` stays a named refusal (independent audit of
2026-09-10, finding F4).

**Read-only over the set.** Nothing here writes into a set root -- an output
directory inside one is refused by name -- and the ledger and the reference
pages go where the caller says. The set's own receipt digests are checked first,
so a ledger is bound to the exact `gold.jsonl` and `page_manifest.jsonl` bytes it
was built from, and the receipt's own bytes are digested into the ledger beside
them.

**Every row ends admitted or refused, by name.** A page-level refusal (a
missing or undecodable image, dimensions that disagree with the manifest, an
EXIF orientation) refuses every record on that page; a record-level refusal
refuses that record alone and the page's reference truth is built from the
rest, with the refused ids beside it. The denominator is the whole set -- 784
validation records, 6,178 training records -- and the summary counts both
sides, so a later trial can name its population instead of quietly claiming
the corpus. Every listed page ends in exactly one outcome too, and the ledger
refuses to validate if the outcomes do not account for the manifest.

**The held-out split is not admitted by accident.** `split="test"` is the
DAI-comparability set `holdout.py` protects; it is admitted only when the caller
passes `release_test_split`, exactly as `fetch.py` requires `--release-test-split`,
and that flag releases the held split alone. It is the same condition as the
fetcher's, so it refuses under the fetcher's own name --
`holdout-ledger-required` -- rather than inventing a second word for one concept.
This route reproduces that layer of the hold-out and not the other two: it never
consults the hold-out ledger, because the sets it reads carry their own split
labels and were not produced by the fetcher. `README.md`'s hold-out section says
so.

Reference truth built here is `reference.py`'s family and nothing more: one
unnamed expert reading, records-only completeness, no adjudication. The
rotation provenance does not fit that closed schema and is not forced into it;
it lives in this module's ledger rows, keyed by `record_id` and
`physical_act_id`.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    is_sha256,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import IdentityRefusal
from common.contracts.identities import physical_act_id, physical_page_id
from operations.spike_perlector.normalization import GRAPHEMIC_V1, character_units

from . import CorpusRefusal
from .holdout import HELD_SPLIT
from .plan import SUPPORTED_ROTATIONS, parse_record_url, unsafe_segment, volume_and_designation
from .reference import CORPUS_ID, SPLITS, build_reference_page, validate_reference_page
from .rows import validate_snapshot

SCHEMA = "recordgold-local-admission.v1"
RECEIPT_SCHEMA = "recordgold_full_page_fetch_v1"
_EXIF_ORIENTATION_TAG = 0x0112

LOCAL_ADMISSION_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "receipt-hash-mismatch",
        "missing-set-file",
        "unknown-split",
        "holdout-ledger-required",
        "split-not-requested",
        "missing-page-file",
        "non-image-body",
        "dimension-mismatch",
        "exif-orientation",
        "inconsistent-page-manifest",
        "unsafe-page-image-path",
        "unsupported-rotation-parameter",
        "unparseable-record-url",
        "unexpected-host",
        "unsupported-size-parameter",
        "unsupported-quality-parameter",
        "unsupported-format-parameter",
        "non-positive-region",
        "unsafe-identifier-segment",
        "unsafe-source-value",
        "empty-normalized-text",
        "snapshot-mismatch",
        "inconsistent-transform",
        "region-outside-page",
        "duplicate-record-id",
        "unmintable-page-identity",
        "reference-build-refused",
        "output-inside-set",
        "wrong-schema",
        "wrong-corpus",
        "self-hash-mismatch",
    }
)

_GOLD_ROW_FIELDS = frozenset(
    {
        "end_date",
        "parish",
        "record_id",
        "record_url",
        "source",
        "split",
        "start_date",
        "text",
        "bbox",
        "source_bbox",
        "iiif_rotation",
        "page_id",
    }
)

# One closed row shape for both decisions. A refused row carries `None` where an
# admitted row carries a measurement, so every row answers the same questions and
# a validator can hold them all to one set of keys (independent audit of
# 2026-09-11, finding 9).
_LEDGER_ROW_FIELDS = frozenset(
    {
        "record_id",
        "page_id",
        "split",
        "record_url",
        "iiif_rotation",
        "source_bbox",
        "bbox",
        "page_sha256",
        "page_width",
        "page_height",
        "page_image",
        "physical_page_id",
        "physical_act_id",
        "reference_page_self_hash",
        "decision",
        "reason",
        "detail",
    }
)

_RECEIPT_FIELDS = frozenset({"path", "receipt_sha256", "status", "requested_splits", "digests"})

_SUMMARY_FIELDS = frozenset(
    {
        "records",
        "admitted",
        "refused",
        "refused_by_reason",
        "pages_listed",
        "pages_by_outcome",
        "admitted_by_rotation",
    }
)

_ROW_SNAPSHOT_FIELDS = frozenset({"consulted", "self_hash", "records_cross_checked"})

_TOP_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "set_root",
        "split",
        "receipt",
        "row_snapshot",
        "summary",
        "rows",
        "reference_pages",
        "self_hash",
    }
)

# Every outcome a listed page can reach, closed and reconciled against
# `pages_listed`: a page that produced no reference truth is named by why rather
# than left out of both buckets (independent audit of 2026-09-11, finding 23 and
# the CodeRabbit page-accounting finding on the same lines).
_PAGE_OUTCOMES = ("admitted", "refused", "no-gold-row", "all-records-refused")


def transform_region(
    region: dict[str, int], *, rotation: str, width: int, height: int
) -> dict[str, int]:
    """Carry a box stated in a rotated IIIF view into the stored page's frame.

    `width`/`height` are the **stored page's** dimensions, and the stored page is
    the upright, already-180-delivered view of the archive image. That frame was
    verified against real pixels on 2026-09-11 for record
    `07291eae-33d8-4b6b-8088-e761c9259db0`: the flipped box cuts the ink
    `gold.jsonl` transcribes, and the unflipped box cuts a different part of the
    page. The box arrives stated in the other frame, which the `/180/` in
    `record_url` names.

    `0` is the identity. `180` maps `(x, y, w, h)` to `(W - x - w, H - y - h, w,
    h)`: the two frames are one point reflection apart, so a box's far corner in
    one is its near corner in the other. Nothing else is defined here; `90` and
    `270` swap the axes and their box would need the *rotated* frame's
    dimensions, which no local record states, so they are refused by name.
    """
    if rotation == "0":
        return dict(region)
    if rotation == "180":
        return {
            "x": width - region["x"] - region["w"],
            "y": height - region["y"] - region["h"],
            "w": region["w"],
            "h": region["h"],
        }
    raise CorpusRefusal(
        f"unsupported-rotation-parameter: {rotation!r} has no frame conversion here; only "
        f"{sorted(SUPPORTED_ROTATIONS)!r} do"
    )


def _reason(error: Exception) -> str:
    return str(error).split(":", 1)[0]


def _read_text(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise CorpusRefusal(f"malformed-record: {what} ({path}) is not UTF-8: {error}") from error


def _load_jsonl(path: Path, what: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(_read_text(path, what).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as error:
            raise CorpusRefusal(
                f"malformed-record: {what} line {number} is not JSON: {error}"
            ) from error
        if not isinstance(row, dict):
            raise CorpusRefusal(f"malformed-record: {what} line {number} is not an object")
        rows.append(row)
    return rows


def _receipt(set_root: Path) -> dict[str, Any]:
    """The set's own receipt, and proof its two files are the bytes it names."""
    receipt_path = set_root / "fetch_receipt.json"
    if not receipt_path.is_file():
        raise CorpusRefusal(f"missing-set-file: {receipt_path} is not a file")
    receipt_body = receipt_path.read_bytes()
    try:
        receipt = json.loads(receipt_body)
    except ValueError as error:
        raise CorpusRefusal(f"malformed-record: {receipt_path} is not JSON: {error}") from error
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        raise CorpusRefusal(f"malformed-record: {receipt_path} does not declare {RECEIPT_SCHEMA!r}")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CorpusRefusal(f"malformed-record: {receipt_path} names no artifacts")
    digests = {}
    for name, filename in (
        ("gold_jsonl", "gold.jsonl"),
        ("page_manifest_jsonl", "page_manifest.jsonl"),
    ):
        declared = artifacts.get(f"{name}_sha256")
        if not (set_root / filename).is_file():
            raise CorpusRefusal(
                f"missing-set-file: {set_root / filename} is not a file; the set cannot be "
                "admitted without it"
            )
        actual = digest_bytes((set_root / filename).read_bytes())
        if not is_sha256(declared) or declared != actual:
            raise CorpusRefusal(
                f"receipt-hash-mismatch: {filename} digests to {actual}, the receipt declares "
                f"{declared!r}"
            )
        digests[filename] = actual
    # The producer of these sets is outside this repository, so a field of the
    # wrong JSON type is exactly what this boundary exists to name: held to a
    # shape here, never carried into the ledger to fail its sealing later.
    status = receipt.get("status")
    requested = receipt.get("requested_splits")
    if not isinstance(status, str) or not status:
        raise CorpusRefusal(
            f"malformed-record: {receipt_path} declares status {status!r}, not a non-empty string"
        )
    if not isinstance(requested, list) or not all(isinstance(item, str) for item in requested):
        raise CorpusRefusal(
            f"malformed-record: {receipt_path} declares requested_splits {requested!r}, "
            "not a list of strings"
        )
    return {
        "path": str(receipt_path),
        # The receipt's own bytes, so the ledger's chain closes back to the file
        # that made the claim and not only to the two files it named.
        "receipt_sha256": digest_bytes(receipt_body),
        "status": status,
        "requested_splits": requested,
        "digests": digests,
    }


def _decode_page(body: bytes, *, width: int, height: int) -> None:
    """Decode the stored page and hold it to the manifest's own frame.

    The same three refusals `fetch.py` applies at fetch time, applied again at
    admission: the manifest's width/height are the frame every `bbox` is stated
    in, and a page that no longer decodes to them, or that carries an EXIF
    display rotation, would put every box on it in the wrong frame. There is no
    switch to skip this: a ledger built with the check off would be
    indistinguishable from one built with it on (GOVERNANCE 10).
    """
    from PIL import Image

    try:
        image = Image.open(io.BytesIO(body))
        image.load()
    except Exception as error:
        raise CorpusRefusal(f"non-image-body: failed to decode as an image: {error}") from error
    if image.size != (width, height):
        raise CorpusRefusal(
            f"dimension-mismatch: decoded image is {image.size[0]}x{image.size[1]}, the page "
            f"manifest declares {width}x{height}"
        )
    orientation = image.getexif().get(_EXIF_ORIENTATION_TAG)
    if orientation is not None and orientation != 1:
        raise CorpusRefusal(
            f"exif-orientation: decoded image declares EXIF orientation {orientation}, only "
            "absent or 1 is accepted"
        )


def _page_image_path(set_root: Path, image_rel: Any, page_id: str) -> Path:
    """The manifest's `image` resolved inside the set, or a named refusal.

    `plan.py`'s segment rule applied to a manifest field: an absolute path, a
    `..` or control-character segment, or a resolved path that leaves the set
    root is refused rather than read. Without this a crafted or damaged manifest
    makes admission hash a file outside the set it was pointed at and record its
    size in the ledger (independent audit of 2026-09-11, finding 8).
    """
    if not isinstance(image_rel, str) or not image_rel:
        raise CorpusRefusal(f"malformed-record: page {page_id!r} names no image")
    candidate = Path(image_rel)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise CorpusRefusal(
            f"unsafe-page-image-path: page {page_id!r} names the absolute image path "
            f"{image_rel!r}; a set's image is always relative to the set root"
        )
    for segment in candidate.parts:
        if unsafe_segment(segment):
            raise CorpusRefusal(
                f"unsafe-page-image-path: page {page_id!r} names image {image_rel!r}, whose "
                f"segment {segment!r} this module refuses rather than normalises"
            )
    resolved = (set_root / candidate).resolve()
    root = set_root.resolve()
    if root not in resolved.parents:
        raise CorpusRefusal(
            f"unsafe-page-image-path: page {page_id!r} resolves image {image_rel!r} to "
            f"{resolved}, which is outside the set root {root}"
        )
    return set_root / candidate


def _identity(value: Any) -> str | None:
    """An identity field as the ledger stores it: the value's text, or `None` when absent."""
    return None if value is None else str(value)


def _canonical_safe(value: Any) -> Any:
    """A raw field reduced to something `canonical_bytes` can actually seal.

    A refused row copies the record's own values out of the file so a reader can
    see what was refused. `canonical_bytes` refuses a float outright, so one
    `239.0` in a `bbox` -- correctly refused by name a moment earlier -- would
    then abort the whole admission inside `self_hash(ledger)` with a bare
    `TypeError` naming no record at all. Anything the canonical serialization
    does not carry becomes its `repr`; the refusal's own detail string already
    holds the offending value verbatim (independent audit of 2026-09-11, round 2
    item 2).
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, list):
        return [_canonical_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical_safe(item) for key, item in value.items()}
    return repr(value)


def _positive_int(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CorpusRefusal(f"malformed-record: {what} must be a positive integer, got {value!r}")
    return value


def _box(value: Any, what: str) -> dict[str, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise CorpusRefusal(f"malformed-record: {what} must be four integers, got {value!r}")
    x, y, w, h = value
    return {"x": x, "y": y, "w": w, "h": h}


def _snapshot_index(row_snapshot: Any) -> tuple[dict[str, dict[str, Any]], str] | None:
    """The sealed corpus facts keyed by record id, or `None` when none was given."""
    if row_snapshot is None:
        return None
    try:
        snapshot = validate_snapshot(row_snapshot)
    except CorpusRefusal as error:
        # `rows.py` refuses in its own vocabulary (`text-sha256-mismatch`,
        # `empty-rows`, ...). This module's set is closed, so its name leads and
        # the snapshot's own reason travels in the detail.
        raise CorpusRefusal(
            f"snapshot-mismatch: the row snapshot does not validate: {error}"
        ) from error
    return ({row["record_id"]: row for row in snapshot["rows"]}, snapshot["self_hash"])


def _cross_check_snapshot(row: dict[str, Any], sealed: dict[str, dict[str, Any]] | None) -> None:
    """Hold a row to the sealed snapshot's own record, field by field.

    The receipt and the two files it names sit in one directory, so anything that
    rewrote a text or a box and re-wrote the receipt passes the receipt check.
    The row snapshot is this repository's own sealed copy of the corpus facts,
    and the only witness that was never in that directory (independent audit of
    2026-09-11, finding 4).
    """
    if sealed is None:
        return
    record_id = row["record_id"]
    sealed_row = sealed.get(record_id)
    if sealed_row is None:
        raise CorpusRefusal(
            f"snapshot-mismatch: record {record_id!r} is not in the sealed row snapshot"
        )
    for field in ("text", "split", "record_url"):
        if row[field] != sealed_row[field]:
            raise CorpusRefusal(
                f"snapshot-mismatch: record {record_id!r} carries {field} {row[field]!r}, the "
                f"sealed row snapshot carries {sealed_row[field]!r}"
            )


def admit_local_set(
    set_root: str | Path,
    *,
    split: str,
    output_dir: str | Path | None = None,
    row_snapshot: dict[str, Any] | None = None,
    release_test_split: bool = False,
) -> dict[str, Any]:
    """Every record of one local set, admitted as reference truth or refused by name.

    Returns the admission ledger (`recordgold-local-admission.v1`), validated and
    self-hashed, with its reference pages inline. With `output_dir`, also writes
    `ledger.json` and `reference-pages.jsonl` there, refusing to overwrite either
    or to write inside the set. `row_snapshot` is a `recordgold-rows.v1` body;
    when given, every row is held to its sealed record. `release_test_split`
    releases the held-out split and nothing else.
    """
    set_root = Path(set_root)
    if split not in SPLITS:
        raise CorpusRefusal(f"unknown-split: {split!r} is not one of {sorted(SPLITS)}")
    if (split == HELD_SPLIT) != bool(release_test_split):
        raise CorpusRefusal(
            f"holdout-ledger-required: release_test_split and split {HELD_SPLIT!r} go together "
            "-- admitting the held-out split as reference truth must be a deliberate, separate "
            "act, and the flag releases no other split"
        )
    if output_dir is not None:
        resolved_output = Path(output_dir).resolve()
        resolved_root = set_root.resolve()
        if resolved_output == resolved_root or resolved_root in resolved_output.parents:
            raise CorpusRefusal(
                f"output-inside-set: {resolved_output} is inside the set root {resolved_root}; "
                "nothing here writes into a set"
            )
    sealed = _snapshot_index(row_snapshot)
    sealed_rows = sealed[0] if sealed is not None else None
    receipt = _receipt(set_root)
    if split not in receipt["requested_splits"]:
        raise CorpusRefusal(
            f"split-not-requested: the receipt at {receipt['path']} requested "
            f"{receipt['requested_splits']!r}; admitting {split!r} from it would give the ledger "
            "a provenance the set's own record contradicts"
        )
    manifest_rows = _load_jsonl(set_root / "page_manifest.jsonl", "page_manifest.jsonl")
    gold_rows = _load_jsonl(set_root / "gold.jsonl", "gold.jsonl")

    pages: dict[str, dict[str, Any]] = {}
    for row in manifest_rows:
        page_id = row.get("page_id")
        if not isinstance(page_id, str) or not page_id:
            raise CorpusRefusal("malformed-record: a page manifest row has no page_id")
        if page_id in pages:
            raise CorpusRefusal(f"inconsistent-page-manifest: page {page_id!r} is listed twice")
        pages[page_id] = row

    ledger_rows: list[dict[str, Any]] = []
    refused_by_reason: dict[str, int] = {}
    rotations: dict[str, int] = {}
    seen_record_ids: set[str] = set()
    by_page: dict[str, list[dict[str, Any]]] = {}
    page_facts: dict[str, dict[str, Any]] = {}
    page_refusals: dict[str, tuple[str, str]] = {}
    cross_checked = 0

    def refuse(
        row: dict[str, Any], error: Exception | str, *, facts: dict[str, Any] | None
    ) -> None:
        reason = _reason(error) if isinstance(error, Exception) else error.split(":", 1)[0]
        detail = str(error)
        refused_by_reason[reason] = refused_by_reason.get(reason, 0) + 1
        # The two identity fields are held to `str | None` by the ledger's own
        # validator; a raw number here refused one record and then aborted the
        # whole ledger. The detail string keeps the original value verbatim.
        ledger_rows.append(
            {
                "record_id": _identity(row.get("record_id")),
                "page_id": _identity(row.get("page_id")),
                "split": _canonical_safe(row.get("split")),
                "record_url": _canonical_safe(row.get("record_url")),
                "iiif_rotation": _canonical_safe(row.get("iiif_rotation")),
                "source_bbox": _canonical_safe(row.get("source_bbox")),
                "bbox": _canonical_safe(row.get("bbox")),
                "page_sha256": facts["sha256"] if facts else None,
                "page_width": facts["width"] if facts else None,
                "page_height": facts["height"] if facts else None,
                "page_image": facts["image"] if facts else None,
                "physical_page_id": None,
                "physical_act_id": None,
                "reference_page_self_hash": None,
                "decision": "refused",
                "reason": reason,
                "detail": detail,
            }
        )

    for row in gold_rows:
        record_id = row.get("record_id")
        page_id = row.get("page_id")
        if set(row) != _GOLD_ROW_FIELDS:
            refuse(
                row,
                f"malformed-record: record {record_id!r} is not the closed gold row",
                facts=None,
            )
            continue
        if not isinstance(record_id, str) or not record_id:
            refuse(row, "malformed-record: a gold row has no record_id", facts=None)
            continue
        if record_id in seen_record_ids:
            refuse(row, f"duplicate-record-id: {record_id!r} appears twice", facts=None)
            continue
        seen_record_ids.add(record_id)
        if row["split"] != split:
            refuse(
                row,
                f"unknown-split: record {record_id!r} carries split {row['split']!r}, this set "
                f"is admitted as {split!r}",
                facts=None,
            )
            continue
        manifest = pages.get(page_id)
        if manifest is None:
            refuse(
                row,
                f"inconsistent-page-manifest: record {record_id!r} names page {page_id!r}, which "
                "the page manifest does not list",
                facts=None,
            )
            continue
        listed = {
            entry.get("record_id")
            for entry in manifest.get("records", [])
            if isinstance(entry, dict)
        }
        if record_id not in listed:
            refuse(
                row,
                f"inconsistent-page-manifest: page {page_id!r} does not list record {record_id!r}",
                facts=None,
            )
            continue
        if page_id not in page_facts and page_id not in page_refusals:
            try:
                width = _positive_int(manifest.get("width"), f"page {page_id!r} width")
                height = _positive_int(manifest.get("height"), f"page {page_id!r} height")
                image_rel = manifest.get("image")
                image_path = _page_image_path(set_root, image_rel, page_id)
                if not image_path.is_file():
                    raise CorpusRefusal(f"missing-page-file: {image_path} is not a file")
                body = image_path.read_bytes()
                _decode_page(body, width=width, height=height)
                page_facts[page_id] = {
                    "sha256": digest_bytes(body),
                    "width": width,
                    "height": height,
                    "image": image_rel,
                }
            except CorpusRefusal as error:
                page_refusals[page_id] = (_reason(error), str(error))
        if page_id in page_refusals:
            refuse(row, page_refusals[page_id][1], facts=None)
            continue
        facts = page_facts[page_id]
        try:
            parsed = parse_record_url(row["record_url"], rotations=SUPPORTED_ROTATIONS)
            rotation = row["iiif_rotation"]
            if rotation != parsed.rotation:
                raise CorpusRefusal(
                    f"inconsistent-transform: record {record_id!r} states iiif_rotation "
                    f"{rotation!r} but its record_url carries {parsed.rotation!r}"
                )
            source_box = _box(row["source_bbox"], f"record {record_id!r} source_bbox")
            if source_box != parsed.region:
                raise CorpusRefusal(
                    f"inconsistent-transform: record {record_id!r} source_bbox {row['source_bbox']} "
                    f"is not the record_url's region {parsed.region}"
                )
            transformed = transform_region(
                parsed.region, rotation=rotation, width=facts["width"], height=facts["height"]
            )
            declared_box = _box(row["bbox"], f"record {record_id!r} bbox")
            if declared_box != transformed:
                raise CorpusRefusal(
                    f"inconsistent-transform: record {record_id!r} bbox {row['bbox']} is not the "
                    f"{rotation}-degree carry of {row['source_bbox']} on a "
                    f"{facts['width']}x{facts['height']} page, which is "
                    f"[{transformed['x']}, {transformed['y']}, {transformed['w']}, {transformed['h']}]"
                )
            if (
                transformed["x"] < 0
                or transformed["y"] < 0
                or transformed["x"] + transformed["w"] > facts["width"]
                or transformed["y"] + transformed["h"] > facts["height"]
            ):
                raise CorpusRefusal(
                    f"region-outside-page: record {record_id!r} carried box {transformed} exceeds "
                    f"the page's {facts['width']}x{facts['height']} bounds"
                )
            source = row["source"]
            if not isinstance(source, str) or unsafe_segment(source) or "/" in source:
                raise CorpusRefusal(
                    f"unsafe-source-value: source {source!r} on record {record_id!r} is not a "
                    "safe single path segment"
                )
            text = row["text"]
            if not isinstance(text, str) or not text:
                raise CorpusRefusal(f"malformed-record: record {record_id!r} carries no text")
            # A text that survives admission but normalises to nothing takes the
            # whole scoring run down later: `scoring._score_units` raises
            # `MeasurementRefusal` on a blank checked reference, which is outside
            # this package's vocabulary and names neither the record nor the
            # page. Refuse it here, by record, against the same profile
            # `compare.py` scores with (independent audit of 2026-09-11,
            # finding 14).
            if not character_units(text, GRAPHEMIC_V1):
                raise CorpusRefusal(
                    f"empty-normalized-text: record {record_id!r} carries text {text!r}, which "
                    f"normalises to nothing under {GRAPHEMIC_V1.profile_id} and could never be "
                    "scored against"
                )
            _cross_check_snapshot(row, sealed_rows)
        except CorpusRefusal as error:
            refuse(row, error, facts=facts)
            continue
        if sealed_rows is not None:
            cross_checked += 1
        volume, designation = volume_and_designation(parsed.identifier)
        by_page.setdefault(page_id, []).append(
            {
                "row": row,
                "parsed": parsed,
                "volume": volume,
                "designation": designation,
                "source": source,
                "rotation": rotation,
                "source_box": source_box,
                "transformed": transformed,
                "text": text,
            }
        )

    reference_pages: list[dict[str, Any]] = []
    pages_admitted: set[str] = set()
    for page_id in sorted(by_page):
        entries = by_page[page_id]
        facts = page_facts[page_id]
        first = entries[0]
        if any(
            (entry["source"], entry["volume"], entry["designation"])
            != (first["source"], first["volume"], first["designation"])
            for entry in entries
        ):
            for entry in entries:
                refuse(
                    entry["row"],
                    f"inconsistent-page-manifest: page {page_id!r} joins records whose URLs name "
                    "different pages",
                    facts=facts,
                )
            continue
        try:
            physical_page = physical_page_id(
                CORPUS_ID, f"{first['source']}/{first['volume']}", first["designation"]
            )
            act_ids = {
                entry["row"]["record_id"]: physical_act_id(physical_page, entry["row"]["record_id"])
                for entry in entries
            }
            reference = build_reference_page(
                page={
                    "sha256": facts["sha256"],
                    "width": facts["width"],
                    "height": facts["height"],
                },
                source=first["source"],
                volume=first["volume"],
                designation=first["designation"],
                split=split,
                records=[
                    {
                        "record_id": entry["row"]["record_id"],
                        "region": entry["transformed"],
                        "split": entry["row"]["split"],
                        "text": entry["text"],
                        "text_sha256": digest_bytes(entry["text"].encode("utf-8")),
                    }
                    for entry in entries
                ],
            )
        except IdentityRefusal as error:
            for entry in entries:
                refuse(entry["row"], f"unmintable-page-identity: {error}", facts=facts)
            continue
        except CorpusRefusal as error:
            for entry in entries:
                refuse(entry["row"], f"reference-build-refused: {error}", facts=facts)
            continue
        pages_admitted.add(page_id)
        reference_pages.append(reference)
        for entry in entries:
            row = entry["row"]
            rotations[entry["rotation"]] = rotations.get(entry["rotation"], 0) + 1
            ledger_rows.append(
                {
                    "record_id": row["record_id"],
                    "page_id": page_id,
                    "split": row["split"],
                    "record_url": row["record_url"],
                    "iiif_rotation": entry["rotation"],
                    "source_bbox": row["source_bbox"],
                    "bbox": row["bbox"],
                    "page_sha256": facts["sha256"],
                    "page_width": facts["width"],
                    "page_height": facts["height"],
                    "page_image": facts["image"],
                    "physical_page_id": physical_page,
                    "physical_act_id": act_ids[row["record_id"]],
                    "reference_page_self_hash": reference["self_hash"],
                    "decision": "admitted",
                    "reason": None,
                    "detail": None,
                }
            )

    ledger_rows.sort(key=lambda entry: (str(entry["page_id"]), str(entry["record_id"])))
    # Counted from the rows themselves rather than defined as each other's
    # complement: a check whose two sides are one subtraction apart proves
    # arithmetic, not reconciliation (independent audit of 2026-09-11, finding 5).
    admitted = sum(1 for entry in ledger_rows if entry["decision"] == "admitted")
    refused = sum(1 for entry in ledger_rows if entry["decision"] == "refused")
    named_in_gold = {row.get("page_id") for row in gold_rows}
    pages_by_outcome = dict.fromkeys(_PAGE_OUTCOMES, 0)
    for page_id in pages:
        if page_id in pages_admitted:
            outcome = "admitted"
        elif page_id in page_refusals:
            outcome = "refused"
        elif page_id not in named_in_gold:
            outcome = "no-gold-row"
        else:
            outcome = "all-records-refused"
        pages_by_outcome[outcome] += 1
    ledger = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "set_root": str(set_root),
        "split": split,
        "receipt": receipt,
        "row_snapshot": {
            "consulted": sealed is not None,
            "self_hash": sealed[1] if sealed is not None else None,
            "records_cross_checked": cross_checked,
        },
        "summary": {
            "records": len(gold_rows),
            "admitted": admitted,
            "refused": refused,
            "refused_by_reason": dict(sorted(refused_by_reason.items())),
            "pages_listed": len(pages),
            "pages_by_outcome": pages_by_outcome,
            "admitted_by_rotation": dict(sorted(rotations.items())),
        },
        "rows": ledger_rows,
        "reference_pages": reference_pages,
    }
    ledger["self_hash"] = self_hash(ledger)
    validate_local_admission_ledger(ledger)
    if output_dir is not None:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = output_dir / "ledger.json"
        pages_path = output_dir / "reference-pages.jsonl"
        for path in (ledger_path, pages_path):
            if path.exists():
                raise CorpusRefusal(
                    f"malformed-record: {path} already exists; a ledger is never overwritten"
                )
        pages_path.write_bytes(b"".join(canonical_bytes(page) + b"\n" for page in reference_pages))
        ledger_path.write_bytes(canonical_bytes(ledger))
    return ledger


def _closed(value: Any, fields: frozenset[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CorpusRefusal(f"malformed-record: {what} must be the closed record {sorted(fields)}")
    return value


def _counts(value: Any, label: str) -> dict[str, int]:
    """A histogram: a mapping from names to non-negative integers, or a refusal by name."""
    if not isinstance(value, dict):
        raise CorpusRefusal(f"malformed-record: {label} is not a mapping of counts")
    for name, count in value.items():
        if (
            not isinstance(name, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise CorpusRefusal(f"malformed-record: {label}[{name!r}] is not a count")
    return value


def validate_local_admission_ledger(ledger: Any) -> dict[str, Any]:
    """Refuse a ledger that is not exactly `recordgold-local-admission.v1`.

    Closed at every level, self-hashed, and reconciled against itself: the row
    count against the record count, the decisions against the rows, the reason
    histogram against the refused count, and every listed page against exactly
    one outcome. Every sibling record in this package carries a validator and a
    loader; this one was the exception (independent audit of 2026-09-11,
    finding 9).
    """
    ledger = _closed(ledger, _TOP_FIELDS, "admission ledger")
    if ledger["schema"] != SCHEMA:
        raise CorpusRefusal(f"wrong-schema: expected {SCHEMA!r}, got {ledger['schema']!r}")
    if ledger["corpus_id"] != CORPUS_ID:
        raise CorpusRefusal(f"wrong-corpus: expected {CORPUS_ID!r}, got {ledger['corpus_id']!r}")
    if not verify_self_hash(ledger):
        raise CorpusRefusal("self-hash-mismatch: the ledger does not hash to its own self_hash")
    if ledger["split"] not in SPLITS:
        raise CorpusRefusal(f"unknown-split: {ledger['split']!r} is not one of {sorted(SPLITS)}")
    if not isinstance(ledger["set_root"], str) or not ledger["set_root"]:
        raise CorpusRefusal("malformed-record: set_root must be a non-empty string")

    receipt = _closed(ledger["receipt"], _RECEIPT_FIELDS, "receipt")
    if not is_sha256(receipt["receipt_sha256"]):
        raise CorpusRefusal("malformed-record: receipt.receipt_sha256 must be a sha256 digest")
    digests = receipt["digests"]
    if not isinstance(digests, dict) or set(digests) != {"gold.jsonl", "page_manifest.jsonl"}:
        raise CorpusRefusal(
            "malformed-record: receipt.digests must name gold.jsonl and page_manifest.jsonl"
        )
    for name, digest in sorted(digests.items()):
        if not is_sha256(digest):
            raise CorpusRefusal(f"malformed-record: receipt.digests[{name!r}] is not a sha256")

    snapshot = _closed(ledger["row_snapshot"], _ROW_SNAPSHOT_FIELDS, "row_snapshot")
    if not isinstance(snapshot["consulted"], bool):
        raise CorpusRefusal("malformed-record: row_snapshot.consulted must be a boolean")
    if snapshot["consulted"] != is_sha256(snapshot["self_hash"]):
        raise CorpusRefusal(
            "malformed-record: row_snapshot.self_hash is a digest exactly when a snapshot was "
            "consulted"
        )
    if not isinstance(snapshot["records_cross_checked"], int) or isinstance(
        snapshot["records_cross_checked"], bool
    ):
        raise CorpusRefusal("malformed-record: row_snapshot.records_cross_checked must be an int")
    if not snapshot["consulted"] and snapshot["records_cross_checked"] != 0:
        raise CorpusRefusal(
            "malformed-record: no snapshot was consulted, so no record was cross-checked"
        )

    summary = _closed(ledger["summary"], _SUMMARY_FIELDS, "summary")
    rows = ledger["rows"]
    if not isinstance(rows, list):
        raise CorpusRefusal("malformed-record: rows must be a list")
    admitted = 0
    refused = 0
    for row in rows:
        row = _closed(row, _LEDGER_ROW_FIELDS, "an admission row")
        if row["decision"] == "admitted":
            admitted += 1
            if row["reason"] is not None or row["detail"] is not None:
                raise CorpusRefusal(
                    f"malformed-record: admitted row {row['record_id']!r} carries a refusal reason"
                )
            for field in ("page_sha256", "physical_page_id", "physical_act_id"):
                if not isinstance(row[field], str) or not row[field]:
                    raise CorpusRefusal(
                        f"malformed-record: admitted row {row['record_id']!r} has no {field}"
                    )
        elif row["decision"] == "refused":
            refused += 1
            if row["reason"] not in LOCAL_ADMISSION_REFUSAL_REASONS:
                raise CorpusRefusal(
                    f"malformed-record: refused row {row['record_id']!r} names reason "
                    f"{row['reason']!r}, which is outside this module's closed vocabulary"
                )
            if any(
                row[field] is not None
                for field in ("physical_act_id", "physical_page_id", "reference_page_self_hash")
            ):
                raise CorpusRefusal(
                    f"malformed-record: refused row {row['record_id']!r} carries an identity it "
                    "was never admitted to earn"
                )
            for field in ("record_id", "page_id"):
                if row[field] is not None and not isinstance(row[field], str):
                    raise CorpusRefusal(
                        f"malformed-record: refused row carries a non-string {field} {row[field]!r}"
                    )
        else:
            raise CorpusRefusal(
                f"malformed-record: row {row['record_id']!r} decides {row['decision']!r}, not "
                "'admitted' or 'refused'"
            )

    if len(rows) != summary["records"]:
        raise CorpusRefusal(
            f"malformed-record: the ledger carries {len(rows)} row(s) for {summary['records']} "
            "record(s); every record ends in exactly one row"
        )
    if (admitted, refused) != (summary["admitted"], summary["refused"]):
        raise CorpusRefusal(
            f"malformed-record: the rows decide {admitted} admitted / {refused} refused, the "
            f"summary claims {summary['admitted']} / {summary['refused']}"
        )
    # Every histogram is a mapping of non-negative integer counts before any of
    # them is summed: a list or a bool there is refused by name, never added up
    # (CodeRabbit on e97d1482).
    histogram = _counts(summary["refused_by_reason"], "refused_by_reason")
    if sum(histogram.values()) != refused:
        raise CorpusRefusal(
            "malformed-record: refused_by_reason does not sum to the number of refused rows"
        )
    outcomes = _counts(summary["pages_by_outcome"], "pages_by_outcome")
    if set(outcomes) != set(_PAGE_OUTCOMES):
        raise CorpusRefusal(
            f"malformed-record: pages_by_outcome must name exactly {sorted(_PAGE_OUTCOMES)}"
        )
    if sum(outcomes.values()) != summary["pages_listed"]:
        raise CorpusRefusal(
            f"malformed-record: pages_by_outcome accounts for {sum(outcomes.values())} page(s) "
            f"of the {summary['pages_listed']} the manifest lists"
        )
    rotations = _counts(summary["admitted_by_rotation"], "admitted_by_rotation")
    if sum(rotations.values()) != admitted:
        raise CorpusRefusal(
            "malformed-record: admitted_by_rotation does not account for every admitted record"
        )

    reference_pages = ledger["reference_pages"]
    if not isinstance(reference_pages, list) or len(reference_pages) != outcomes["admitted"]:
        raise CorpusRefusal(
            "malformed-record: the ledger's reference pages do not match its admitted page count"
        )
    # The pages themselves, and the link the rows claim to them. `evaluate.py`
    # decides `reference-page-not-in-ledger` from the rows'
    # `reference_page_self_hash` alone, so a ledger naming page identities no
    # page in it carries would bless any reference pages at all (independent
    # audit of 2026-09-11, round 2 item 4).
    embedded = set()
    for page in reference_pages:
        try:
            embedded.add(validate_reference_page(page)["self_hash"])
        except CorpusRefusal as error:
            raise CorpusRefusal(
                f"malformed-record: the ledger carries a reference page that does not "
                f"validate: {error}"
            ) from error
    claimed = {row["reference_page_self_hash"] for row in rows if row["decision"] == "admitted"}
    if claimed != embedded:
        raise CorpusRefusal(
            "malformed-record: the admitted rows name reference pages the ledger does not "
            f"carry, or carry pages no row names ({len(claimed - embedded)} named but absent, "
            f"{len(embedded - claimed)} carried but unnamed)"
        )
    return ledger


def load_local_admission_ledger(path: str | Path) -> dict[str, Any]:
    """Read a ledger written by `admit_local_set`, refusing one that fails to validate.

    The only tracked way to read a `recordgold-local-admission.v1` file back, so
    nothing downstream can score against a ledger that was never verified.
    """
    return validate_local_admission_ledger(json.loads(Path(path).read_bytes()))


def read_row_snapshot(path: str | Path) -> dict[str, Any]:
    """One `recordgold-rows.v1` file, refused by name rather than by traceback.

    The same boundary `_receipt` and `_load_jsonl` already hold: a missing file,
    a non-UTF-8 file and a file that is not JSON each refuse under this module's
    own vocabulary (independent audit of 2026-09-11, round 2 item 7).
    """
    path = Path(path)
    if not path.is_file():
        raise CorpusRefusal(f"missing-set-file: {path} is not a file")
    try:
        return json.loads(_read_text(path, "the row snapshot"))
    except ValueError as error:
        raise CorpusRefusal(f"malformed-record: {path} is not JSON: {error}") from error


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("set_root", help="a local RecordGold set (pages/, gold.jsonl, ...)")
    parser.add_argument("--split", required=True, choices=sorted(SPLITS))
    parser.add_argument("--output-dir", required=True, help="new directory for the ledger")
    parser.add_argument(
        "--row-snapshot",
        help=(
            "a recordgold-rows.v1 file; every row is held to its sealed record. Without it "
            "the set's own receipt is the only witness to the corpus facts."
        ),
    )
    parser.add_argument(
        "--release-test-split",
        action="store_true",
        help=(
            f"Required to admit --split {HELD_SPLIT}, and refused with any other --split — "
            "the flag releases the held split only."
        ),
    )
    args = parser.parse_args(argv)
    snapshot = read_row_snapshot(args.row_snapshot) if args.row_snapshot else None
    ledger = admit_local_set(
        args.set_root,
        split=args.split,
        output_dir=args.output_dir,
        row_snapshot=snapshot,
        release_test_split=args.release_test_split,
    )
    json.dump(ledger["summary"], sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script over real sets
    raise SystemExit(main())
