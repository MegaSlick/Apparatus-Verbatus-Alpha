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

from common.contracts.errors import SchemaRefusal

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
    """
    if not isinstance(record, dict):
        raise SchemaRefusal(f"Armarium formats {source} is not an object")
    raw = record
    try:
        keys = set(raw)
    except TypeError as error:
        raise SchemaRefusal(f"Armarium formats {source} has invalid keys") from error
    required = {"schema", "formats", "embed_pixels"}
    if keys != required:
        raise SchemaRefusal(
            f"Armarium formats {source} must contain exactly schema, formats, and embed_pixels"
        )
    if raw["schema"] != FORMAT_SCHEMA:
        raise SchemaRefusal(
            f"Armarium formats configuration declares {raw['schema']!r}, not {FORMAT_SCHEMA!r}"
        )
    formats = raw["formats"]
    if (
        not isinstance(formats, list)
        or not formats
        or any(not isinstance(item, str) for item in formats)
    ):
        raise SchemaRefusal("Armarium formats must be a non-empty list of names")
    if len(set(formats)) != len(formats):
        raise SchemaRefusal("Armarium formats names a format more than once")
    unknown = sorted(set(formats) - KNOWN_FORMATS)
    if unknown:
        raise SchemaRefusal(f"Armarium formats names unknown format(s) {unknown}")
    if not isinstance(raw["embed_pixels"], bool):
        raise SchemaRefusal("Armarium embed_pixels must be a boolean")
    return ArmariumFormats(tuple(formats), raw["embed_pixels"])


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
