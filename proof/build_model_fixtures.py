"""Render the local-repository fixture snapshots `config/models.toml` pins, and
write each one's digest-manifest artifact.

Run from the repository root:

    python3 proof/build_model_fixtures.py

**These are not models.** A few deterministic bytes stand in for a model
repository, exactly as `proof/build_fixture.py`'s synthetic pages stand in for a
real scanned register — so the seat framework's fixture roster can round-trip
resolve -> ensure -> verify -> receipt entirely offline, with no network call and
nothing downloaded.

The generator lives here, beside `build_fixture.py`, because building fixtures is
what `proof/` is for. What it *writes* lives under `config/`, because a seat's
`path` and `manifest` are resolved relative to `config/models.toml` and a fixture
that sat anywhere else could not be pinned by it. `test_proof_model_fixtures.py`
re-runs this in a temporary directory and refuses any drift between what it
produces and what is checked in, so the pins below can never quietly stop
describing the bytes on disk.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.chairs.manifests import build_manifest, manifest_digest, write_manifest  # noqa: E402
from common.contracts.canonical import digest_bytes  # noqa: E402

CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config"
MODEL_ROOT = CONFIG_ROOT / "model-fixtures"
MANIFEST_ROOT = CONFIG_ROOT / "manifests"

# One directory per configured fixture seat. Distinct content per seat, so their
# digest manifests actually differ rather than several seats sharing one fixture
# by accident — which would make a mismatch between two seats invisible.
FIXTURE_CHAIRS = (
    "attestator_1",
    "attestator_2",
    "attestator_3",
    "designator_structure",
    "perlector",
)


def fixture_files(chair: str) -> dict[str, bytes]:
    """The bytes one fixture seat's snapshot holds, derived from its own name."""
    return {"identity.txt": f"fixture seat: {chair}\n".encode()}


def build(model_root: Path, manifest_root: Path) -> dict[str, str]:
    """Write every fixture snapshot and manifest; return each seat's pin."""
    pins: dict[str, str] = {}
    for chair in FIXTURE_CHAIRS:
        directory = model_root / chair
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in sorted(fixture_files(chair).items()):
            (directory / name).write_bytes(data)
        manifest = build_manifest(directory)
        path = manifest_root / f"{chair}.json"
        pin = write_manifest(manifest, path)
        # The pin is the digest of the artifact's exact canonical bytes, which is
        # what `read_manifest` checks; asserting it here keeps the two spellings
        # of "the pin" from drifting inside the generator itself.
        assert pin == manifest_digest(manifest) == digest_bytes(path.read_bytes())
        pins[chair] = pin
    return pins


def main() -> int:
    for chair, pin in sorted(build(MODEL_ROOT, MANIFEST_ROOT).items()):
        print(f"{chair}: digest_manifest = {pin}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
