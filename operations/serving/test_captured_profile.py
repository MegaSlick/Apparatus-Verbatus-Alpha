"""The ``captured`` serving-recipe kind, offline and fake-first.

A captured row says a witness chair is answered by another chair's retained
response rather than by a launch of its own. Every test here runs against
:mod:`operations.serving.fakes` or the committed config files; nothing starts a
process, opens a socket, or resolves a model.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from common.chairs.config import load_models_toml, parse_models_config
from common.chairs.errors import ServingRecipeRefusal
from common.chairs.models import AbsentChair, ChairIdentity, ServingDetails, VerifiedSnapshot
from common.chairs.receipts import build_receipt
from common.witness_adapters import CAPTURE_WITNESS_ADAPTER_NAMES, KNOWN_WITNESS_ADAPTER_NAMES
from operations.pod.preflight import load_placement_table

from .client import ServingModeRefusal, serving_mode_for
from .config import (
    CapturedProfile,
    FixtureProfile,
    ServingConfigInputs,
    ServingProfile,
    ServingRecipes,
    UnsupportedProfile,
    chair_preflight_identity_digest,
    load_serving_recipes,
    parse_serving_recipes,
    profile_preflight_digest,
    verify_recipes_cover_chairs,
)
from .errors import ServingConfigurationError
from .fakes import FakeBlobStore, FakeEndpoint, FakeLauncher, FakePackages, FakePublisher
from .manager import ServingManager
from .residency import FileResidencyLease

ROOT = Path(__file__).resolve().parents[2]
TIER = "generic-48gb"
TIERS = ("generic-24gb", "generic-48gb", "generic-80gb-plus")
REVISION = "a" * 40
MANIFEST = "b" * 64
SOURCE = "designator_structure"
WITNESS = "attestator_1"


# --- builders -----------------------------------------------------------------


def _identity(
    role: str,
    recipe: str,
    *,
    witness_adapter: str | None = None,
    witness_scope: str | None = None,
    repo: str = "example/chandra",
) -> ChairIdentity:
    return ChairIdentity(
        role=role,
        source="huggingface",
        repo=repo,
        path=None,
        revision=REVISION,
        digest_manifest=MANIFEST,
        manifest="manifests/chandra.json",
        adapter_of=None,
        serving_recipe=recipe,
        license_note="test identity only",
        witness_adapter=witness_adapter,
        witness_scope=witness_scope,
    )


def _source_chair() -> ChairIdentity:
    return _identity(SOURCE, "recipe-designator")


def _captured_chair(adapter: str = "chandra-capture.v1") -> ChairIdentity:
    return _identity(WITNESS, "recipe-witnesses", witness_adapter=adapter, witness_scope="page")


def _vllm_row(
    *, recipe: str, chair: str, served_model_id: str, port: int, tier: str = TIER
) -> dict[str, object]:
    return {
        "kind": "vllm",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "host": "127.0.0.1",
        "port": port,
        "served_model_id": served_model_id,
        "dtype": "bfloat16",
        "seed": 7,
        "required_packages": {"vllm": "0.test"},
        "max_model_len": 2048,
        "max_num_seqs": 1,
        "max_num_batched_tokens": 256,
        "gpu_memory_utilization": "0.85",
        "min_pixels": 1,
        "max_pixels": 1024,
        "enable_prefix_caching": True,
        "enforce_eager": False,
        "trust_remote_code": False,
        "enable_tower_connector_lora": False,
        "max_lora_rank": 16,
        "generation_config": "vllm",
        "preflight_state": "unproven",
        "startup_timeout_seconds": 3,
        "poll_interval_seconds": 1,
        "request_timeout_seconds": 30,
        "readiness_probe": {
            "kind": "chat-completions",
            "request_json": '{"messages":[{"role":"user","content":"READY"}],"max_tokens":4}',
        },
    }


def _proven(row: dict[str, object], chair: ChairIdentity) -> dict[str, object]:
    sealed = {**row, "preflight_state": "proven"}
    sealed["preflight_identity_digest"] = chair_preflight_identity_digest(chair)
    sealed["preflight_digest"] = profile_preflight_digest(sealed)
    return sealed


def _captured_row(
    *,
    recipe: str = "recipe-witnesses",
    chair: str = WITNESS,
    tier: str = TIER,
    captured_from: str = SOURCE,
) -> dict[str, object]:
    return {
        "kind": "captured",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "captured_from": captured_from,
    }


def _fixture_row(*, recipe: str, chair: str, tier: str = TIER) -> dict[str, object]:
    return {
        "kind": "fixture",
        "recipe": recipe,
        "chair": chair,
        "tier": tier,
        "description": "walking-skeleton stand-in",
    }


def _recipes(*rows: dict[str, object]) -> ServingRecipes:
    return parse_serving_recipes({"schema": "serving-recipes.v1", "profiles": list(rows)})


def _roster_row(identity: ChairIdentity) -> dict[str, object]:
    row: dict[str, object] = {
        "state": "configured",
        "source": identity.source,
        "repo": identity.repo,
        "revision": identity.revision,
        "digest_manifest": identity.digest_manifest,
        "manifest": identity.manifest,
        "serving_recipe": identity.serving_recipe,
        "license_note": identity.license_note,
    }
    if identity.witness_adapter is not None:
        row["witness_adapter"] = identity.witness_adapter
        row["witness_scope"] = identity.witness_scope
    return row


def _roster(*identities: ChairIdentity, absent: tuple[str, ...] = ()):
    chairs: dict[str, object] = {identity.role: _roster_row(identity) for identity in identities}
    for role in absent:
        chairs[role] = {"state": "absent", "reason": "absent for this test"}
    return parse_models_config({"witness_floor": 1, "chairs": chairs}, source_path=None)


def _paired_catalogue(
    source: ChairIdentity, witness: ChairIdentity, tiers: tuple[str, ...] = TIERS
) -> ServingRecipes:
    """A source chair served at every tier and a witness captured from it."""

    rows: list[dict[str, object]] = []
    for tier in tiers:
        rows.append(
            _vllm_row(
                recipe=source.serving_recipe,
                chair=source.role,
                served_model_id="source-api",
                port=8101,
                tier=tier,
            )
        )
        rows.append(
            _captured_row(
                recipe=witness.serving_recipe,
                chair=witness.role,
                tier=tier,
                captured_from=source.role,
            )
        )
    return _recipes(*rows)


# --- the row itself -----------------------------------------------------------


def test_a_captured_row_parses_to_a_captured_profile_with_its_source_chair() -> None:
    catalogue = _recipes(_captured_row())
    (profile,) = catalogue.profiles
    assert isinstance(profile, CapturedProfile)
    assert profile.kind == "captured"
    assert profile.key == ("recipe-witnesses", WITNESS, TIER)
    assert profile.captured_from == SOURCE
    assert catalogue.for_identity(_captured_chair(), TIER) is profile


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        ({"host": "127.0.0.1"}, "unknown field(s) ['host']"),
        ({"preflight_state": "proven"}, "unknown field(s) ['preflight_state']"),
        ({"description": "a fixture field"}, "unknown field(s) ['description']"),
    ),
)
def test_a_captured_row_refuses_every_launch_field(change: dict, expected: str) -> None:
    """A row that is never launched may not carry a flag, a mark or a port:
    in review those read as planning values for a chair nothing starts."""

    with pytest.raises(ServingConfigurationError, match="never launched") as refusal:
        _recipes({**_captured_row(), **change})
    assert expected in str(refusal.value)


def test_a_captured_row_without_a_source_chair_is_refused() -> None:
    row = _captured_row()
    del row["captured_from"]
    with pytest.raises(
        ServingConfigurationError, match="missing field\\(s\\) \\['captured_from'\\]"
    ):
        _recipes(row)


@pytest.mark.parametrize("value", ("", "  ", " designator_structure", 7))
def test_a_captured_row_source_must_be_an_exact_chair_name(value: object) -> None:
    with pytest.raises(ServingConfigurationError, match="captured_from must be a non-blank"):
        _recipes(_captured_row(captured_from=value))  # type: ignore[arg-type]


def test_a_chair_cannot_be_captured_from_itself() -> None:
    with pytest.raises(ServingConfigurationError, match="names itself as captured_from"):
        _recipes(_captured_row(captured_from=WITNESS))


@pytest.mark.parametrize("chair", ("designator_structure", "perlector", "secondary_proposer"))
def test_only_an_attestator_chair_may_be_captured(chair: str) -> None:
    """A capture is a Testimonium; nothing captures the proposer or the reader."""

    with pytest.raises(ServingConfigurationError, match="not an Attestator chair"):
        _recipes(_captured_row(chair=chair, captured_from="some_other_chair"))


def test_a_captured_row_owns_no_endpoint_or_alias_to_collide_on() -> None:
    """The launch-only collision rules do not see a captured row, so it can sit
    beside a vllm row on any port without inventing a serving claim."""

    catalogue = _recipes(
        _vllm_row(recipe="recipe-designator", chair=SOURCE, served_model_id="x", port=8000),
        _captured_row(),
    )
    kinds = {type(profile) for profile in catalogue.profiles}
    assert kinds == {ServingProfile, CapturedProfile}


def test_a_duplicate_captured_key_is_still_refused() -> None:
    with pytest.raises(ServingConfigurationError, match="duplicate a recipe/chair/tier key"):
        _recipes(_captured_row(), _captured_row())


# --- serving_mode_for ---------------------------------------------------------


def test_all_captured_rows_resolve_to_captured_with_or_without_a_tier() -> None:
    chair = _captured_chair()
    catalogue = _recipes(*(_captured_row(tier=tier) for tier in TIERS))
    assert serving_mode_for(catalogue, chair, None) == "captured"
    assert serving_mode_for(catalogue, chair, TIER) == "captured"
    # Like the fixture posture, a captured chair has no serving moment a tier
    # could shape, so an unconfigured tier is not a refusal either.
    assert serving_mode_for(catalogue, chair, "tier-does-not-exist") == "captured"


def test_captured_rows_naming_different_sources_across_tiers_refuse() -> None:
    chair = _captured_chair()
    catalogue = _recipes(
        _captured_row(tier="generic-24gb", captured_from=SOURCE),
        _captured_row(tier="generic-48gb", captured_from="attestator_3"),
    )
    with pytest.raises(ServingModeRefusal) as refusal:
        serving_mode_for(catalogue, chair, TIER)
    assert refusal.value.code == "SERVING_MODE_UNRESOLVED"
    assert "exactly one source" in refusal.value.detail
    assert "'attestator_3'" in refusal.value.detail and f"'{SOURCE}'" in refusal.value.detail


def test_a_chair_captured_at_one_tier_and_live_at_another_refuses_the_captured_tier() -> None:
    """Mirror of the fixture/live mix: the live tier answers live, the captured
    tier refuses by name, and without a tier nothing is resolved."""

    chair = _captured_chair()
    live = _proven(
        _vllm_row(
            recipe=chair.serving_recipe,
            chair=chair.role,
            served_model_id="x",
            port=8000,
            tier="tier-a",
        ),
        chair,
    )
    catalogue = _recipes(live, _captured_row(tier="tier-b"))
    assert serving_mode_for(catalogue, chair, "tier-a") == "live"
    with pytest.raises(ServingModeRefusal) as refusal:
        serving_mode_for(catalogue, chair, "tier-b")
    assert refusal.value.code == "SERVING_MODE_UNRESOLVED"
    assert "half captured" in refusal.value.detail
    with pytest.raises(ServingModeRefusal) as unresolved:
        serving_mode_for(catalogue, chair, None)
    assert unresolved.value.code == "SERVING_MODE_UNRESOLVED"


def test_a_captured_and_fixture_mix_for_one_chair_resolves_nowhere() -> None:
    # The refusal at each tier names the posture actually sitting at the
    # *other* tier, never a fixed guess of "live" — this chair is never live
    # anywhere in this catalogue, so "live" must not appear in either message.
    chair = _captured_chair()
    catalogue = _recipes(
        _captured_row(tier="tier-a"),
        _fixture_row(recipe=chair.serving_recipe, chair=chair.role, tier="tier-b"),
    )
    with pytest.raises(ServingModeRefusal, match="a live serving profile needs"):
        serving_mode_for(catalogue, chair, None)
    with pytest.raises(ServingModeRefusal, match="tier='tier-a'.*another tier is fixture"):
        serving_mode_for(catalogue, chair, "tier-a")
    with pytest.raises(ServingModeRefusal, match="tier='tier-b'.*another tier is captured"):
        serving_mode_for(catalogue, chair, "tier-b")


# --- the manager never launches it --------------------------------------------


class _RecordingRegistry:
    """The fakes' registry, plus the two facts the refusal-order claim needs."""

    def __init__(self, identities: dict[str, ChairIdentity], tmp_path: Path) -> None:
        self.identities = dict(identities)
        self.snapshots = {
            role: VerifiedSnapshot(identity, tmp_path / role, identity.digest_manifest)
            for role, identity in identities.items()
        }
        self.ensure_calls: list[str] = []
        self.refusals: list[tuple[str, str]] = []

    def resolve(self, role: str) -> ChairIdentity:
        return self.identities[role]

    def ensure(self, identity: ChairIdentity) -> VerifiedSnapshot:
        self.ensure_calls.append(identity.role)
        return self.snapshots[identity.role]

    def receipt(self, identity: ChairIdentity, details: ServingDetails):
        return build_receipt(identity, details)

    def refuse_recipe_start(self, identity: ChairIdentity, difference: str) -> None:
        self.refusals.append((identity.role, difference))
        raise ServingRecipeRefusal(identity.role, difference)


