import hashlib

from operations.autoclave.fingerprint import INPUTS, digest


def test_the_declared_inputs_cover_every_baked_file():
    assert set(INPUTS) == {
        ".dockerignore",
        "operations/autoclave/Dockerfile",
        "operations/autoclave/agent-brief.md",
        "operations/autoclave/fingerprint.py",
        "operations/autoclave/refresh_claude_token.py",
        "requirements-dev.txt",
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
