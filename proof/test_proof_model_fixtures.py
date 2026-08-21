"""The fixture chair snapshots, their manifests, and the pins that name them agree.

Three artifacts have to say the same thing about every fixture chair: the bytes
under `config/model-fixtures/<chair>/`, the manifest artifact under
`config/manifests/<chair>.json`, and the `digest_manifest` pin in
`config/models.toml`. Nothing in the pipeline can notice if the checked-in
manifest silently stops describing the checked-in bytes — `ensure()` would refuse
at the first run, which is honest but late, and a pin edited to match a corrupted
snapshot would not be refused at all.

So this rebuilds every fixture from `proof/build_model_fixtures.py` into a
temporary directory and compares, byte for byte, against what is committed.
"""

from pathlib import Path

import pytest

from common.chairs.config import load_models_toml
from common.chairs.models import ChairIdentity
from proof.build_model_fixtures import FIXTURE_CHAIRS, build, fixture_files

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / "config"
MODELS_CONFIG = CONFIG_ROOT / "models.toml"


def test_every_configured_fixture_chair_has_a_generator_entry():
    """A chair configured in models.toml but absent from the generator would be a
    fixture nothing can rebuild, and therefore nothing can check."""
    config = load_models_toml(MODELS_CONFIG)
    configured = {role for role, chair in config.chairs.items() if isinstance(chair, ChairIdentity)}
    assert configured == set(FIXTURE_CHAIRS)


def test_the_checked_in_snapshots_manifests_and_pins_all_agree(tmp_path):
    rebuilt_pins = build(tmp_path / "model-fixtures", tmp_path / "manifests")
    config = load_models_toml(MODELS_CONFIG)

    assert set(rebuilt_pins) == set(FIXTURE_CHAIRS)
    assert {
        path.relative_to(CONFIG_ROOT / "model-fixtures").as_posix()
        for path in (CONFIG_ROOT / "model-fixtures").rglob("*")
        if path.is_file()
    } == {f"{chair}/{name}" for chair in FIXTURE_CHAIRS for name in fixture_files(chair)}
    assert {path.name for path in (CONFIG_ROOT / "manifests").iterdir() if path.is_file()} == {
        f"{chair}.json" for chair in FIXTURE_CHAIRS
    }
    for chair in FIXTURE_CHAIRS:
        for name, data in fixture_files(chair).items():
            committed = (CONFIG_ROOT / "model-fixtures" / chair / name).read_bytes()
            assert committed == data, f"{chair}/{name} on disk is not what the generator writes"
        committed_manifest = (CONFIG_ROOT / "manifests" / f"{chair}.json").read_bytes()
        rebuilt_manifest = (tmp_path / "manifests" / f"{chair}.json").read_bytes()
        assert committed_manifest == rebuilt_manifest, f"{chair}'s manifest artifact has drifted"

        identity = config.chairs[chair]
        assert isinstance(identity, ChairIdentity)
        assert identity.digest_manifest == rebuilt_pins[chair], (
            f"{chair}'s digest_manifest pin does not name its own manifest artifact"
        )


def test_no_two_fixture_chairs_share_a_snapshot():
    """Distinct bytes per chair, so a manifest crossed between two chairs fails."""
    pins = {
        role: chair.digest_manifest
        for role, chair in load_models_toml(MODELS_CONFIG).chairs.items()
        if isinstance(chair, ChairIdentity)
    }
    assert len(set(pins.values())) == len(pins) == len(FIXTURE_CHAIRS)


def test_builder_removes_stale_snapshot_and_manifest_files(tmp_path):
    model_root = tmp_path / "model-fixtures"
    manifest_root = tmp_path / "manifests"
    (model_root / "attestator_1").mkdir(parents=True)
    (model_root / "attestator_1" / "stale.bin").write_bytes(b"stale")
    (model_root / "retired").mkdir()
    (manifest_root / "retired.json").parent.mkdir(parents=True)
    (manifest_root / "retired.json").write_text("stale")

    build(model_root, manifest_root)

    assert not (model_root / "attestator_1" / "stale.bin").exists()
    assert not (model_root / "retired").exists()
    assert not (manifest_root / "retired.json").exists()


def test_builder_does_not_suppress_a_failed_cleanup(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "model-fixtures"
    model_root.mkdir()

    def refused_cleanup(_path: Path) -> None:
        raise PermissionError("injected cleanup refusal")

    monkeypatch.setattr("proof.build_model_fixtures.shutil.rmtree", refused_cleanup)

    with pytest.raises(PermissionError, match="injected cleanup refusal"):
        build(model_root, tmp_path / "manifests")


def test_builder_does_not_read_a_nested_deletion_race_as_an_absent_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_root = tmp_path / "model-fixtures"
    model_root.mkdir()

    def raced_cleanup(_path: Path) -> None:
        raise FileNotFoundError("injected disappearing child")

    monkeypatch.setattr("proof.build_model_fixtures.shutil.rmtree", raced_cleanup)

    with pytest.raises(FileNotFoundError, match="injected disappearing child"):
        build(model_root, tmp_path / "manifests")