def test_the_manager_refuses_to_launch_a_captured_row_by_name_before_any_snapshot(
    tmp_path: Path,
) -> None:
    chair = _captured_chair()
    catalogue = _recipes(_captured_row())
    blob_store = FakeBlobStore(tmp_path / "blobs")
    endpoint = FakeEndpoint(served_model_id="never-served", blob_store=blob_store)
    launcher = FakeLauncher(endpoint)
    registry = _RecordingRegistry({chair.role: chair}, tmp_path)
    publisher = FakePublisher()
    manager = ServingManager(
        registry=registry,
        recipes=catalogue,
        config_inputs=ServingConfigInputs("1" * 64, "2" * 64),
        launcher=launcher,
        http=endpoint,
        receipt_publisher=publisher,
        log_root=tmp_path / "logs",
        package_inspector=FakePackages({"vllm": "0.test"}),
        residency_lease=FileResidencyLease(tmp_path / "pod-gpu.lock"),
    )

    with pytest.raises(ServingRecipeRefusal, match="captured serving profile") as refusal:
        manager.start(chair, TIER)

    message = str(refusal.value)
    assert f"captured from chair '{SOURCE}'" in message
    assert "never launched" in message
    assert "no serving process was started" in message
    assert launcher.processes == [] and launcher.calls == []
    assert publisher.calls == []
    assert endpoint.requests == []
    # Refused at the recipe door, not after a snapshot or pin check: otherwise
    # an operator would see a checkpoint failure instead of the capture cause.
    assert registry.ensure_calls == []
    assert registry.refusals[0][0] == chair.role
    # No lease was ever taken for a chair that is never launched.
    assert manager._residency_handle is None


