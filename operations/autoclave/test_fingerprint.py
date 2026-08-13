import hashlib
import re
from pathlib import Path

from operations.autoclave.fingerprint import INPUTS, digest


def test_the_declared_inputs_cover_every_baked_file():
    root = Path(__file__).parents[2]
    dockerfile = (root / "operations/autoclave/Dockerfile").read_text()
    # Every COPY must be understood, not merely the two-token spelling. The old pattern
    # required exactly `COPY <src> <dst>`, so `COPY --chown=agent:agent src dst`, a
    # multi-source `COPY a b dir/`, or a continued line simply dropped out of `copied` --
    # and both comparisons below still passed. A file baked in that way would never reach
    # INPUTS, the fingerprint would stop moving when it changed, and the launcher would
    # accept a stale image as current. That is the same defect its stale-image test names.
    statements = re.findall(r"(?m)^COPY\s+(.*)$", dockerfile)
    copied = set()
    for statement in statements:
        tokens = [token for token in statement.split() if not token.startswith("--")]
        assert len(tokens) >= 2, f"this COPY was not understood: {statement!r}"
        copied.update(tokens[:-1])
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


def test_more_than_one_argument_is_refused_rather_than_silently_ignored(tmp_path):
    """Two arguments used to fall through to this repository's own root.

    The caller named a tree, got a digest for a different one, and nothing said so.
    """

    import subprocess
    import sys

    script = Path(__file__).with_name("fingerprint.py")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout.strip() == "", "a digest was printed for an unrequested tree"
    assert "at most one repository root" in result.stderr


def test_moving_bytes_between_two_inputs_changes_the_fingerprint(tmp_path):
    """The property, asserted without restating the algorithm.

    `test_paths_are_part_of_the_fingerprint` below rebuilds `digest`'s exact update
    sequence, so anyone editing `digest` will edit it to match and it will then agree with
    whatever the implementation now does. This one cannot be satisfied that way: if the
    path were not bound to its bytes, swapping two inputs' contents would leave the digest
    unchanged.
    """

    first, second = INPUTS[0], INPUTS[1]
    for relative in INPUTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())

    original = digest(tmp_path)
    (tmp_path / first).write_bytes(second.encode())
    (tmp_path / second).write_bytes(first.encode())

    assert digest(tmp_path) != original, "the fingerprint ignores which path holds which bytes"


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
