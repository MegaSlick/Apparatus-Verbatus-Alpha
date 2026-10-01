"""The sealed Perlector protocol declaration: its closed schema, read and refused."""

from pathlib import Path

import protocol
import pytest

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "config" / "perlector_protocol.toml"


def test_the_sealed_protocol_declaration_reproduces():
    protocol_config, protocol_sha256 = protocol.load(PROTOCOL)
    assert sorted(protocol_config) == ["feed", "page_context", "truncation"]
    assert len(protocol_sha256) == 64


@pytest.mark.parametrize(
    "extra",
    ['reading_unit = "page"', "max_images = 32", 'selection_rule = "x"'],
)
def test_a_field_outside_the_closed_schema_is_refused(tmp_path, extra):
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(extra + "\n" + PROTOCOL.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ContractError, match="not its closed schema"):
        protocol.load(path)


def test_a_protocol_declaration_missing_a_table_is_refused(tmp_path):
    shipped = PROTOCOL.read_text(encoding="utf-8")
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(shipped[: shipped.index("[page_context]")], encoding="utf-8")
    with pytest.raises(ContractError, match="not its closed schema"):
        protocol.load(path)


def test_the_page_render_bounds_are_sealed_and_checked(tmp_path):
    sealed, _digest = protocol.load(PROTOCOL)
    assert sealed["page_context"] == {"maximum_edge": 2560}
    shipped = PROTOCOL.read_text(encoding="utf-8")
    for edited in (
        shipped.replace("maximum_edge = 2560", "maximum_edge = 0"),
        shipped.replace("maximum_edge = 2560", "maximum_edge = 2560\ncovered_page_edge = 1024"),
    ):
        assert edited != shipped
        path = tmp_path / "protocol.toml"
        path.write_text(edited, encoding="utf-8")
        with pytest.raises(ContractError, match="page_context"):
            protocol.load(path)