# --- roster reconciliation ----------------------------------------------------


def test_a_witness_captured_from_a_served_chair_with_the_same_model_reconciles() -> None:
    source, witness = _source_chair(), _captured_chair()
    verify_recipes_cover_chairs(_roster(source, witness), _paired_catalogue(source, witness), TIERS)


def test_a_capture_from_an_absent_or_unknown_chair_is_refused() -> None:
    source, witness = _source_chair(), _captured_chair()
    catalogue = _paired_catalogue(source, witness)
    absent = _roster(witness, absent=(SOURCE,))
    unexpected = ServingConfigurationError  # coverage refuses the source's rows first
    with pytest.raises(unexpected, match="unexpected="):
        verify_recipes_cover_chairs(absent, catalogue, TIERS)
    # With coverage satisfied by a stand-in, the capture-specific refusal shows.
    only_witness = _recipes(*(_captured_row(tier=tier, captured_from="nobody") for tier in TIERS))
    with pytest.raises(ServingConfigurationError, match="does not configure; an absent or unknown"):
        verify_recipes_cover_chairs(_roster(witness), only_witness, TIERS)


@pytest.mark.parametrize("source_kind", ("fixture", "unsupported", "captured"))
def test_a_capture_from_a_chair_that_is_not_served_at_that_tier_is_refused(
    source_kind: str,
) -> None:
    """A fixture, unsupported or captured source gives no response to retain."""

    source, witness = _source_chair(), _captured_chair()
    rows: list[dict[str, object]] = []
    for tier in TIERS:
        if source_kind == "fixture":
            rows.append(_fixture_row(recipe=source.serving_recipe, chair=source.role, tier=tier))
        elif source_kind == "unsupported":
            rows.append(
                {
                    "kind": "unsupported",
                    "recipe": source.serving_recipe,
                    "chair": source.role,
                    "tier": tier,
                    "reason": "no engine",
                }
            )
        else:
            # A captured source must itself be a witness chair; use one, and
            # capture it from the witness so every named chair is configured
            # and the only fault is that no vllm row exists at this tier.
            source = replace(source, role="attestator_2")
            rows.append(
                _captured_row(
                    recipe=source.serving_recipe,
                    chair=source.role,
                    tier=tier,
                    captured_from=witness.role,
                )
            )
        rows.append(
            _captured_row(
                recipe=witness.serving_recipe,
                chair=witness.role,
                tier=tier,
                captured_from=source.role,
            )
        )
    if source_kind == "captured":
        source = replace(source, witness_adapter="chandra-capture.v1", witness_scope="page")
    catalogue = _recipes(*rows)
    with pytest.raises(ServingConfigurationError, match="only a chair with a vllm row"):
        verify_recipes_cover_chairs(_roster(source, witness), catalogue, TIERS)


