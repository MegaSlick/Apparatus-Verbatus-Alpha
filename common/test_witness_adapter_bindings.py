"""Run-entry bindings for the named witness-adapter registry."""

import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from common import witness_adapters
from common.chairs.config import parse_models_config
from common.chairs.models import ModelsConfig
from common.contracts.errors import ContractError, IncompatibleReuse
from common.runtree.store import RunTree
from common.stage import load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[1]


def _models() -> ModelsConfig:
    from common.chairs import load_models_toml

    return load_models_toml(ROOT / "config" / "models.toml")


def _with_witness(**changes: object) -> ModelsConfig:
    models = _models()
    chairs = dict(models.chairs)
    chairs["attestator_1"] = replace(chairs["attestator_1"], **changes)
    return replace(models, chairs=chairs)


def test_a_configured_witness_without_an_adapter_refuses_by_chair_before_a_run_exists():
    with pytest.raises(
        ContractError, match="chair 'attestator_1' has no witness_adapter"
    ) as caught:
        run_config_bindings(
            _with_witness(witness_adapter=None, witness_scope=None),
            load_fixture(str(ROOT / "proof")),
            "happy",
        )
    message = str(caught.value)
    assert "no native boundary to run" in message
    assert "Add witness_adapter and witness_scope" in message


def test_an_unknown_adapter_name_refuses_at_run_binding_by_that_name():
    with pytest.raises(
        ContractError, match="witness adapter 'not-an-adapter' is not declared"
    ) as caught:
        run_config_bindings(
            _with_witness(witness_adapter="not-an-adapter"),
            load_fixture(str(ROOT / "proof")),
            "happy",
        )
    message = str(caught.value)
    assert "No adapter code can run for its chair" in message
    assert "add the new shared declaration and runnable binding together" in message


