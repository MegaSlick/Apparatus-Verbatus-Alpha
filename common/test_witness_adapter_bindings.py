import warnings
from dataclasses import replace
from pathlib import Path

import pytest

from common import witness_adapters
from common.chairs import load_models_toml
from common.chairs.config import parse_models_config
from common.chairs.errors import ConfigurationRefusal, ReceiptRefusal
from common.chairs.models import ChairIdentity, ModelsConfig, ServingDetails, is_witness_role
from common.chairs.receipts import build_receipt, receipt_record, validate_receipt
from common.contracts.errors import ContractError, IncompatibleReuse
from common.runtree.store import RunTree
from common.stage import load_fixture, run_config_bindings

ROOT = Path(__file__).resolve().parents[1]


def _models() -> ModelsConfig:
    return load_models_toml(ROOT / "config" / "models.toml")


def _with_witness(**changes: object) -> ModelsConfig:
    models = _models()
    chairs = dict(models.chairs)
    chairs["attestator_1"] = replace(chairs["attestator_1"], **changes)
    return replace(models, chairs=chairs)


def _configured_chair(*, source="huggingface", **changes):
    row = {
        "state": "configured",
        "source": source,
        "digest_manifest": "b" * 64,
        "manifest": "manifests/example.json",
        "serving_recipe": "fixture",
        "license_note": "fixture",
    }
    if source == "huggingface":
        row.update(repo="fixture/example", revision="a" * 40)
    else:
        row["path"] = "example"
    row.update(changes)
    return row


def _serving_details() -> ServingDetails:
    return ServingDetails(
        tokenizer_revision="c" * 40,
        seed=1,
        context_cap=1024,
        pixel_cap=1024,
        engine="fixture",
        engine_version="1",
        dtype="fixture",
        adapter_identity=None,
        endpoint="http://127.0.0.1:8000",
        started_at="2026-08-26T00:00:00Z",
    )


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


def test_a_string_subclass_cannot_replace_the_adapter_refusal_with_its_hooks():
    class HostileString(str):
        def strip(self, *args, **kwargs):
            raise RuntimeError("strip hook ran")

        def __hash__(self):
            raise RuntimeError("hash hook ran")

        def __repr__(self):
            raise RuntimeError("repr hook ran")

    name = HostileString("churro.v1")
    with pytest.raises(witness_adapters.AdapterRefusal) as caught:
        witness_adapters.resolve_witness_adapter_name(name)

    assert caught.value.name is name
    assert "<HostileString>" in str(caught.value)


def test_an_oversized_adapter_name_is_bounded_before_hashing_or_reporting():
    name = "x" * (witness_adapters.MAX_WITNESS_ADAPTER_NAME_LENGTH + 1_000_000)

    with pytest.raises(witness_adapters.AdapterRefusal) as caught:
        witness_adapters.resolve_witness_adapter_name(name)

    message = str(caught.value)
    assert "exceeds the 128-character name bound" in message
    assert f"({len(name)} characters)" in message
    assert len(message) < 500