def test_a_capture_from_a_different_model_is_refused_naming_the_differing_facts() -> None:
    source = _source_chair()
    witness = replace(_captured_chair(), repo="example/another-model")
    with pytest.raises(ServingConfigurationError, match="cannot stand in for a different model"):
        verify_recipes_cover_chairs(
            _roster(source, witness), _paired_catalogue(source, witness), TIERS
        )
    with pytest.raises(ServingConfigurationError, match="\\['repo'\\]"):
        verify_recipes_cover_chairs(
            _roster(source, witness), _paired_catalogue(source, witness), TIERS
        )


def test_a_captured_chair_must_declare_a_capture_adapter() -> None:
    source, witness = _source_chair(), _captured_chair(adapter="chandra.v1")
    with pytest.raises(ServingConfigurationError, match="not a capture adapter"):
        verify_recipes_cover_chairs(
            _roster(source, witness), _paired_catalogue(source, witness), TIERS
        )


def test_a_chair_with_a_capture_adapter_may_not_be_served_through_a_vllm_row() -> None:
    """The reverse: a capture adapter reads another chair's bytes and has
    nothing to serve through, so a vllm row for it is a launch nobody asked for."""

    source, witness = _source_chair(), _captured_chair()
    rows: list[dict[str, object]] = []
    for tier in TIERS:
        rows.append(
            _vllm_row(
                recipe=source.serving_recipe,
                chair=source.role,
                served_model_id="source-api",
                port=8101,
                tier=tier,
            )
        )
        rows.append(
            _vllm_row(
                recipe=witness.serving_recipe,
                chair=witness.role,
                served_model_id="witness-api",
                port=8102,
                tier=tier,
            )
        )
    with pytest.raises(ServingConfigurationError, match="nothing to serve through"):
        verify_recipes_cover_chairs(_roster(source, witness), _recipes(*rows), TIERS)


