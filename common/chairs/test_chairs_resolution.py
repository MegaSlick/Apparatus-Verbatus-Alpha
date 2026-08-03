"""Spec 02, test 1 — Resolution.

"Every role in a fixture `models.toml` resolves to an exact identity or fails
naming the missing field. Non-40-hex and branch-name revisions are refused. A
role the schema has never seen resolves without a schema change."

Resolution is pure and offline: it reads the configuration and returns an
identity, an explicit absence, or a refusal. Nothing here touches a filesystem
beyond the one config file, and nothing here reaches a network.
"""

import pytest

from common.chairs.config import load_models_toml, parse_models_config
from common.chairs.errors import ConfigurationRefusal, UnresolvedChairRefusal
from common.chairs.models import AbsentChair, ChairIdentity

from .conftest import (
    HF_REVISION,
    LICENSE_NOTE,
    SERVING_RECIPE,
    absent_chair,
    config_of,
    hf_chair,
    local_chair,
    write_models_toml,
)

DIGEST = "d" * 64


# --- An exact identity, field for field -----------------------------------------


def test_a_huggingface_chair_resolves_to_its_exact_pinned_identity(tmp_path):
    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST)})
    identity = config.chairs["attestator_1"]

    assert identity == ChairIdentity(
        role="attestator_1",
        source="huggingface",
        repo="fixture-org/attestator_1",
        path=None,
        revision=HF_REVISION,
        digest_manifest=DIGEST,
        manifest="manifests/attestator_1.json",
        adapter_of=None,
        serving_recipe=SERVING_RECIPE,
        license_note=LICENSE_NOTE,
    )
    assert identity.receipt_revision == HF_REVISION
    assert identity.receipt_revision_kind == "git-commit"


def test_a_local_repository_chair_resolves_to_a_path_and_no_revision(tmp_path):
    """A local seat has no git revision by contract, so its verified manifest
    hash is the immutable revision-equivalent — and the receipt says which."""
    config = config_of(
        tmp_path, {"perlector": local_chair("perlector", DIGEST)}, model_root="model-fixtures"
    )
    identity = config.chairs["perlector"]

    assert isinstance(identity, ChairIdentity)
    assert (identity.repo, identity.revision, identity.path) == (None, None, "perlector")
    assert identity.receipt_revision == DIGEST
    assert identity.receipt_revision_kind == "digest-manifest"


def test_resolution_reads_a_real_file_the_same_way_it_reads_a_mapping(tmp_path):
    """`load_models_toml` is what an operator's edit actually goes through."""
    path = write_models_toml(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST)})
    assert (
        load_models_toml(path).chairs["attestator_1"]
        == config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST)}).chairs[
            "attestator_1"
        ]
    )


# --- Fails naming the missing field ------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["source", "repo", "revision", "digest_manifest", "manifest", "serving_recipe", "license_note"],
)
def test_a_huggingface_chair_missing_any_required_field_is_refused_naming_it(tmp_path, field):
    table = hf_chair("attestator_1", DIGEST)
    del table[field]
    with pytest.raises(ConfigurationRefusal) as caught:
        config_of(tmp_path, {"attestator_1": table})
    assert field in str(caught.value)
    assert "attestator_1" in str(caught.value)


@pytest.mark.parametrize(
    "field", ["source", "path", "digest_manifest", "manifest", "serving_recipe", "license_note"]
)
def test_a_local_repository_chair_missing_any_required_field_is_refused_naming_it(tmp_path, field):
    table = local_chair("perlector", DIGEST)
    del table[field]
    with pytest.raises(ConfigurationRefusal) as caught:
        config_of(tmp_path, {"perlector": table}, model_root="model-fixtures")
    assert field in str(caught.value)


def test_a_chair_with_no_recognizable_state_is_refused(tmp_path):
    for state in ("", "enabled", "Configured", None):
        table = hf_chair("attestator_1", DIGEST, state=state)
        with pytest.raises(ConfigurationRefusal, match="configured.*absent"):
            config_of(tmp_path, {"attestator_1": table})


def test_a_blank_field_is_refused_rather_than_read_as_an_omission(tmp_path):
    """The schema has no blank-string sentinels: `adapter_of = ""` is gone, and
    a blanked required field is a malformed pin rather than a quiet default."""
    with pytest.raises(ConfigurationRefusal, match="non-blank"):
        config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST, serving_recipe="  ")})


# --- Revision syntax is per source -------------------------------------------------


@pytest.mark.parametrize(
    "revision",
    [
        "main",  # the branch name the spec names outright
        "refs/heads/main",
        "a" * 39,
        "a" * 41,
        "A" * 40,  # uppercase hex is not the spelling a pin is written in
        "g" * 40,
        "",
        " " + "a" * 39,
    ],
)
def test_a_revision_that_is_not_exactly_forty_lowercase_hex_is_refused(tmp_path, revision):
    with pytest.raises(ConfigurationRefusal, match="40 lowercase"):
        config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST, revision=revision)})


def test_a_digest_manifest_that_is_not_a_lowercase_sha256_is_refused(tmp_path):
    with pytest.raises(ConfigurationRefusal, match="64 lowercase"):
        config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", "d" * 63)})


