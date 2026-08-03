"""The fixture seat snapshots, their manifests, and the pins that name them agree.

Three artifacts have to say the same thing about every fixture seat: the bytes
under `config/model-fixtures/<seat>/`, the manifest artifact under
`config/manifests/<seat>.json`, and the `digest_manifest` pin in
`config/models.toml`. Nothing in the pipeline can notice if the checked-in
manifest silently stops describing the checked-in bytes — `ensure()` would refuse
at the first run, which is honest but late, and a pin edited to match a corrupted
snapshot would not be refused at all.

So this rebuilds every fixture from `proof/build_model_fixtures.py` into a
temporary directory and compares, byte for byte, against what is committed.
"""

from pathlib import Path

from common.seats.config import load_models_toml
from common.seats.models import SeatIdentity
from proof.build_model_fixtures import FIXTURE_SEATS, build, fixture_files

ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / "config"
MODELS_CONFIG = CONFIG_ROOT / "models.toml"


def test_every_configured_fixture_seat_has_a_generator_entry():
    """A seat configured in models.toml but absent from the generator would be a
    fixture nothing can rebuild, and therefore nothing can check."""
    config = load_models_toml(MODELS_CONFIG)
    configured = {role for role, seat in config.seats.items() if isinstance(seat, SeatIdentity)}
    assert configured == set(FIXTURE_SEATS)


def test_the_checked_in_snapshots_manifests_and_pins_all_agree(tmp_path):
    rebuilt_pins = build(tmp_path / "model-fixtures", tmp_path / "manifests")
    config = load_models_toml(MODELS_CONFIG)

    assert set(rebuilt_pins) == set(FIXTURE_SEATS)
    for seat in FIXTURE_SEATS:
        for name, data in fixture_files(seat).items():
            committed = (CONFIG_ROOT / "model-fixtures" / seat / name).read_bytes()
            assert committed == data, f"{seat}/{name} on disk is not what the generator writes"
        committed_manifest = (CONFIG_ROOT / "manifests" / f"{seat}.json").read_bytes()
        rebuilt_manifest = (tmp_path / "manifests" / f"{seat}.json").read_bytes()
        assert committed_manifest == rebuilt_manifest, f"{seat}'s manifest artifact has drifted"

        identity = config.seats[seat]
        assert isinstance(identity, SeatIdentity)
        assert identity.digest_manifest == rebuilt_pins[seat], (
            f"{seat}'s digest_manifest pin does not name its own manifest artifact"
        )


def test_no_two_fixture_seats_share_a_snapshot():
    """Distinct bytes per seat, so a manifest crossed between two seats fails."""
    pins = {
        role: seat.digest_manifest
        for role, seat in load_models_toml(MODELS_CONFIG).seats.items()
        if isinstance(seat, SeatIdentity)
    }
    assert len(set(pins.values())) == len(pins) == len(FIXTURE_SEATS)