# --- the committed real roster and catalogue ------------------------------------


def _real_pair():
    models = load_models_toml(ROOT / "config" / "models-real.toml")
    catalogue = load_serving_recipes(ROOT / "config" / "serving_recipes_real.toml")
    placement = load_placement_table(ROOT / "config" / "pod_placement.toml")
    return models, catalogue, tuple(tier.identifier for tier in placement.tiers)


def test_the_real_catalogue_captures_attestator_1_from_the_structure_chair() -> None:
    models, catalogue, tiers = _real_pair()
    verify_recipes_cover_chairs(models, catalogue, tiers)
    witness = models.chairs["attestator_1"]
    assert isinstance(witness, ChairIdentity)
    assert witness.witness_adapter == "chandra-capture.v1"
    assert witness.witness_scope == "page"
    for tier in tiers:
        profile = catalogue.for_identity(witness, tier)
        assert isinstance(profile, CapturedProfile)
        assert profile.captured_from == "designator_structure"
    assert serving_mode_for(catalogue, witness, None) == "captured"
    # Both chairs pin the one Chandra repository at the one revision.
    source = models.chairs["designator_structure"]
    assert isinstance(source, ChairIdentity)
    assert (witness.repo, witness.revision) == (source.repo, source.revision)


def test_the_real_roster_keeps_the_secondary_proposer_absent_by_ruling() -> None:
    models, catalogue, _tiers = _real_pair()
    secondary = models.chairs["secondary_proposer"]
    assert isinstance(secondary, AbsentChair)
    assert "2026-08-12" in secondary.reason
    assert [
        profile for profile in catalogue.profiles if profile.chair == "secondary_proposer"
    ] == []
    assert not any(isinstance(profile, UnsupportedProfile) for profile in catalogue.profiles)


