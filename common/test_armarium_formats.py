"""The Armarium format choice is explicit, closed, and sealed into a run."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.armarium_formats import (
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    KNOWN_FORMATS,
    ArmariumFormats,
    load_armarium_formats,
)
from common.chairs.registry import ChairRegistry
from common.contracts.errors import SchemaRefusal
from common.stage import load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_formats_choose_only_the_plainly_approved_projections():
    formats = load_armarium_formats(DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH)
    assert set(formats.formats) == KNOWN_FORMATS
    assert formats.embed_pixels is False
    assert "obsidian-vault" not in formats.formats


def test_unknown_format_is_refused_instead_of_becoming_an_implicit_product(tmp_path):
    path = tmp_path / "formats.toml"
    path.write_text(
        'schema = "armarium-formats.v1"\nformats = ["text-bundle", "obsidian-vault"]\n'
        "embed_pixels = false\n",
        encoding="utf-8",
    )
    with pytest.raises(SchemaRefusal, match="unknown"):
        load_armarium_formats(path)


def test_direct_format_construction_cannot_bypass_the_closed_parser():
    with pytest.raises(SchemaRefusal, match="unknown"):
        ArmariumFormats(("text-bundle", "witness-picker"), False)
    with pytest.raises(SchemaRefusal, match="more than once"):
        ArmariumFormats(("jsonl", "jsonl"), False)


def test_format_configuration_changes_the_sealed_run_binding(tmp_path):
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    fixture = load_fixture(str(ROOT / "proof"))
    baseline = run_config_bindings(registry.config, fixture, "happy")
    changed = tmp_path / "formats.toml"
    changed.write_text(
        'schema = "armarium-formats.v1"\n'
        'formats = ["text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"]\n'
        "embed_pixels = true\n",
        encoding="utf-8",
    )
    alternate = run_config_bindings(
        registry.config,
        fixture,
        "happy",
        armarium_formats_config_path=changed,
    )
    assert baseline["config_digest"] != alternate["config_digest"]


def test_format_settings_are_parsed_from_the_same_bytes_that_are_digested(monkeypatch, tmp_path):
    registry = ChairRegistry.from_toml(str(ROOT / "config" / "models.toml"))
    fixture = load_fixture(str(ROOT / "proof"))
    formats_path = tmp_path / "formats.toml"
    first = (
        'schema = "armarium-formats.v1"\n'
        'formats = ["text-bundle", "acts-database", "jsonl", "review-items", "salvage-tier"]\n'
        "embed_pixels = false\n"
    ).encode()
    second = first.replace(b"false", b"true")
    formats_path.write_bytes(first)
    original_read_bytes = Path.read_bytes
    format_reads = 0

    def changing_read_bytes(path: Path) -> bytes:
        nonlocal format_reads
        if path == formats_path:
            format_reads += 1
            return first if format_reads == 1 else second
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changing_read_bytes)
    bindings = run_config_bindings(
        registry.config,
        fixture,
        "happy",
        armarium_formats_config_path=formats_path,
    )

    assert format_reads == 1
    assert bindings["armarium_formats"].embed_pixels is False
