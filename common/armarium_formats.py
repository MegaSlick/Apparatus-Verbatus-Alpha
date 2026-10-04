"""The sealed configuration surface for Armarium export projections.

The manifest is always written; `KNOWN_FORMATS` are the projections Armarium
can emit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from common.contracts.errors import ContractError, SchemaRefusal
from common.sealed_config import read_sealed_toml

FORMAT_SCHEMA: Final = "armarium-formats.v1"
KNOWN_FORMATS: Final = frozenset({"text-bundle", "acts-database", "jsonl", "review-items"})
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
# Crops are cut from their page and stored as lossless PNG, and together cover
# about one page; the Door estimates them at this many bytes per page pixel.
CROP_BYTES_PER_PIXEL: Final = 1


@dataclass(frozen=True)
class ArmariumFormats:
    """The run-bound projection choices, validated before a run starts."""

    formats: tuple[str, ...]
    embed_pixels: bool

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

    def to_record(self) -> dict[str, object]:
        return {
            "schema": FORMAT_SCHEMA,
            "formats": list(self.formats),
            "embed_pixels": self.embed_pixels,
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
    required = {"schema", "formats", "embed_pixels"}
    if set(record) != required:
        raise SchemaRefusal(
            f"Armarium formats {source} must contain exactly schema, formats, and embed_pixels"
        )
    if record["schema"] != FORMAT_SCHEMA:
        raise SchemaRefusal(
            f"Armarium formats configuration declares {record['schema']!r}, not {FORMAT_SCHEMA!r}"
        )
    formats = record["formats"]
    if not isinstance(formats, list):
        raise SchemaRefusal("Armarium formats must be a non-empty list of names")
    return ArmariumFormats(tuple(formats), record["embed_pixels"])


def bind_armarium_formats(path: str | Path) -> tuple[str, ArmariumFormats]:
    """Read, seal and parse one formats configuration for a run binding.

    The only reader: nothing may bind a format selection without also sealing
    the table it came from, and both come from one read.
    """
    raw, digest = read_sealed_toml(path, "Armarium formats configuration")
    return digest, armarium_formats_from_record(raw, source=f"configuration {path}")


def estimated_embedded_export_bytes(pages: Iterable[tuple[int, int, int]]) -> int:
    """An embedded export's size from its sealed pages, as `(stored bytes, width, height)`.

    Each page is carried as stored, and its crops at `CROP_BYTES_PER_PIXEL` over
    its pixel area. The records beside them are small next to the pixels and
    are not counted; the Armarium checks the real archive before storing it.
    """
    return sum(stored + width * height * CROP_BYTES_PER_PIXEL for stored, width, height in pages)


def require_within_export_archive_limit(size: int, *, what: str, embed_pixels: bool) -> None:
    """Refuse an export archive of `size` bytes above the limit, naming `what` was measured."""
    limit = MAX_EXPORT_ARCHIVE_BYTES
    if size <= limit:
        return
    remedy = (
        "split the submission into smaller runs, or export with embed_pixels = false"
        if embed_pixels
        else "split the submission into smaller runs"
    )
    raise ContractError(
        f"{what} is {size} bytes, above the {limit}-byte export archive limit, so it could "
        f"never be sealed. Nothing was dropped: {remedy}"
    )
