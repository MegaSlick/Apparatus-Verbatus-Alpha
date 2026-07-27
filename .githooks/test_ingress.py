"""Integration tests for the staged/history repository ingress check."""

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent
SCANNER = HOOKS / "check_ingress.py"
ONE_MIB = 1_048_576


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )


def run_scan(repo, *args):
    return subprocess.run(
        [sys.executable, str(SCANNER), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def write(repo, path, data):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        target.write_bytes(data)
    return target


def stage(repo, *paths):
    git(repo, "add", "--", *paths)


def commit(repo, message):
    git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Ingress Test")
    git(tmp_path, "config", "user.email", "ingress@example.invalid")
    return tmp_path


def runpod_secret():
    # Kept discontinuous so this test file does not itself contain a token.
    return "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"


def generic_secret():
    return "Ab3dE5fG7hJ9kL2mN4pQ6rS8tU"


def fixture_manifest(path, data, *, digest=None, media_type="image/png"):
    digest = digest or hashlib.sha256(data).hexdigest()
    return f"""\
version = 1

[[fixture]]
path = "{path}"
sha256 = "{digest}"
bytes = {len(data)}
media_type = "{media_type}"
source = "public test fixture"
reason = "scanner integration test"
"""


def test_staged_state_is_scanned_instead_of_working_file(repo):
    path = write(repo, "app.py", "value = 1\n")
    stage(repo, "app.py")
    secret = runpod_secret()
    path.write_text(f'RUNPOD_API_KEY = "{secret}"\n', encoding="utf-8")
    stage(repo, "app.py")
    path.write_text("value = 2\n", encoding="utf-8")

    blocked = run_scan(repo, "--staged")
    assert blocked.returncode == 1
    assert secret not in blocked.stderr
    assert run_scan(repo, "--worktree").returncode == 0

    stage(repo, "app.py")
    path.write_text(f'RUNPOD_API_KEY = "{secret}"\n', encoding="utf-8")
    clean_index = run_scan(repo, "--staged")
    assert clean_index.returncode == 0


@pytest.mark.parametrize(
    "document",
    [
        '{{"api_key": "{secret}"}}\n',
        '"api_key" = "{secret}"\n',
    ],
)
def test_quoted_json_and_toml_credential_keys_are_blocked(repo, document):
    secret = generic_secret()
    write(repo, "settings.txt", document.format(secret=secret))
    stage(repo, "settings.txt")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[literal-credential]" in result.stderr
    assert secret not in result.stderr


def test_worktree_mode_scans_untracked_files(repo):
    secret = generic_secret()
    write(repo, "untracked.json", f'{{"api_key": "{secret}"}}\n')
    assert run_scan(repo, "--staged").returncode == 0
    result = run_scan(repo, "--worktree")
    assert result.returncode == 1
    assert "[literal-credential]" in result.stderr


def test_forced_add_from_ignored_private_area_is_blocked(repo):
    write(repo, ".gitignore", "private/*\n")
    secret = runpod_secret()
    write(repo, "private/key.txt", f'RUNPOD_API_KEY = "{secret}"\n')
    git(repo, "add", ".gitignore")
    git(repo, "add", "-f", "private/key.txt")

    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "runpod-api-key" in result.stderr
    assert secret not in result.stderr


def test_text_size_boundary_is_exact(repo):
    write(repo, "boundary.txt", b"a" * ONE_MIB)
    stage(repo, "boundary.txt")
    assert run_scan(repo, "--staged").returncode == 0

    write(repo, "boundary.txt", b"a" * (ONE_MIB + 1))
    stage(repo, "boundary.txt")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[oversize]" in result.stderr


def test_binary_is_blocked_outside_proof_manifest(repo):
    write(repo, "operations/page.png", b"\x89PNG\r\n\x1a\npayload")
    stage(repo, "operations/page.png")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[binary]" in result.stderr


def test_hash_bound_public_fixture_is_allowed(repo):
    data = b"\x89PNG\r\n\x1a\npublic fixture"
    path = "proof/fixtures/page.png"
    write(repo, path, data)
    write(repo, "proof/fixtures.toml", fixture_manifest(path, data))
    stage(repo, "proof/fixtures.toml", path)
    assert run_scan(repo, "--staged").returncode == 0


@pytest.mark.parametrize(
    ("digest", "media_type", "payload"),
    [
        ("0" * 64, "image/png", b"\x89PNG\r\n\x1a\npublic fixture"),
        (None, "image/png", b"PK\x03\x04renamed archive"),
    ],
)
def test_fixture_hash_or_magic_mismatch_is_blocked(repo, digest, media_type, payload):
    path = "proof/fixtures/page.png"
    write(repo, path, payload)
    write(
        repo,
        "proof/fixtures.toml",
        fixture_manifest(path, payload, digest=digest, media_type=media_type),
    )
    stage(repo, "proof/fixtures.toml", path)
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[fixture]" in result.stderr


def test_add_then_delete_secret_remains_blocked_in_history(repo):
    secret = runpod_secret()
    write(repo, "temporary.py", f'RUNPOD_API_KEY = "{secret}"\n')
    stage(repo, "temporary.py")
    commit(repo, "add secret")
    git(repo, "rm", "temporary.py")
    commit(repo, "delete secret")

    result = run_scan(repo, "--history", "HEAD")
    assert result.returncode == 1
    assert "runpod-api-key" in result.stderr
    assert secret not in result.stderr


def test_unusual_safe_paths_are_parsed_without_loss(repo):
    paths = ("space name.txt", "accent-é.txt", "-leading.txt")
    for path in paths:
        write(repo, path, "safe\n")
    stage(repo, *paths)
    assert run_scan(repo, "--staged").returncode == 0


def test_sensitive_filename_and_lfs_pointer_are_blocked(repo):
    write(repo, ".env", "RUNPOD_API_KEY=YOUR_API_KEY\n")
    write(
        repo,
        "model.bin",
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 1\n",
    )
    stage(repo, ".env", "model.bin")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[sensitive-filename]" in result.stderr
    assert "[git-lfs]" in result.stderr


def test_history_scan_refuses_an_incomplete_clone(repo, tmp_path):
    write(repo, "first.txt", "first\n")
    stage(repo, "first.txt")
    commit(repo, "first")
    write(repo, "second.txt", "second\n")
    stage(repo, "second.txt")
    commit(repo, "second")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)],
        check=True,
        timeout=10,
    )
    result = run_scan(shallow, "--history", "HEAD")
    assert result.returncode == 2
    assert "complete clone" in result.stderr