def test_a_huggingface_chair_may_not_carry_a_local_path(tmp_path):
    with pytest.raises(ConfigurationRefusal, match="forbidden"):
        config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST, path="somewhere")})


def test_a_local_repository_chair_may_not_carry_a_repo_or_a_revision(tmp_path):
    for extra in ({"repo": "org/name"}, {"revision": HF_REVISION}):
        with pytest.raises(ConfigurationRefusal, match="forbidden"):
            config_of(
                tmp_path,
                {"perlector": local_chair("perlector", DIGEST, **extra)},
                model_root="model-fixtures",
            )


def test_a_local_repository_chair_needs_a_configured_model_root(tmp_path):
    """A path with no root to be relative to is not a pin, it is a guess."""
    with pytest.raises(ConfigurationRefusal, match="model_root is required"):
        config_of(tmp_path, {"perlector": local_chair("perlector", DIGEST)})


# --- adapter_of names a base that exists -------------------------------------------


def test_adapter_of_naming_a_base_outside_the_roster_is_refused(tmp_path):
    chairs = {"attestator_1": hf_chair("attestator_1", DIGEST, adapter_of="no_such_base")}
    with pytest.raises(ConfigurationRefusal, match="adapter_of"):
        config_of(tmp_path, chairs)


def test_adapter_of_naming_an_explicitly_absent_base_is_refused(tmp_path):
    """An adapter whose base is declared absent has nothing to ride on, and the
    one thing that must never happen is the bare base answering in its place."""
    chairs = {
        "attestator_1": hf_chair("attestator_1", DIGEST, adapter_of="base"),
        "base": absent_chair(),
    }
    with pytest.raises(ConfigurationRefusal, match="absent"):
        config_of(tmp_path, chairs)


def test_adapter_of_naming_itself_is_refused(tmp_path):
    chairs = {"attestator_1": hf_chair("attestator_1", DIGEST, adapter_of="attestator_1")}
    with pytest.raises(ConfigurationRefusal, match="itself"):
        config_of(tmp_path, chairs)


def test_adapter_of_naming_a_real_sibling_chair_resolves(tmp_path):
    chairs = {
        "attestator_1": hf_chair("attestator_1", DIGEST, adapter_of="base"),
        "base": hf_chair("base", DIGEST),
    }
    assert config_of(tmp_path, chairs).chairs["attestator_1"].adapter_of == "base"


# --- A role the schema has never seen ----------------------------------------------


def test_a_role_the_schema_has_never_seen_resolves_without_a_schema_change(tmp_path):
    """Spec 02: "The registry accepts a role added later without a schema
    change — that is test 1's real requirement." Nothing in `common/chairs/`
    holds a list of role names to add to, which is what makes this pass."""
    chairs = {
        "attestator_1": hf_chair("attestator_1", DIGEST),
        "haruspex_of_the_marginalia": hf_chair("haruspex_of_the_marginalia", DIGEST),
    }
    config = config_of(tmp_path, chairs)
    identity = config.chairs["haruspex_of_the_marginalia"]

    assert isinstance(identity, ChairIdentity)
    assert identity.role == "haruspex_of_the_marginalia"
    # And it is not mistaken for a witness merely by being new.
    assert config.witness_chairs == ("attestator_1",)


def test_a_role_with_no_table_at_all_refuses_naming_the_role(tmp_path):
    config = config_of(tmp_path, {"attestator_1": hf_chair("attestator_1", DIGEST)})
    registry = _registry(config, tmp_path)

    with pytest.raises(UnresolvedChairRefusal) as caught:
        registry.resolve("perlector")
    assert caught.value.chair == "perlector"


def test_an_absent_chair_resolves_to_an_explicit_absence_rather_than_raising(tmp_path):
    config = config_of(tmp_path, {"attestator_1": absent_chair("witness withdrawn")})
    assert config.chairs["attestator_1"] == AbsentChair("attestator_1", "witness withdrawn")


# --- The file itself -----------------------------------------------------------------


def test_an_unknown_top_level_field_is_refused_rather_than_ignored(tmp_path):
    """A misspelt `witness_flor` silently ignored is a floor of zero nobody set."""
    with pytest.raises(ConfigurationRefusal, match="unknown top-level"):
        parse_models_config(
            {"witness_floor": 1, "chairs": {"a_1": hf_chair("a_1", DIGEST)}, "witness_flor": 3}
        )


def test_a_config_with_no_chairs_at_all_is_refused(tmp_path):
    with pytest.raises(ConfigurationRefusal, match="non-empty"):
        parse_models_config({"witness_floor": 1, "chairs": {}})


@pytest.mark.parametrize("floor", [-1, "3", True, None, 1.5])
def test_a_witness_floor_that_is_not_a_non_negative_integer_is_refused(floor):
    with pytest.raises(ConfigurationRefusal, match="witness_floor"):
        parse_models_config({"witness_floor": floor, "chairs": {"a_1": hf_chair("a_1", DIGEST)}})


def _registry(config, tmp_path):
    from common.chairs.registry import ChairRegistry

    return ChairRegistry(config, manifest_root=tmp_path)
