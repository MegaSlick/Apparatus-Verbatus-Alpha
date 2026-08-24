import tomllib
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.stages import TRIAGE_MODES
from common.stage import load_fixture, require_triage_modes, run_config_bindings


def test_triage_modes_are_sealed_and_rechecked_at_point_of_use(tmp_path):
    config = tmp_path / "triage_modes.toml"
    config.write_text(
        "[manual]\nreview_at_or_below_confidence = 4\n[semi]\nreview_at_or_below_confidence = 4\n[auto]\nreview_at_or_below_confidence = 4\n"
    )
    sealed = {"triage-modes": digest_bytes(config.read_bytes())}
    require_triage_modes(sealed, config)
    config.write_text(
        "[manual]\nreview_at_or_below_confidence = 3\n[semi]\nreview_at_or_below_confidence = 4\n[auto]\nreview_at_or_below_confidence = 4\n"
    )
    with pytest.raises(ContractError, match="changed between run binding") as refusal:
        require_triage_modes(sealed, config)
    # The drift refusal names both digests: the message alone cannot otherwise be
    # told apart from a run that sealed the wrong file, and the sealed digest is
    # the fact that decides which.
    assert sealed["triage-modes"] in str(refusal.value)
    assert digest_bytes(config.read_bytes()) in str(refusal.value)


def test_triage_modes_are_bound_at_run_creation():
    root = Path(__file__).resolve().parents[1]
    fixture = load_fixture(root / "proof")
    bindings = run_config_bindings(
        ChairRegistry.from_toml(root / "config/models.toml").config, fixture, "happy"
    )
    assert bindings["sealed_config_digests"]["triage-modes"] == digest_bytes(
        (root / "config/triage_modes.toml").read_bytes()
    )


def test_triage_modes_refuse_an_unsealed_or_non_vocabulary_config(tmp_path):
    config = tmp_path / "triage_modes.toml"
    config.write_text(
        "[manual]\nreview_at_or_below_confidence = 4\n[semi]\nreview_at_or_below_confidence = 4\n[auto]\nreview_at_or_below_confidence = 4\n"
    )
    with pytest.raises(ContractError, match="sealed no digest"):
        require_triage_modes({}, config)
    config.write_text("[manual]\nreview_at_or_below_confidence = 4\n")
    with pytest.raises(ContractError, match="wrong closed schema"):
        require_triage_modes({"triage-modes": digest_bytes(config.read_bytes())}, config)


def test_the_sealed_file_declares_exactly_the_shared_mode_vocabulary():
    # Three spellings of one triple — this file's sections, the manifest schema's
    # closed check, and the point-of-use recheck — is the drift the shared
    # constant exists to stop. Pin the config to it so they cannot part.
    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "config/triage_modes.toml").read_text(encoding="utf-8"))
    assert tuple(declared) == TRIAGE_MODES


def test_a_config_declaring_an_unshared_mode_name_is_refused(tmp_path):
    config = tmp_path / "triage_modes.toml"
    config.write_text(
        "[manual]\nreview_at_or_below_confidence = 4\n"
        "[semi]\nreview_at_or_below_confidence = 4\n"
        "[automatic]\nreview_at_or_below_confidence = 4\n"
    )
    with pytest.raises(ContractError, match="wrong closed schema"):
        require_triage_modes({"triage-modes": digest_bytes(config.read_bytes())}, config)


@pytest.mark.parametrize(
    ("raw", "cause"),
    [
        (b"\xff", "not valid UTF-8"),
        (b"[manual\n", "not valid TOML"),
    ],
)
def test_an_unreadable_triage_config_names_its_actual_cause(tmp_path, raw, cause):
    config = tmp_path / "triage_modes.toml"
    config.write_bytes(raw)
    with pytest.raises(ContractError, match=cause):
        require_triage_modes({"triage-modes": digest_bytes(raw)}, config)
