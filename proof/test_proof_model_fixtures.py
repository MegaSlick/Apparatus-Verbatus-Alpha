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
REAL_MODELS_CONFIG = CONFIG_ROOT / "models-real.toml"


def test_every_configured_fixture_chair_has_a_generator_entry():
    """A chair configured in models.toml but absent from the generator would be a
    fixture nothing can rebuild, and therefore nothing can check."""
    config = load_models_toml(MODELS_CONFIG)
    configured = {role for role, chair in config.chairs.items() if isinstance(chair, ChairIdentity)}
    assert configured == set(FIXTURE_CHAIRS)


def test_the_checked_in_snapshots_manifests_and_pins_all_agree(tmp_path):
    rebuilt_pins = build(tmp_path / "model-fixtures", tmp_path / "manifests")
    config = load_models_toml(MODELS_CONFIG)
    real_config = load_models_toml(REAL_MODELS_CONFIG)

    assert set(rebuilt_pins) == set(FIXTURE_CHAIRS)
    assert {
        path.relative_to(CONFIG_ROOT / "model-fixtures").as_posix()
        for path in (CONFIG_ROOT / "model-fixtures").rglob("*")
        if path.is_file()
    } == {f"{chair}/{name}" for chair in FIXTURE_CHAIRS for name in fixture_files(chair)}
    fixture_manifests = {
        chair.manifest for chair in config.chairs.values() if isinstance(chair, ChairIdentity)
    }
    real_manifests = {
        chair.manifest for chair in real_config.chairs.values() if isinstance(chair, ChairIdentity)
    }
    assert fixture_manifests == {f"manifests/{chair}.json" for chair in FIXTURE_CHAIRS}
    assert fixture_manifests.isdisjoint(real_manifests)
    assert {
        path.relative_to(CONFIG_ROOT).as_posix()
        for path in (CONFIG_ROOT / "manifests").rglob("*")
        if path.is_file()
    } == fixture_manifests | real_manifests
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


def test_builder_removes_stale_snapshot_files(tmp_path):
    """`model_root` is this generator's own exclusive directory, so a stale
    entry under any name -- including one for a chair long since dropped from
    `FIXTURE_CHAIRS` -- is safe to wipe outright."""
    model_root = tmp_path / "model-fixtures"
    manifest_root = tmp_path / "manifests"
    (model_root / "attestator_1").mkdir(parents=True)
    (model_root / "attestator_1" / "stale.bin").write_bytes(b"stale")
    (model_root / "retired").mkdir()

    build(model_root, manifest_root)

    assert not (model_root / "attestator_1" / "stale.bin").exists()
    assert not (model_root / "retired").exists()


def test_builder_removes_only_the_manifests_it_owns(tmp_path):
    """`manifest_root` (`config/manifests/`) is NOT exclusive to this generator:
    the real serving roster's own digest manifests are checked in beside the
    fixture ones. A file not named after a current `FIXTURE_CHAIRS` entry --
    a real-roster manifest, or a genuinely retired fixture chair's leftover --
    must survive a rebuild; only the exact files this generator is about to
    rewrite may be touched."""
    model_root = tmp_path / "model-fixtures"
    manifest_root = tmp_path / "manifests"
    manifest_root.mkdir(parents=True)
    (manifest_root / "retired.json").write_text("stale fixture leftover")
    (manifest_root / "qwen3.8-27B.json").write_text("a real-roster manifest")

    build(model_root, manifest_root)

    assert (manifest_root / "retired.json").read_text() == "stale fixture leftover"
    assert (manifest_root / "qwen3.8-27B.json").read_text() == "a real-roster manifest"


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
