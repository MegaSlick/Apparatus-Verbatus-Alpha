"""The sealed configuration surface for Armarium export projections.

The manifest is always written.  The listed formats are deliberately the ones
Spec 11 names plainly; an Obsidian vault and uncertainty-display conventions are
not included because those remain Tyrel's decisions.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, SchemaRefusal

FORMAT_SCHEMA: Final = "armarium-formats.v1"
KNOWN_FORMATS: Final = frozenset(
    {"text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"}
)
DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "formats.toml"
)


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

    Only the record's own shape -- its key set, its schema string, and that
    ``formats`` is a list rather than some other iterable ``tuple()`` would
    silently misread (a bare string would explode into one entry per
    character) -- is checked here. The format-name and ``embed_pixels`` rules
    live in exactly one place, ``ArmariumFormats.__post_init__``, which this
    function calls into rather than re-deriving the same five checks a second
    time.
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


def parse_armarium_formats_bytes(data: bytes, *, source: str | Path = "bytes") -> ArmariumFormats:
    """Parse precisely the bytes whose digest is sealed into a run authority."""
    label = str(source)
    if not isinstance(data, bytes):
        raise SchemaRefusal(f"Armarium formats configuration {label} is not bytes")
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SchemaRefusal(f"Armarium formats configuration {label} could not be read") from error
    return armarium_formats_from_record(raw, source=f"configuration {label}")


def load_armarium_formats(path: str | Path) -> ArmariumFormats:
    """Load the one format configuration without guessing missing choices."""
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as error:
        raise SchemaRefusal(f"Armarium formats configuration {source} could not be read") from error
    return parse_armarium_formats_bytes(data, source=source)


def bind_armarium_formats(path: str | Path) -> tuple[str, ArmariumFormats]:
    """Read, digest and parse one formats configuration for a run binding.

    The digest is over exactly the bytes read here, before parsing -- the same
    bytes a sealed run's ``config_digest`` must be reproducible from. Two
    independent call sites (a fixture run's config bindings, and the real
    Door's) used to each read, digest and parse this file themselves; sharing
    one function is what keeps a future change to any of those three steps
    from having to be made twice to stay correct in both places.
    """
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ContractError(
            f"the Armarium formats configuration binding at {path} could not be read"
        ) from error
    return digest_bytes(data), parse_armarium_formats_bytes(data, source=path)
