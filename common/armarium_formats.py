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
from common.runtree import store as runtree_store
from common.sealed_config import read_sealed_toml

FORMAT_SCHEMA: Final = "armarium-formats.v1"
KNOWN_FORMATS: Final = frozenset({"text-bundle", "acts-database", "jsonl", "review-items"})
DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "formats.toml"
)
# An embedded export carries every exported page and the crops cut from it. The
# crops are stored losslessly and together cover about one page, so they are
# estimated at the page's own bytes again.
CROP_BYTES_PER_PAGE_BYTE: Final = 1


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


def require_export_within_archive_limit(page_bytes: Iterable[int], *, embed_pixels: bool) -> None:
    """Refuse a run whose export archive is estimated past its limit, before any reading.

    The Armarium refuses an oversized archive anyway, but only at the end of the
    run; this estimate from the sealed pages lets the run be refused at its
    start. With `embed_pixels = false` the archive holds records only, so no
    estimate is made here.
    """
    if not embed_pixels:
        return
    sizes = list(page_bytes)
    pages = sum(sizes)
    estimate = pages * (1 + CROP_BYTES_PER_PAGE_BYTE)
    limit = runtree_store.MAX_EXPORT_ARCHIVE_BYTES
    if estimate > limit:
        raise ContractError(
            f"with embed_pixels = true this run's export archive is estimated at {estimate} "
            f"bytes ({len(sizes)} exported page(s) of {pages} bytes, and their crops estimated "
            f"at as much again), above the {limit}-byte export archive limit, so it could "
            "never be sealed. The submission is refused whole before any reading starts, and "
            "nothing is dropped: split it into smaller runs, or export with embed_pixels = false"
        )
