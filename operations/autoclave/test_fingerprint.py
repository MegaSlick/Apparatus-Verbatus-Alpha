import hashlib
import re
from pathlib import Path

from operations.autoclave.fingerprint import INPUTS, digest


def test_the_declared_inputs_cover_every_baked_file():
    root = Path(__file__).parents[2]
    dockerfile = (root / "operations/autoclave/Dockerfile").read_text()
    copied = set(re.findall(r"(?m)^COPY\s+(\S+)\s+\S+\s*$", dockerfile))
    admitted = {
        line[1:]
        for line in (root / ".dockerignore").read_text().splitlines()
        if line.startswith("!") and not line.endswith("/")
    }

    assert copied == admitted
    assert set(INPUTS) == copied | {
        ".dockerignore",
        "operations/autoclave/Dockerfile",
        "operations/autoclave/fingerprint.py",
    }


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

    expected = hashlib.sha256()
    for relative in INPUTS:
        data = (tmp_path / relative).read_bytes()
        expected.update(relative.encode("utf-8"))
        expected.update(b"\0")
        expected.update(len(data).to_bytes(8, "big"))
        expected.update(data)

    assert digest(tmp_path) == expected.hexdigest()
