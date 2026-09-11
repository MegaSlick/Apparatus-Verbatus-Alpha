"""Admit the RecordGold pages and records already on this machine as reference truth.

The corpus was fetched once (`fetch.py`) into per-split sets -- a `pages/`
folder, a `page_manifest.jsonl`, a `gold.jsonl` and a `fetch_receipt.json`
naming their digests -- and those sets are the input to every local trial.
Forty of their records are stated in a 180-degree IIIF view: `record_url`
carries `/180/`, `source_bbox` is the box in that rotated frame, and `bbox` is
the same box carried into the stored page's frame by
`(W - x - w, H - y - h, w, h)`. `plan.py`'s fetch-time parser refuses those
rows by name, correctly, because at fetch time it holds no page dimensions to
convert with. This module does hold them -- the stored page is decoded and
measured before any record on it is admitted -- so it carries the box across
and records every fact of the crossing: the original URL, the rotation, the
original box, the transformed box, the page's digest and dimensions. Nothing
is treated as unrotated that was not; a rotation outside `0`/`180` stays a
named refusal (independent audit of 2026-09-10, finding F4).

**Read-only over the set.** Nothing here writes into a set root; the ledger and
the reference pages go where the caller says. The set's own receipt digests
are checked first, so a ledger is bound to the exact `gold.jsonl` and
`page_manifest.jsonl` bytes it was built from.

**Every row ends admitted or refused, by name.** A page-level refusal (a
missing or undecodable image, dimensions that disagree with the manifest, an
EXIF orientation) refuses every record on that page; a record-level refusal
refuses that record alone and the page's reference truth is built from the
rest, with the refused ids beside it. The denominator is the whole set -- 784
validation records, 6,178 training records -- and the summary counts both
sides, so a later trial can name its population instead of quietly claiming
the corpus.

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

from common.contracts.canonical import canonical_bytes, digest_bytes, is_sha256
from common.contracts.errors import IdentityRefusal
from common.contracts.identities import physical_act_id, physical_page_id

from . import CorpusRefusal
from .plan import SUPPORTED_ROTATIONS, _unsafe_segment, _volume_and_designation, parse_record_url
from .reference import CORPUS_ID, SPLITS, build_reference_page

SCHEMA = "recordgold-local-admission.v1"
RECEIPT_SCHEMA = "recordgold_full_page_fetch_v1"
_EXIF_ORIENTATION_TAG = 0x0112

LOCAL_ADMISSION_REFUSAL_REASONS = frozenset(
    {
        "malformed-record",
        "receipt-hash-mismatch",
        "missing-set-file",
        "unknown-split",
        "missing-page-file",
        "non-image-body",
        "dimension-mismatch",
        "exif-orientation",
        "inconsistent-page-manifest",
        "unsupported-rotation-parameter",
        "unparseable-record-url",
        "unexpected-host",
        "unsupported-size-parameter",
        "unsupported-quality-parameter",
        "unsupported-format-parameter",
        "non-positive-region",
        "unsafe-identifier-segment",
        "unsafe-source-value",
        "inconsistent-transform",
        "region-outside-page",
        "duplicate-record-id",
        "unmintable-page-identity",
        "reference-build-refused",
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


def transform_region(
    region: dict[str, int], *, rotation: str, width: int, height: int
) -> dict[str, int]:
    """Carry a box stated in a rotated IIIF view into the stored page's frame.

    `0` is the identity. `180` maps `(x, y, w, h)` to `(W - x - w, H - y - h, w,
    h)`: the view is the page turned upside down, so a box's far corner in the
    view is its near corner on the stored page. Nothing else is defined here;
    `90` and `270` swap the axes and their box would need the *rotated* frame's
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


