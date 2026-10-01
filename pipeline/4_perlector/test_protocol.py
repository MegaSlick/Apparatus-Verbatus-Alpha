"""The sealed Perlector protocol declaration: its closed schema, read and refused."""

from pathlib import Path

import protocol
import pytest

from common.contracts.errors import ContractError

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "config" / "perlector_protocol.toml"


def test_the_sealed_protocol_declaration_reproduces():
    protocol_config, protocol_sha256 = protocol.load(ROOT / "config" / "perlector_protocol.toml")
    assert protocol_config["selection_rule"] == protocol.SELECTION_RULE
    assert protocol_config["page_shared_prefix_policy"] == protocol.PAGE_SHARED_PREFIX_POLICY
    assert protocol_config[protocol.READING_UNIT_FIELD] == "page"
    assert len(protocol_sha256) == 64


def _write_protocol(tmp_path, **overrides):
    fields = {
        "selection_rule": protocol.SELECTION_RULE,
        "page_shared_prefix_policy": protocol.PAGE_SHARED_PREFIX_POLICY,
        "pass_b_fragment": protocol.PASS_B_FRAGMENT,
        "max_images": 32,
        "reading_unit": "page",
    }
    fields.update(overrides)
    path = tmp_path / "perlector_protocol.toml"
    scalar_lines = "\n".join(
        f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {str(value).lower()}"
        for key, value in fields.items()
    )
    # The tables are carried verbatim from the shipped declaration; these tests are
    # about its scalar fields.
    shipped = (ROOT / "config" / "perlector_protocol.toml").read_text(encoding="utf-8")
    truncation_table = shipped[shipped.index("[truncation]") :]
    path.write_text(scalar_lines + "\n\n" + truncation_table)
    return path


def test_a_pass_b_fragment_asserting_the_prior_was_wrong_is_refused(tmp_path):
    """The no-picking control (iterative_reader.md:46-51): the protocol
    declaration cannot ship a fragment that forces a change, only one that
    reports the finding."""
    path = _write_protocol(tmp_path, pass_b_fragment="The prior reading was wrong; correct it.")
    with pytest.raises(ContractError, match="protocol is neutral"):
        protocol.load(path)


@pytest.mark.parametrize(
    "fragment",
    [
        # Forces a change without using the blacklisted five words.
        "The prior reading contains errors. Find and fix them.",
        "The earlier draft is incorrect; rewrite it.",
        # Tells the reader to follow the draft: a picker under another name.
        "Trust the prior reading; reproduce it verbatim.",
        # An instrument constraining its own measure.
        "Rate your confidence no higher than medium.",
    ],
)
def test_a_pass_b_fragment_that_is_not_the_declared_neutral_form_is_refused(tmp_path, fragment):
    """Every one of these was ACCEPTED before the fragment was pinned: the
    phrase blacklist below catches five literal words and nothing else, so it
    could not be the no-picking / measure-honestly control it was named as."""
    with pytest.raises(ContractError, match="not the declared neutral form"):
        protocol.load(_write_protocol(tmp_path, pass_b_fragment=fragment))


def test_the_shipped_declaration_carries_the_pinned_neutral_form():
    """The pin binds the shipped bytes, not only a hypothetical file. The
    fragment itself is iterative_reader.md:49-50 verbatim; that note is a
    workbench design record and not tracked here, so the constant is where the
    repository holds it and this is the check that config agrees with it."""
    protocol_config, _sha = protocol.load(ROOT / "config" / "perlector_protocol.toml")
    assert protocol_config["pass_b_fragment"] == protocol.PASS_B_FRAGMENT
    assert protocol_config["max_images"] == 32


@pytest.mark.parametrize("capacity", [0, -1, True, "32"])
def test_a_protocol_without_a_positive_integer_image_capacity_is_refused(tmp_path, capacity):
    """Distinct from the closed-schema refusal below: a missing field, and a
    bad max_images value, are different faults and must read as different
    faults -- a guard must be able to fail for the reason it names."""
    path = _write_protocol(tmp_path, max_images=capacity)
    with pytest.raises(ContractError, match="max_images is not a positive integer") as excinfo:
        protocol.load(path)
    assert repr(capacity) in str(excinfo.value)


def test_a_blank_pass_b_fragment_is_refused(tmp_path):
    path = _write_protocol(tmp_path, pass_b_fragment="   ")
    with pytest.raises(ContractError, match="blank Pass-B fragment"):
        protocol.load(path)


def test_an_unknown_selection_rule_is_refused(tmp_path):
    path = _write_protocol(tmp_path, selection_rule="stratified-v1")
    with pytest.raises(ContractError, match="unknown selection rule"):
        protocol.load(path)


def test_an_unknown_page_shared_prefix_policy_is_refused(tmp_path):
    path = _write_protocol(tmp_path, page_shared_prefix_policy="witness-order-first.v1")
    with pytest.raises(ContractError, match="unknown page-shared-prefix policy"):
        protocol.load(path)


def test_a_protocol_declaration_missing_a_field_is_refused(tmp_path):
    path = tmp_path / "perlector_protocol.toml"
    path.write_text(f'selection_rule = "{protocol.SELECTION_RULE}"')
    with pytest.raises(ContractError, match="not its closed schema"):
        protocol.load(path)


def test_a_reading_unit_other_than_the_page_is_refused_by_name(tmp_path):
    path = _write_protocol(tmp_path, reading_unit="act")
    with pytest.raises(ContractError, match="reading_unit 'act' is not one of"):
        protocol.load(path)


def test_the_cap_and_the_page_render_bound_are_sealed_and_the_fragment_is_pinned(tmp_path):
    sealed, _digest = protocol.load(PROTOCOL)
    assert sealed["neighbours"]["characters_per_row"] == 800
    assert sealed["neighbours"]["fragment"] == protocol.NEIGHBOUR_FRAGMENT
    assert sealed["page_context"] == {"maximum_edge": 2560, "covered_page_edge": 1024}
    shipped = PROTOCOL.read_text(encoding="utf-8")
    for edited, refusal in (
        (shipped.replace("characters_per_row = 800", "characters_per_row = 0"), "neighbours"),
        (shipped.replace("never copy a neighbour's text", "copy a neighbour's text"), "fragment"),
        (shipped.replace("maximum_edge = 2560", "maximum_edge = 0"), "page_context"),
    ):
        assert edited != shipped
        path = tmp_path / "protocol.toml"
        path.write_text(edited, encoding="utf-8")
        with pytest.raises(ContractError, match=refusal):
            protocol.load(path)