def test_a_known_adapter_name_with_no_configured_occupant_is_reported(monkeypatch, capsys):
    """Reported, not fatal — and reported where a global switch cannot erase it.

    A `RuntimeWarning` here would vanish under `PYTHONWARNINGS=ignore` while the
    run still exited successfully, which is the shape GOVERNANCE 2 refuses. The
    filter below is set to "error" for that reason and not to "ignore": under it
    a report routed through the warnings machinery would raise instead of
    reaching anyone, so passing proves the report is not routed through it at
    all, and the remaining assertions prove which stream it does reach.
    """
    monkeypatch.setattr(
        witness_adapters,
        "KNOWN_WITNESS_ADAPTER_NAMES",
        frozenset({"churro.v1", "unbound.fixture.v1"}),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        bindings = run_config_bindings(_models(), load_fixture(str(ROOT / "proof")), "happy")
    captured = capsys.readouterr()
    assert "unbound.fixture.v1" in captured.err
    assert "unbound.fixture.v1" not in captured.out
    assert bindings["config_digest"]


def test_blank_adapter_name_is_refused_by_the_closed_models_schema():
    raw = {
        "witness_floor": 1,
        "chairs": {
            "attestator_1": {
                "state": "configured",
                "source": "huggingface",
                "repo": "fixture/example",
                "revision": "a" * 40,
                "digest_manifest": "b" * 64,
                "manifest": "manifests/example.json",
                "serving_recipe": "fixture",
                "license_note": "fixture",
                "witness_adapter": "  ",
                "witness_scope": "page",
            }
        },
    }
    with pytest.raises(ContractError, match="witness_adapter.*non-blank"):
        parse_models_config(raw)


def test_a_missing_chair_table_is_refused_before_adapter_binding():
    with pytest.raises(ContractError, match="chairs must be a non-empty table"):
        parse_models_config({"witness_floor": 0})


@pytest.mark.parametrize("scope", ("crop", "Page"))
def test_bad_witness_scope_is_refused_by_the_closed_models_schema(scope):
    raw = {
        "witness_floor": 1,
        "chairs": {
            "attestator_1": {
                "state": "configured",
                "source": "huggingface",
                "repo": "fixture/example",
                "revision": "a" * 40,
                "digest_manifest": "b" * 64,
                "manifest": "manifests/example.json",
                "serving_recipe": "fixture",
                "license_note": "fixture",
                "witness_adapter": "churro.v1",
                "witness_scope": scope,
            }
        },
    }
    with pytest.raises(ContractError, match="invalid witness_scope") as caught:
        parse_models_config(raw)
    message = str(caught.value)
    assert "cannot determine whether it runs once per page or once per act" in message
    assert "Set witness_scope to exactly 'page' or 'act'" in message


def test_two_chairs_may_share_one_adapter_at_different_scopes():
    """The adapter belongs to each occupant; it is not a unique or ranked seat.

    Collapsing adapter names only decides whether a registry declaration is in
    use. Scope remains on each identity, so sharing a native boundary cannot
    collapse the page/act distinction or select one chair over the other.
    """
    models = _models()
    witness_adapters.validate_witness_adapter_bindings(models)
    first = models.chairs["attestator_1"]
    second = models.chairs["attestator_2"]
    assert first.witness_adapter == second.witness_adapter == "churro.v1"
    assert (first.witness_scope, second.witness_scope) == ("page", "act")


@pytest.mark.parametrize(
    "rows",
    (
        {"witness_adapter": "churro.v1", "witness_scope": "page"},
        {"witness_adapter": "churro.v1"},
        {"witness_scope": "act"},
    ),
)
def test_a_non_witness_chair_may_not_declare_a_witness_boundary(rows):
    """Only an Attestator is shown a witness's native boundary.

    `validate_witness_adapter_bindings` walks `witness_chairs` alone, so these
    rows on a Perlector or a Designator were read by nothing — while
    `to_record()` still carried them into that chair's provenance record and
    into `config_digest`. A record that asserts an adapter a chair never uses is
    a false provenance fact (GOVERNANCE 6), and an operator who typed the rows
    onto the wrong role was told nothing (GOVERNANCE 2). Both are refused at the
    file that made the claim.
    """
    raw = {
        "witness_floor": 0,
        "chairs": {
            "perlector": {
                "state": "configured",
                "source": "huggingface",
                "repo": "fixture/example",
                "revision": "a" * 40,
                "digest_manifest": "b" * 64,
                "manifest": "manifests/example.json",
                "serving_recipe": "fixture",
                "license_note": "fixture",
                **rows,
            }
        },
    }
    with pytest.raises(ContractError, match="non-Attestator chair") as caught:
        parse_models_config(raw)
    message = str(caught.value)
    assert "never invokes a native witness boundary" in message
    assert "Remove both fields or move them" in message


def test_the_live_roster_declares_the_rows_on_witness_chairs_and_nowhere_else():
    for role, chair in _models().chairs.items():
        carries = getattr(chair, "witness_adapter", None) is not None
        assert carries == role.startswith("attestator_"), role


def test_adapter_rows_travel_in_the_resolved_provenance_record():
    record = _models().chairs["attestator_1"].to_record()
    assert record["witness_adapter"] == "churro.v1"
    assert record["witness_scope"] == "page"


def test_witness_scope_is_inside_the_sealed_config_digest():
    """Scope is a run-shaping fact, so a run cannot be resumed under a new one.

    The two halves of that guarantee are tested apart: this asserts that
    changing only `witness_scope` moves `config_digest`, and
    `pipeline/orchestrator/test_orchestrator_acceptance.py::
    test_reusing_a_run_id_with_a_changed_configuration_fails_before_writing`
    asserts that a moved `config_digest` refuses by name at the Door. Without
    the first half, a corpus re-run that quietly swapped a page witness for an
    act witness would inherit the earlier run's seal.
    """
    fixture = load_fixture(str(ROOT / "proof"))
    sealed = run_config_bindings(_models(), fixture, "happy")["config_digest"]
    flipped = run_config_bindings(_with_witness(witness_scope="act"), fixture, "happy")[
        "config_digest"
    ]
    assert _models().chairs["attestator_1"].witness_scope == "page"
    assert sealed != flipped


def test_witness_adapter_is_inside_the_sealed_config_digest(monkeypatch):
    monkeypatch.setattr(
        witness_adapters,
        "KNOWN_WITNESS_ADAPTER_NAMES",
        frozenset({"churro.v1", "other.fixture.v1"}),
    )
    fixture = load_fixture(str(ROOT / "proof"))
    sealed = run_config_bindings(_models(), fixture, "happy")["config_digest"]
    swapped = run_config_bindings(
        _with_witness(witness_adapter="other.fixture.v1"), fixture, "happy"
    )["config_digest"]
    assert sealed != swapped


def test_reducing_a_roster_cannot_reuse_one_run_id_silently(tmp_path):
    """A corpus may be run again under a changed roster, but not as the old run.

    The synchronized witness-context declaration below permits the reduced
    roster as a new configuration. Both its explicit chair list and its digest
    move, and ``RunTree.create`` refuses the old run id before changing a byte.
    """
    fixture = load_fixture(str(ROOT / "proof"))
    full_models = _models()
    full_bindings = run_config_bindings(full_models, fixture, "happy")

    reduced_chairs = dict(full_models.chairs)
    del reduced_chairs["attestator_3"]
    reduced_models = replace(full_models, chairs=reduced_chairs)
    reduced_context = tmp_path / "witness_context.toml"
    reduced_context.write_text(
        "[attestator_1]\n"
        'training_domain = "fixture"\n\n'
        "[attestator_2]\n"
        'training_domain = "fixture"\n',
        encoding="utf-8",
    )
    reduced_bindings = run_config_bindings(
        reduced_models,
        fixture,
        "happy",
        witness_context_config_path=reduced_context,
    )
    assert reduced_bindings["witness_chairs"] == ["attestator_1", "attestator_2"]
    assert reduced_bindings["config_digest"] != full_bindings["config_digest"]

    run_root = tmp_path / "runs"
    source_manifest = [{"relative_path": "page.png", "sha256": "a" * 64, "ordinal": 1}]
    RunTree.create(
        run_root,
        "r",
        source_manifest=source_manifest,
        config_digest=full_bindings["config_digest"],
        adapter_recipes=full_bindings["adapter_recipes"],
        witness_chairs=full_bindings["witness_chairs"],
    )
    before = {
        path.relative_to(run_root).as_posix(): path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(IncompatibleReuse, match="different config_digest, witness_chairs"):
        RunTree.create(
            run_root,
            "r",
            source_manifest=source_manifest,
            config_digest=reduced_bindings["config_digest"],
            adapter_recipes=reduced_bindings["adapter_recipes"],
            witness_chairs=reduced_bindings["witness_chairs"],
        )
    after = {
        path.relative_to(run_root).as_posix(): path.read_bytes()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_a_stale_fixture_still_declaring_page_witness_chairs_is_refused_not_ignored(tmp_path):
    """Scope moved to models.toml's witness_scope; a fixture that still carries the
    retired key must not load as if nothing changed. Read back and silently ignored
    is the exact half-win a stale fixture must not get: the operator believes the
    key still does something, and it does not."""
    original = (ROOT / "proof" / "skeleton_fixture.toml").read_text()
    marker = 'fixture_id = "synthetic-two-page-v0"\n'
    assert marker in original
    stale = original.replace(marker, marker + 'page_witness_chairs = ["attestator_1"]\n', 1)
    (tmp_path / "skeleton_fixture.toml").write_text(stale)

    with pytest.raises(ContractError, match="page_witness_chairs"):
        load_fixture(str(tmp_path))