def _load_jsonl(path: Path, what: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
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
    receipt = json.loads(receipt_path.read_bytes())
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
    return {
        "path": str(receipt_path),
        "status": receipt.get("status"),
        "requested_splits": receipt.get("requested_splits"),
        "digests": digests,
    }


def _decode_page(body: bytes, *, width: int, height: int) -> None:
    """Decode the stored page and hold it to the manifest's own frame.

    The same three refusals `fetch.py` applies at fetch time, applied again at
    admission: the manifest's width/height are the frame every `bbox` is stated
    in, and a page that no longer decodes to them, or that carries an EXIF
    display rotation, would put every box on it in the wrong frame.
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


def admit_local_set(
    set_root: str | Path,
    *,
    split: str,
    output_dir: str | Path | None = None,
    decode_pages: bool = True,
) -> dict[str, Any]:
    """Every record of one local set, admitted as reference truth or refused by name.

    Returns the admission ledger (`recordgold-local-admission.v1`) with its
    reference pages inline. With `output_dir`, also writes `ledger.json` and
    `reference-pages.jsonl` there and refuses to overwrite either. `decode_pages`
    exists for tests over synthetic sets only; a real admission decodes every
    page.
    """
    set_root = Path(set_root)
    if split not in SPLITS:
        raise CorpusRefusal(f"unknown-split: {split!r} is not one of {sorted(SPLITS)}")
    receipt = _receipt(set_root)
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

    def refuse(row: dict[str, Any], error: Exception | str, *, page_sha256: str | None) -> None:
        reason = _reason(error) if isinstance(error, Exception) else error.split(":", 1)[0]
        detail = str(error)
        refused_by_reason[reason] = refused_by_reason.get(reason, 0) + 1
        ledger_rows.append(
            {
                "record_id": row.get("record_id"),
                "page_id": row.get("page_id"),
                "split": row.get("split"),
                "record_url": row.get("record_url"),
                "iiif_rotation": row.get("iiif_rotation"),
                "source_bbox": row.get("source_bbox"),
                "bbox": row.get("bbox"),
                "page_sha256": page_sha256,
                "physical_act_id": None,
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
                page_sha256=None,
            )
            continue
        if not isinstance(record_id, str) or not record_id:
            refuse(row, "malformed-record: a gold row has no record_id", page_sha256=None)
            continue
        if record_id in seen_record_ids:
            refuse(row, f"duplicate-record-id: {record_id!r} appears twice", page_sha256=None)
            continue
        seen_record_ids.add(record_id)
        if row["split"] != split:
            refuse(
                row,
                f"unknown-split: record {record_id!r} carries split {row['split']!r}, this set "
                f"is admitted as {split!r}",
                page_sha256=None,
            )
            continue
        manifest = pages.get(page_id)
        if manifest is None:
            refuse(
                row,
                f"inconsistent-page-manifest: record {record_id!r} names page {page_id!r}, which "
                "the page manifest does not list",
                page_sha256=None,
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
                page_sha256=None,
            )
            continue
        if page_id not in page_facts and page_id not in page_refusals:
            try:
                width = _positive_int(manifest.get("width"), f"page {page_id!r} width")
                height = _positive_int(manifest.get("height"), f"page {page_id!r} height")
                image_rel = manifest.get("image")
                if not isinstance(image_rel, str) or not image_rel:
                    raise CorpusRefusal(f"malformed-record: page {page_id!r} names no image")
                image_path = set_root / image_rel
                if not image_path.is_file():
                    raise CorpusRefusal(f"missing-page-file: {image_path} is not a file")
                body = image_path.read_bytes()
                if decode_pages:
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
            refuse(row, page_refusals[page_id][1], page_sha256=None)
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
            if not isinstance(source, str) or _unsafe_segment(source) or "/" in source:
                raise CorpusRefusal(
                    f"unsafe-source-value: source {source!r} on record {record_id!r} is not a "
                    "safe single path segment"
                )
            text = row["text"]
            if not isinstance(text, str) or not text:
                raise CorpusRefusal(f"malformed-record: record {record_id!r} carries no text")
        except CorpusRefusal as error:
            refuse(row, error, page_sha256=facts["sha256"])
            continue
        volume, designation = _volume_and_designation(parsed.identifier)
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
    pages_admitted = 0
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
                    page_sha256=facts["sha256"],
                )
            continue
        try:
            physical_page = physical_page_id(
                CORPUS_ID, f"{first['source']}/{first['volume']}", first["designation"]
            )
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
                refuse(
                    entry["row"],
                    f"unmintable-page-identity: {error}",
                    page_sha256=facts["sha256"],
                )
            continue
        except CorpusRefusal as error:
            for entry in entries:
                refuse(
                    entry["row"],
                    f"reference-build-refused: {error}",
                    page_sha256=facts["sha256"],
                )
            continue
        pages_admitted += 1
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
                    "physical_act_id": physical_act_id(physical_page, row["record_id"]),
                    "reference_page_self_hash": reference["self_hash"],
                    "decision": "admitted",
                    "reason": None,
                    "detail": None,
                }
            )

    ledger_rows.sort(key=lambda entry: (str(entry["page_id"]), str(entry["record_id"])))
    admitted = sum(1 for entry in ledger_rows if entry["decision"] == "admitted")
    ledger = {
        "schema": SCHEMA,
        "corpus_id": CORPUS_ID,
        "set_root": str(set_root),
        "split": split,
        "receipt": receipt,
        "summary": {
            "records": len(gold_rows),
            "admitted": admitted,
            "refused": len(gold_rows) - admitted,
            "refused_by_reason": dict(sorted(refused_by_reason.items())),
            "pages_listed": len(pages),
            "pages_admitted": pages_admitted,
            "pages_refused": len(page_refusals),
            "admitted_by_rotation": dict(sorted(rotations.items())),
        },
        "rows": ledger_rows,
        "reference_pages": reference_pages,
    }
    if ledger["summary"]["records"] != admitted + ledger["summary"]["refused"]:
        raise CorpusRefusal("malformed-record: the admission ledger does not reconcile")
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


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("set_root", help="a local RecordGold set (pages/, gold.jsonl, ...)")
    parser.add_argument("--split", required=True, choices=sorted(SPLITS))
    parser.add_argument("--output-dir", required=True, help="new directory for the ledger")
    args = parser.parse_args(argv)
    ledger = admit_local_set(args.set_root, split=args.split, output_dir=args.output_dir)
    json.dump(ledger["summary"], sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a script over real sets
    raise SystemExit(main())