def test_every_other_real_chair_is_still_an_unproven_vllm_row() -> None:
    models, catalogue, tiers = _real_pair()
    configured = [
        identity for identity in models.chairs.values() if isinstance(identity, ChairIdentity)
    ]
    assert len(catalogue.profiles) == len(configured) * len(tiers)
    for identity in configured:
        if identity.role == "attestator_1":
            continue
        for tier in tiers:
            profile = catalogue.for_identity(identity, tier)
            assert isinstance(profile, ServingProfile), identity.role
            assert profile.preflight_state == "unproven"
            assert profile.required_packages["vllm"] == "0.10.1"
        assert serving_mode_for(catalogue, identity, tiers[0]) == "live"


def test_the_shipped_real_catalogue_mixes_postures_across_witness_chairs() -> None:
    """Known blocker, measured rather than discovered on a rented pod.

    ``pipeline.3_attestatores.run.witness_serving_modes`` refuses to start a
    run whose witness chairs resolve to more than one serving posture (one
    run, one way of reading its witnesses). With the shipped real pair,
    ``attestator_1`` is ``captured`` while every other configured witness
    chair is ``live`` -- exactly the mix that function refuses. This is a
    named D5 gap (teach that function that ``captured`` coexists with
    ``live``), not something D2 can close: ``witness_serving_modes`` lives in
    a file outside this unit's ownership. This test reproduces its own
    posture computation against the committed config so the refusal is
    proven here rather than surprising an operator at Attestatores start.
    """
    models, catalogue, tiers = _real_pair()
    modes = {
        role: serving_mode_for(catalogue, chair, tiers[0])
        for role, chair in models.chairs.items()
        if role in models.witness_chairs and isinstance(chair, ChairIdentity)
    }
    assert modes["attestator_1"] == "captured"
    assert set(modes.values()) - {"captured"} == {"live"}
    postures = {mode for mode in modes.values()}
    assert len(postures) > 1, (
        "if this now holds, the D5 mixed-posture reconciliation has landed and "
        "pipeline/3_attestatores/run.py::witness_serving_modes should be re-checked "
        "for whether it still refuses this catalogue"
    )


def test_the_fixture_catalogue_is_untouched_by_the_captured_kind() -> None:
    models = load_models_toml(ROOT / "config" / "models.toml")
    catalogue = load_serving_recipes(ROOT / "config" / "serving_recipes.toml")
    assert all(isinstance(profile, FixtureProfile) for profile in catalogue.profiles)
    assert not any(
        getattr(chair, "witness_adapter", None) in CAPTURE_WITNESS_ADAPTER_NAMES
        for chair in models.chairs.values()
    )
    assert CAPTURE_WITNESS_ADAPTER_NAMES < KNOWN_WITNESS_ADAPTER_NAMES
