import tomllib
from pathlib import Path

import pytest

from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError
from common.contracts.stages import TRIAGE_MODES
from common.stage import (
    MAX_TRIAGE_MODES_CONFIG_BYTES,
    load_fixture,
    require_triage_modes,
    run_config_bindings,
)


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


def test_malformed_replacement_cannot_mask_the_sealed_digest_refusal(tmp_path):
    config = tmp_path / "triage_modes.toml"
    original = b"[manual]\nreview_at_or_below_confidence = 4\n"
    config.write_bytes(original)
    sealed = {"triage-modes": digest_bytes(original)}
    replacement = b"[manual\n"
    config.write_bytes(replacement)
    with pytest.raises(ContractError, match="changed between run binding") as refusal:
        require_triage_modes(sealed, config)
    assert sealed["triage-modes"] in str(refusal.value)
    assert digest_bytes(replacement) in str(refusal.value)


def test_triage_config_read_is_bounded_before_digest_or_toml_work(tmp_path):
    config = tmp_path / "triage_modes.toml"
    config.write_bytes(b"x" * (MAX_TRIAGE_MODES_CONFIG_BYTES + 1))
    with pytest.raises(ContractError, match=f"{MAX_TRIAGE_MODES_CONFIG_BYTES}-byte limit"):
        require_triage_modes({"triage-modes": "0" * 64}, config)


def test_triage_modes_are_bound_at_run_creation():
    root = Path(__file__).resolve().parents[1]
    fixture = load_fixture(root / "proof")
    bindings = run_config_bindings(
        ChairRegistry.from_toml(root / "config/models.toml").config, fixture, "happy"
    )
    assert bindings["sealed_config_digests"]["triage-modes"] == digest_bytes(
        (root / "config/triage_modes.toml").read_bytes()
    )


def test_the_binding_seals_the_configuration_its_caller_named(tmp_path):
    """Every other sealed configuration binds a caller-supplied path; triage modes
    alone read the repository default, so a run bound against another file sealed a
    digest of bytes its point-of-use check would never read. Found by CodeRabbit."""
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "triage_modes.toml"
    config.write_text(
        "[manual]\nreview_at_or_below_confidence = 3\n"
        "[semi]\nreview_at_or_below_confidence = 2\n"
        "[auto]\nreview_at_or_below_confidence = 1\n"
    )
    fixture = load_fixture(root / "proof")

    bindings = run_config_bindings(
        ChairRegistry.from_toml(root / "config/models.toml").config,
        fixture,
        "happy",
        triage_modes_config_path=config,
    )

    assert bindings["sealed_config_digests"]["triage-modes"] == digest_bytes(config.read_bytes())
    require_triage_modes(bindings["sealed_config_digests"], config)


@pytest.mark.parametrize(
    ("body", "names"),
    [
        pytest.param(
            b"[automatic]\nreview_at_or_below_confidence = 4\n",
            "wrong closed schema",
            id="a-mode-nobody-declared",
        ),
        pytest.param(
            b"[manual]\nreview_at_or_below_confidence = 9\n"
            b"[semi]\nreview_at_or_below_confidence = 2\n"
            b"[auto]\nreview_at_or_below_confidence = 1\n",
            "wrong closed schema",
            id="a-confidence-outside-the-ordinal",
        ),
        pytest.param(b"[manual\n", "not valid TOML", id="not-parseable-at-all"),
        pytest.param(b"[manual]\nx = \xff\n", "not valid UTF-8", id="not-decodable-at-all"),
    ],
)
def test_the_binding_refuses_a_triage_configuration_it_could_only_seal(tmp_path, body, names):
    """`run_config_bindings` hashed these bytes without parsing them, so a file
    declaring a mode nobody declared sealed cleanly into `run.json` and the run
    walked several stages before the first `require_triage_modes` refused it. The
    binding and the point-of-use check now share one validator, so the refusal
    lands at run creation, where nothing has been written yet. Found by
    CodeRabbit."""
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "triage_modes.toml"
    config.write_bytes(body)

    with pytest.raises(ContractError, match=names):
        run_config_bindings(
            ChairRegistry.from_toml(root / "config/models.toml").config,
            load_fixture(root / "proof"),
            "happy",
            triage_modes_config_path=config,
        )


def test_the_run_digest_moves_when_the_triage_thresholds_do(tmp_path):
    """The tests above read `sealed_config_digests`, which is the *named* copy. Drop
    `triage_modes_config_sha256` from `config_digest` and every one of them still
    passes, while the guarantee that actually matters is gone: `open_context`
    compares `config_digest`, so reopening a run under changed review thresholds
    would no longer be refused as incompatible reuse. Two otherwise identical
    bindings, differing only in the thresholds, must not hash alike. Found by
    CodeRabbit."""
    root = Path(__file__).resolve().parents[1]
    fixture = load_fixture(root / "proof")
    models = ChairRegistry.from_toml(root / "config/models.toml").config

    def bind(threshold: int, name: str) -> str:
        config = tmp_path / name
        config.write_text(
            f"[manual]\nreview_at_or_below_confidence = {threshold}\n"
            "[semi]\nreview_at_or_below_confidence = 2\n"
            "[auto]\nreview_at_or_below_confidence = 1\n"
        )
        return run_config_bindings(models, fixture, "happy", triage_modes_config_path=config)[
            "config_digest"
        ]

    assert bind(3, "a.toml") != bind(4, "b.toml")
    # Identical bytes under a different file name still bind identically, so what
    # moved the digest above is the thresholds and not the path.
    assert bind(3, "a.toml") == bind(3, "c.toml")


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
    # The raw config, manifest schema, and point-of-use check must not acquire
    # independent mode vocabularies.
    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "config/triage_modes.toml").read_text(encoding="utf-8"))
    # Membership, unordered, because that is what `require_triage_modes` checks:
    # comparing tuples pinned the order of the TOML tables too, and would have
    # failed a harmless reordering of a file whose order means nothing.
    assert set(declared) == set(TRIAGE_MODES)


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
    "policy",
    [
        "review_at_or_below_confidence = 5",
        "review_at_or_below_confidence = -1",
        "review_at_or_below_confidence = true",
        'review_at_or_below_confidence = "4"',
        "review_at_or_below_confidence = 4\nreview_above_confidence = 1",
        "",
    ],
    ids=["above", "below", "boolean", "string", "extra-key", "absent"],
)
def test_a_mode_table_outside_the_closed_threshold_shape_is_refused(tmp_path, policy):
    # The threshold is the number that decides whether a frame is held for review,
    # and every clause bounding it was unpinned: the mode-name tests above pass
    # whatever the ordinal, the type check, or the closed key set is doing.
    config = tmp_path / "triage_modes.toml"
    config.write_text(
        f"[manual]\n{policy}\n"
        "[semi]\nreview_at_or_below_confidence = 4\n"
        "[auto]\nreview_at_or_below_confidence = 4\n"
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
