from pathlib import Path

from operations.autoclave.fingerprint import INPUTS, digest


def test_every_declared_input_changes_the_fingerprint(tmp_path):
    for relative in INPUTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())

    original = digest(tmp_path)
    for relative in INPUTS:
        path = tmp_path / relative
        before = path.read_bytes()
        path.write_bytes(before + b" changed")
        assert digest(tmp_path) != original, f"{relative} does not affect the fingerprint"
        path.write_bytes(before)


def test_paths_are_part_of_the_fingerprint(tmp_path):
    for relative in INPUTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"same bytes")

    assert digest(tmp_path) != digest(Path(__file__).parents[2])