def test_a_known_adapter_name_with_no_configured_occupant_is_reported(monkeypatch, capsys):
    """A non-fatal registry finding must survive global warning filters.

    Treating warnings as errors proves the report bypasses that suppressible
    channel; the stream assertions prove it reaches stderr only.
    """
    monkeypatch.setattr(
        witness_adapters,
        "KNOWN_WITNESS_ADAPTER_NAMES",
        witness_adapters.KNOWN_WITNESS_ADAPTER_NAMES | {"unbound.fixture.v1"},
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


def test_an_adapter_without_any_scope_is_refused_like_a_wrong_one():
    """Omission is the likeliest mistake, and it must not read as a default.

    `witness_scope` is optional in the closed schema because a non-witness chair
    carries neither field. Once `witness_adapter` names a boundary, a missing
    scope leaves the adapter unable to say whether it runs per page or per act,
    and guessing either would silently change how much ink a chair is shown.
    """
    raw = {
        "witness_floor": 1,
        "chairs": {"attestator_1": _configured_chair(witness_adapter="churro.v1")},
    }

    with pytest.raises(ContractError, match="invalid witness_scope") as caught:
        parse_models_config(raw)

    assert "Set witness_scope to exactly 'page' or 'act'" in str(caught.value)


def test_the_live_roster_pins_one_adapter_per_chair():
    """The three adapters now partition the roster one-to-one.

    Chandra reads page geometry natively, Churro answers whole pages with no
    layout, and DAI crops acts. Pinning the assignment makes a moved binding a
    loud fact rather than a silent reassignment of which ink a chair is shown.
    """
    models = _models()
    witness_adapters.validate_witness_adapter_bindings(models)
    # The whole configured map, not three named chairs: naming only the chairs
    # it expects, this test would stay green while `models.toml` added a fourth
    # witness chair, and the sentence above about a one-to-one partition would
    # quietly stop being true of the live roster.
    # `getattr`, because an absent chair carries neither field at all: it must
    # be skipped, not raise, and it can never hold a binding to miss.
    assert {
        name: (chair.witness_adapter, chair.witness_scope)
        for name, chair in models.chairs.items()
        if getattr(chair, "witness_adapter", None) is not None
        or getattr(chair, "witness_scope", None) is not None
    } == {
        "attestator_1": ("chandra.v1", "page"),
        "attestator_2": ("dai.v1", "act"),
        "attestator_3": ("churro.v1", "page"),
    }


def test_two_chairs_may_share_one_adapter_at_different_scopes():
    """The adapter belongs to each occupant; it is not a unique or ranked seat.

    Collapsing adapter names only decides whether a registry declaration is in
    use. Scope remains on each identity, so sharing a native boundary cannot
    collapse the page/act distinction or select one chair over the other.

    The roster above happens to assign one adapter per chair, so asserting that
    roster is not a test of this rule: it would pass unchanged if the validator
    grew a uniqueness refusal. The sharing case has to be constructed.
    """
    models = _models()
    chairs = dict(models.chairs)
    chairs["attestator_2"] = replace(
        chairs["attestator_2"], witness_adapter="chandra.v1", witness_scope="act"
    )
    shared = replace(models, chairs=chairs)

    witness_adapters.validate_witness_adapter_bindings(shared)

    assert shared.chairs["attestator_1"].witness_adapter == "chandra.v1"
    assert shared.chairs["attestator_1"].witness_scope == "page"
    assert shared.chairs["attestator_2"].witness_adapter == "chandra.v1"
    assert shared.chairs["attestator_2"].witness_scope == "act"


@pytest.mark.parametrize(
    "rows",
    (
        {"witness_adapter": "churro.v1", "witness_scope": "page"},
        {"witness_adapter": "churro.v1"},
        {"witness_scope": "act"},
    ),
)
def test_a_non_witness_chair_may_not_declare_a_witness_boundary(rows):
    """Non-witness rows would seal provenance for a boundary the role never uses."""
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


def test_case_variant_chair_roles_are_refused_before_they_alias_a_cache_directory():
    raw = {
        "witness_floor": 0,
        "chairs": {
            "Reader": _configured_chair(),
            "reader": _configured_chair(),
        },
    }

    with pytest.raises(ConfigurationRefusal, match="case-variant chair roles"):
        parse_models_config(raw)


@pytest.mark.parametrize(
    ("first", "second", "label"),
    (
        (
            _configured_chair(manifest="manifests/Reader.json"),
            _configured_chair(manifest="manifests/reader.json"),
            "manifest paths",
        ),
        (
            _configured_chair(source="local-repository", path="Reader"),
            _configured_chair(source="local-repository", path="reader"),
            "local-repository paths",
        ),
    ),
)
def test_case_variant_configured_paths_are_refused_before_filesystem_resolution(
    first, second, label
):
    raw = {
        "witness_floor": 0,
        "model_root": "models",
        "chairs": {"first": first, "second": second},
    }

    with pytest.raises(ConfigurationRefusal, match=f"case-variant {label}"):
        parse_models_config(raw)


@pytest.mark.parametrize(
    ("first", "second"),
    (
        (
            _configured_chair(manifest="manifests/shared.json"),
            _configured_chair(manifest="manifests/shared.json"),
        ),
        (
            _configured_chair(source="local-repository", path="shared"),
            _configured_chair(source="local-repository", path="shared"),
        ),
    ),
)
def test_two_chairs_may_share_one_path_under_the_exact_same_spelling(first, second):
    """Only two spellings of one folded path are ambiguous; exact sharing is not.

    A refusal that also caught identical spellings would forbid the deliberate
    case the check exists to distinguish, and there would be no way to pin two
    chairs to one manifest or one local repository.
    """
    raw = {
        "witness_floor": 0,
        "model_root": "models",
        "chairs": {"first": first, "second": second},
    }

    models = parse_models_config(raw)

    assert models.chairs["first"].manifest == models.chairs["second"].manifest
    assert models.chairs["first"].path == models.chairs["second"].path


def test_the_live_roster_declares_the_rows_on_witness_chairs_and_nowhere_else():
    """An explicit absence never parses the rows, so it is not held to them.

    `validate_witness_adapter_bindings` skips `AbsentChair` for that reason, and
    `_parse_chair` returns before the fields are read. Asserting the rows on an
    absence would make this test refuse a roster the production rule allows.
    """
    occupied = 0
    for role, chair in _models().chairs.items():
        if not isinstance(chair, ChairIdentity):
            continue
        assert (chair.witness_adapter is not None) == is_witness_role(role), role
        occupied += 1
    assert occupied, "the live roster declares no occupied chair to check"


def test_adapter_rows_travel_in_the_resolved_provenance_record():
    record = _models().chairs["attestator_1"].to_record()
    assert record["witness_adapter"] == "chandra.v1"
    assert record["witness_scope"] == "page"


@pytest.mark.parametrize(
    ("role", "changes", "message"),
    (
        ("perlector", {"witness_adapter": "churro.v1", "witness_scope": "page"}, "non-Attestator"),
        ("attestator_1", {"witness_adapter": "unknown.v1"}, "exact declared"),
        ("attestator_1", {"witness_scope": "crop"}, "exactly 'page' or 'act'"),
    ),
)
def test_receipt_reader_validates_witness_fields_inside_a_nested_identity(role, changes, message):
    models = _models()
    record = receipt_record(build_receipt(models.chairs["attestator_1"], _serving_details()))
    nested = {**models.chairs[role].to_record(), **changes}
    record["adapter_identity"] = nested

    with pytest.raises(ReceiptRefusal, match=message):
        validate_receipt(record)


def test_witness_scope_is_inside_the_sealed_config_digest():
    """Changing invocation granularity must make the old run seal incompatible."""
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
        witness_adapters.KNOWN_WITNESS_ADAPTER_NAMES | {"other.fixture.v1"},
    )
    fixture = load_fixture(str(ROOT / "proof"))
    sealed = run_config_bindings(_models(), fixture, "happy")["config_digest"]
    swapped = run_config_bindings(
        _with_witness(witness_adapter="other.fixture.v1"), fixture, "happy"
    )["config_digest"]
    assert sealed != swapped


def test_reducing_a_roster_cannot_reuse_one_run_id_silently(tmp_path):
    """A corpus may be run again under a changed roster, but not as the old run.

    The witness-context declaration must change with the roster; both its chair
    list and digest then prevent reuse before a byte changes.
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
    """A retired scope key must refuse rather than look authoritative when ignored."""
    original = (ROOT / "proof" / "skeleton_fixture.toml").read_text()
    marker = 'fixture_id = "synthetic-two-page-v0"\n'
    assert marker in original
    stale = original.replace(marker, marker + 'page_witness_chairs = ["attestator_1"]\n', 1)
    (tmp_path / "skeleton_fixture.toml").write_text(stale)

    with pytest.raises(ContractError, match="page_witness_chairs"):
        load_fixture(str(tmp_path))
