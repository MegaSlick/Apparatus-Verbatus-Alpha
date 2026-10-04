"""The sealed configuration surface for Armarium export projections.

The manifest is always written; `KNOWN_FORMATS` are the projections Armarium
can emit. `lot` says whether every row carries the run's lot
(`common.contracts.identities.lot_id`).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from common.contracts.errors import ContractError, SchemaRefusal
from common.sealed_config import read_sealed_toml

FORMAT_SCHEMA: Final = "armarium-formats.v2"
KNOWN_FORMATS: Final = frozenset({"text-bundle", "acts-database", "jsonl", "csv", "review-items"})
DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "formats.toml"
)
# The largest export archive a run may produce. The archive embeds pages and
# crops when `embed_pixels` is true, so it may outgrow the single page-blob read
# ceiling in `common/runtree/store.py`, which reads the Armarium's blobs under
# this limit instead. It must stay at or below `MAX_FETCH_OBJECT_BYTES` in
# `operations/operator/surface.py`, so an archive that seals can be fetched;
# `operations/operator/test_surface.py` pins that. Every whole-archive read is
# held in memory, so raising it raises peak memory at sealing and publication.
MAX_EXPORT_ARCHIVE_BYTES: Final = 192 * 1024 * 1024
# How many whole pages of crops the Door allows for each exported page. Only
# delivered readings carry crops, one per reading: the bounding box of its
# region. Two readings whose regions claim mostly the same ink are held
# (`duplicate-region` in `common/page_accounting.py`), so delivered regions
# barely overlap, but their bounding boxes can still intersect where readings
# interleave (columns, marginalia). Twice the page area allows for that; it is
# an allowance rather than a proof, so the Armarium still checks the real
# archive before storing it.
CROP_PAGE_COVERAGE: Final = 2


@dataclass(frozen=True)
class ArmariumFormats:
    """The run-bound projection choices, validated before a run starts."""

    formats: tuple[str, ...]
    embed_pixels: bool
    lot: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.formats, tuple)
            or not self.formats
            or any(not isinstance(item, str) for item in self.formats)
        ):
            raise SchemaRefusal("Armarium formats must be a non-empty tuple of names")
        if len(set(self.formats)) != len(self.formats):
            raise SchemaRefusal("Armarium formats names a format more than once")
        unknown = sorted(set(self.formats) - KNOWN_FORMATS)
        if unknown:
            raise SchemaRefusal(f"Armarium formats names unknown format(s) {unknown}")
        if not isinstance(self.embed_pixels, bool):
            raise SchemaRefusal("Armarium embed_pixels must be a boolean")
        if not isinstance(self.lot, bool):
            raise SchemaRefusal("Armarium lot must be a boolean")

    def to_record(self) -> dict[str, object]:
        return {
            "schema": FORMAT_SCHEMA,
            "formats": list(self.formats),
            "embed_pixels": self.embed_pixels,
            "lot": self.lot,
        }


def armarium_formats_from_record(record: object, *, source: str = "record") -> ArmariumFormats:
    """Validate the sealed representation used by a run and its bundle.

    The disk configuration and the exported manifest deliberately use the same
    closed record.  Keeping the validator here means a verifier never trusts a
    manifest's claimed format selection just because its self-hash is valid.

    Only the record's own shape is this function's question: its key set, its schema
    string, and that ``formats`` is a list -- a bare string would pass ``tuple()``
    silently and explode into one entry per character. The format-name and
    ``embed_pixels`` rules live in ``ArmariumFormats.__post_init__``, which this
    calls into.
    """
    if not isinstance(record, dict):
        raise SchemaRefusal(f"Armarium formats {source} is not an object")
    required = {"schema", "formats", "embed_pixels", "lot"}
    if set(record) != required:
        raise SchemaRefusal(
            f"Armarium formats {source} must contain exactly schema, formats, embed_pixels and lot"
        )
    if record["schema"] != FORMAT_SCHEMA:
        raise SchemaRefusal(
            f"Armarium formats configuration declares {record['schema']!r}, not {FORMAT_SCHEMA!r}"
        )
    formats = record["formats"]
    if not isinstance(formats, list):
        raise SchemaRefusal("Armarium formats must be a non-empty list of names")
    return ArmariumFormats(tuple(formats), record["embed_pixels"], record["lot"])


def bind_armarium_formats(path: str | Path) -> tuple[str, ArmariumFormats]:
    """Read, seal and parse one formats configuration for a run binding.

    The only reader: nothing may bind a format selection without also sealing
    the table it came from, and both come from one read.
    """
    raw, digest = read_sealed_toml(path, "Armarium formats configuration")
    return digest, armarium_formats_from_record(raw, source=f"configuration {path}")


def estimated_embedded_export_bytes(pages: Iterable[tuple[int, int, int, float]]) -> int:
    """An upper estimate of an embedded export's pixels, from its sealed pages.

    Each page is `(stored bytes, width, height, crop bytes per pixel)`: it is
    carried as stored, and its crops as `CROP_PAGE_COVERAGE` whole-page crops,
    each an uncompressed PNG of one filter byte plus the packed pixels per row.
    The records beside them are small next to the pixels and are not counted.
    """
    return sum(
        stored + CROP_PAGE_COVERAGE * height * (1 + math.ceil(width * bytes_per_pixel))
        for stored, width, height, bytes_per_pixel in pages
    )


def require_within_export_archive_limit(size: int, *, what: str, embed_pixels: bool) -> None:
    """Refuse an export archive of `size` bytes above the limit, naming `what` was measured."""
    limit = MAX_EXPORT_ARCHIVE_BYTES
    if size <= limit:
        return
    # The format choice is sealed into the run, so a run is never re-exported
    # under another; the remedy is always a new run.
    remedy = (
        "start new runs over smaller parts of the submission, or a new run whose formats "
        "configuration (config/formats.toml, or --formats-config) sets embed_pixels = false"
        if embed_pixels
        else "start new runs over smaller parts of the submission"
    )
    raise ContractError(
        f"{what} is {size} bytes, above the {limit}-byte export archive limit, so it could "
        f"never be sealed. Nothing was dropped: {remedy}"
    )
