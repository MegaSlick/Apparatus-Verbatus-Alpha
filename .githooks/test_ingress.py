"""Integration tests for the staged/history repository ingress check."""

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent
SCANNER = HOOKS / "check_ingress.py"
ONE_MIB = 1_048_576
pytestmark = pytest.mark.full


def scanner_module():
    """Import the scanner by path; `.githooks` is not an importable package."""
    spec = importlib.util.spec_from_file_location("check_ingress", SCANNER)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the scanner's dataclasses resolve their
    # string annotations through sys.modules under `from __future__ import
    # annotations`, and fail with an unhelpful AttributeError without it.
    sys.modules["check_ingress"] = module
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 10 + "TEST" + "b" * 16,
        "rpa_" + "a" * 24,
        "rpa_" + "a" * 8 + "_TEST_" + "b" * 8,
    ],
)
def test_exact_provider_tokens_are_not_exempted_by_coincidence_or_low_entropy(repo, secret):
    write(repo, "settings.txt", f"value = {secret}\n")
    stage(repo, "settings.txt")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert secret not in result.stderr


@pytest.mark.parametrize("secret", ["a" * 24, "TEST_PLACEHOLDER_VALUE_1234"])
def test_generic_credential_assignment_has_no_value_based_exemptions(repo, secret):
    write(repo, "settings.txt", f'api_key = "{secret}"\n')
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


def test_explicit_file_mode_scans_ignored_reports_and_fails_on_missing_input(repo):
    write(repo, ".gitignore", "workbench/raw/\n")
    safe_report = write(repo, "workbench/raw/safe.log", "no findings\n")
    secret = runpod_secret()
    report = write(repo, "workbench/raw/reviewer.log", f"pasted {secret}\n")
    assert git(repo, "check-ignore", str(report.relative_to(repo))).returncode == 0
    assert run_scan(repo, "--file", str(safe_report)).returncode == 0

    blocked = run_scan(repo, "--file", str(safe_report), "--file", str(report))
    assert blocked.returncode == 1
    assert secret not in blocked.stderr

    missing = run_scan(repo, "--file", str(repo / "workbench/raw/missing.log"))
    assert missing.returncode == 2
    assert "could not run" in missing.stderr


def test_standard_input_file_mode_scans_before_a_report_is_persisted(repo):
    secret = runpod_secret()
    blocked = subprocess.run(
        [sys.executable, str(SCANNER), "--stdin-file"],
        cwd=repo,
        input=f"review pasted {secret}\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    clean = subprocess.run(
        [sys.executable, str(SCANNER), "--stdin-file"],
        cwd=repo,
        input="review found no credentials\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert blocked.returncode == 1
    assert "[runpod-api-key]" in blocked.stderr
    assert secret not in blocked.stderr
    assert clean.returncode == 0


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


def test_forced_add_of_safe_text_from_private_area_is_still_blocked(repo):
    write(repo, ".gitignore", "private/*\n!private/README.md\n")
    write(repo, "private/local-note.txt", "ordinary local material\n")
    stage(repo, ".gitignore")
    git(repo, "add", "-f", "private/local-note.txt")

    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[private-path]" in result.stderr


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


def test_secret_in_commit_message_is_blocked_locally_and_in_history(repo, tmp_path):
    secret = runpod_secret()
    message = tmp_path / "message.txt"
    message.write_text(f"do not keep {secret}\n", encoding="utf-8")
    local = run_scan(repo, "--message-file", str(message))
    assert local.returncode == 1
    assert secret not in local.stderr

    write(repo, "safe.txt", "safe\n")
    stage(repo, "safe.txt")
    commit(repo, f"do not keep {secret}")
    history = run_scan(repo, "--history", "HEAD")
    assert history.returncode == 1
    assert "<commit-message>" in history.stderr
    assert secret not in history.stderr


def test_annotated_tag_message_is_scanned_separately_from_commit_history(repo):
    write(repo, "safe.txt", "safe\n")
    stage(repo, "safe.txt")
    commit(repo, "safe")
    secret = runpod_secret()
    git(repo, "tag", "-a", "unsafe", "-m", f"release {secret}")

    # Commit history alone cannot see the annotated tag object.
    assert run_scan(repo, "--history", "HEAD").returncode == 0
    tagged = run_scan(repo, "--ref-object", "refs/tags/unsafe")
    assert tagged.returncode == 1
    assert "<annotated-tag>" in tagged.stderr
    assert secret not in tagged.stderr


def test_audit_field_protocol_fails_closed_and_scans_both_fields(repo):
    secret = runpod_secret().encode()
    malformed = subprocess.run(
        [sys.executable, str(SCANNER), "--audit-fields"],
        cwd=repo,
        input=b"one field only",
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert malformed.returncode == 2

    blocked = subprocess.run(
        [sys.executable, str(SCANNER), "--audit-fields"],
        cwd=repo,
        input=b"Claude Opus 5\0found " + secret + b"\0",
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert blocked.returncode == 1
    assert secret not in blocked.stderr


def test_audit_receipt_validation_rejects_partial_records(repo):
    receipt = write(
        repo,
        "receipt",
        "commit:  " + "a" * 40 + "\n"
        "branch:  work/example\n"
        "audited: unknown (1 commit(s))\n"
        "\nauditor: Claude Opus 5\n",
    )
    result = run_scan(repo, "--audit-receipt", str(receipt))
    assert result.returncode == 1
    assert "[receipt-shape]" in result.stderr


def test_ref_field_protocol_scans_names_without_echoing_them(repo):
    secret = runpod_secret().encode()
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--ref-fields"],
        cwd=repo,
        input=b"refs/heads/work/" + secret + b"\0refs/heads/work/safe\0",
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 1
    assert b"<git-ref:" in result.stderr
    assert secret not in result.stderr


def test_control_character_path_is_blocked_in_staged_and_history(repo):
    path = "evil\nREADME.md"
    write(repo, path, "safe text\n")
    stage(repo, path)
    staged = run_scan(repo, "--staged")
    assert staged.returncode == 1
    assert "[control-path]" in staged.stderr
    assert "<repository-path:" in staged.stderr
    assert path not in staged.stderr

    commit(repo, "control path")
    history = run_scan(repo, "--history", "HEAD")
    assert history.returncode == 1
    assert "[control-path]" in history.stderr


def test_credential_shaped_repository_path_is_blocked_without_echoing_it(repo):
    secret = runpod_secret()
    path = f"proof/{secret}.txt"
    write(repo, path, "safe text\n")
    stage(repo, path)

    staged = run_scan(repo, "--staged")
    assert staged.returncode == 1
    assert "<repository-path:" in staged.stderr
    assert secret not in staged.stderr

    commit(repo, "credential-shaped path")
    history = run_scan(repo, "--history", "HEAD")
    assert history.returncode == 1
    assert secret not in history.stderr


@pytest.mark.parametrize(
    ("suffix", "rule"),
    [
        (".env", "sensitive-filename"),
        ("payload.bin", "binary"),
        ("large.txt", "oversize"),
    ],
)
def test_other_path_findings_also_redact_credential_shaped_names(repo, suffix, rule):
    secret = runpod_secret()
    path = f"{secret}/{suffix}"
    payloads = {
        "sensitive-filename": "safe placeholder\n",
        "binary": b"\0binary",
        "oversize": b"x" * (ONE_MIB + 1),
    }
    payload = payloads[rule]
    write(repo, path, payload)
    stage(repo, path)

    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert f"[{rule}]" in result.stderr
    assert secret not in result.stderr


def test_read_errors_redact_credential_shaped_file_arguments(repo):
    secret = runpod_secret()
    result = run_scan(repo, "--file", str(repo / secret / "missing.log"))
    assert result.returncode == 2
    assert "<redacted-error:" in result.stderr
    assert secret not in result.stderr


def test_symlink_is_blocked_in_staged_and_history(repo):
    target = repo / "external-link"
    target.symlink_to("/etc/passwd")
    stage(repo, "external-link")
    staged = run_scan(repo, "--staged")
    assert staged.returncode == 1
    assert "[symlink]" in staged.stderr

    commit(repo, "symlink")
    history = run_scan(repo, "--history", "HEAD")
    assert history.returncode == 1
    assert "[symlink]" in history.stderr


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


def test_ntfy_topic_is_a_secret_in_both_leak_shapes(repo):
    # The topic name IS the whole credential — read and forge. The old
    # repository leaked it as pasted command lines, so both the assignment
    # form and the URL form are refused. Built discontinuously so this test
    # file does not itself contain a scannable topic.
    topic = "verbatus-" + "a7b9c2d4e6"
    assignment = "NTFY_" + 'TOPIC = "' + topic + '"\n'
    write(repo, "notes.py", assignment)
    stage(repo, "notes.py")
    assert run_scan(repo, "--staged").returncode == 1

    url_line = "# see https://ntfy" + ".sh/" + topic + "\n"
    write(repo, "notes.py", url_line)
    stage(repo, "notes.py")
    assert run_scan(repo, "--staged").returncode == 1


def test_ntfy_docs_and_explicit_angle_bracket_placeholder_stay_committable(repo):
    # The angle brackets keep the example visibly non-working and outside the
    # exact assignment grammar. Exact topic-shaped values receive no exemption.
    body = (
        "# docs: https://docs.ntfy" + ".sh/publish/ and https://ntfy" + ".sh/docs\n"
        "NTFY_" + 'TOPIC = "<EXAMPLE-TOPIC>"\n'
    )
    write(repo, "notes.py", body)
    stage(repo, "notes.py")
    assert run_scan(repo, "--staged").returncode == 0


def test_ntfy_exact_assignment_does_not_exempt_delimited_placeholder_words(repo):
    topic = "verbatus_" + "TEST_" + "topicvalue"
    assignment = "NTFY_" + f'TOPIC = "{topic}"\n'
    # Even the path containing the two fingerprint-bound historical fixtures
    # receives no blanket exemption for placeholder-looking topics.
    path = "operations/notify/test_notify.py"
    write(repo, path, assignment)
    stage(repo, path)
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert topic not in result.stderr


@pytest.mark.parametrize("mode", ["--file", "--message-file", "--audit-receipt"])
def test_a_fifo_at_a_scanned_path_fails_closed_instead_of_hanging(repo, mode):
    # Opening a FIFO with no writer blocks forever. This scanner runs from
    # pre-commit and from CI, so a hang is a session or a build that never
    # finishes and never says why. The subprocess timeout below is the test:
    # before the fix it expires, which is the defect.
    fifo = repo / "pipe"
    os.mkfifo(fifo)
    result = run_scan(repo, mode, str(fifo))
    assert result.returncode == 2
    assert "could not run" in result.stderr
    assert "not a regular file" in result.stderr


def test_a_symlink_to_a_fifo_is_judged_by_the_open_descriptor(repo):
    # The type must be decided from the descriptor actually opened, not from
    # the name; that is the same check that closes the swap race (L23).
    fifo = repo / "pipe"
    os.mkfifo(fifo)
    link = repo / "link"
    link.symlink_to(fifo)
    result = run_scan(repo, "--file", str(link))
    assert result.returncode == 2
    assert "not a regular file" in result.stderr


def test_a_character_device_is_refused_even_though_it_reads_cleanly(repo):
    # /dev/null returns EOF at once, so "the read succeeded" is not evidence
    # that a regular file was scanned. Fail closed on anything but S_ISREG.
    result = run_scan(repo, "--file", "/dev/null")
    assert result.returncode == 2
    assert "not a regular file" in result.stderr


def test_worktree_scan_reports_unscannable_entries_instead_of_skipping_them(repo):
    # GOVERNANCE 2: a partial result is visibly partial. A tracked file
    # replaced on disk by a FIFO used to fall through every branch of the
    # worktree walk, and the run still reported passed. Git omits untracked
    # non-regular files from `ls-files --others`, so a tracked path is the
    # only way one reaches the scanner at all.
    write(repo, "payload.txt", "safe\n")
    stage(repo, "payload.txt")
    commit(repo, "add payload")
    (repo / "payload.txt").unlink()
    os.mkfifo(repo / "payload.txt")
    result = run_scan(repo, "--worktree")
    assert result.returncode == 1
    assert "[unscannable]" in result.stderr
    assert "fifo" in result.stderr


# One realistic sample per declared pattern. Every value is built
# discontinuously so this test file never contains a scannable credential:
# the scanner reads its own repository and must pass on this file.
PROVIDER_SAMPLES = (
    ("private-key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("runpod-api-key", "rpa_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"),
    ("runpod-s3-secret", "rps_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r"),
    ("aws-access-key", "AKIA" + "Q2W3E4R5T6Y7U8I9"),
    ("aws-access-key", "ASIA" + "Q2W3E4R5T6Y7U8I9"),
    ("github-token", "ghp_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r1S3t5"),
    ("github-token", "github_pat_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r1S3t5U7v9W2x4"),
    ("gitlab-token", "glpat-" + "A7b9C2d4E6f8G1h3J5k7"),
    ("google-api-key", "AIza" + "SyA7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r1S"),
    ("google-oauth-secret", "GOCSPX-" + "A7b9C2d4E6f8G1h3J5k7L9m2"),
    ("openai-api-key", "sk-" + "proj-A7b9C2d4E6f8G1h3J5k7L9m2"),
    ("anthropic-api-key", "sk-ant-" + "api03-A7b9C2d4E6f8G1h3J5k7L9m2"),
    ("huggingface-token", "hf_" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8"),
    ("slack-token", "xox" + "b-2468013579-1357924680-A7b9C2d4E6f8G1h3J5k7L9m2"),
    ("slack-token", "xap" + "p-1-A0B1C2D3E4F-2468013579-a7b9c2d4e6f8g1h3j5k7l9m2"),
    (
        "slack-webhook",
        "https://hooks.slack" + ".com/services/T02468AC/B13579BD/A7b9C2d4E6f8G1h3J5k7L9m2",
    ),
    ("stripe-live-key", "sk_live_" + "A7b9C2d4E6f8G1h3J5k7L9m2"),
    ("stripe-restricted-key", "rk_live_" + "A7b9C2d4E6f8G1h3J5k7L9m2"),
    ("stripe-webhook-secret", "whsec_" + "A7b9C2d4E6f8G1h3J5k7L9m2"),
    (
        "pypi-token",
        "pypi-" + "AgEIcHlwaS5vcmc" + "A7b9C2d4E6f8G1h3J5k7L9m2N4p6Q8r1S3t5U7v9W2x4Y6z8B1c3D5e7F9",
    ),
    (
        "json-web-token",
        "eyJ"
        + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        + "."
        + "eyJ"
        + "zdWIiOiIxMjM0NTY3ODkwIn0"
        + "."
        + "A7b9C2d4E6f8G1h3J5k7L9m2",
    ),
    ("credential-url", "https://svc-user:" + "T0pS3cr3tValue@example.invalid/path"),
    ("credential-uri", "postgresql://svc-user:" + "T0pS3cr3tValue@db.example.invalid/app"),
    ("credential-uri", "mongodb+srv://svc-user:" + "T0pS3cr3tValue@db.example.invalid/app"),
    ("ntfy-topic", "NTFY_" + 'TOPIC = "verbatus-' + "a7b9c2d4e6" + '"'),
    ("ntfy-topic", "https://ntfy" + ".sh/verbatus-" + "a7b9c2d4e6"),
)


@pytest.mark.parametrize(("rule", "secret"), PROVIDER_SAMPLES)
def test_each_declared_credential_shape_is_blocked_by_its_own_rule(repo, rule, secret):
    write(repo, "notes.txt", "pasted into a note: " + secret + "\n")
    stage(repo, "notes.txt")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert f"[{rule}]" in result.stderr
    assert secret not in result.stderr


def test_every_declared_pattern_has_a_regression_sample():
    # Without this, deleting or corrupting a rule stays green: L39. It is the
    # compiled pattern that must be covered, not merely the rule name, because
    # several names carry more than one pattern.
    module = scanner_module()
    samples = [secret.encode() for _, secret in PROVIDER_SAMPLES]
    uncovered = [
        rule
        for rule, pattern in module.SECRET_PATTERNS
        if not any(pattern.search(sample) for sample in samples)
    ]
    assert not uncovered, f"declared patterns with no regression sample: {uncovered}"

    declared = {rule for rule, _ in module.SECRET_PATTERNS}
    claimed = {rule for rule, _ in PROVIDER_SAMPLES}
    assert claimed - declared == set(), "a sample names a rule the scanner no longer declares"


@pytest.mark.parametrize(
    "key",
    [
        "aws_secret_access_key",
        "aws_session_token",
        "AWS_SECRET_ACCESS_KEY",
        "google_client_secret",
        "db-password",
        "runpod.api_key",
    ],
)
def test_vendor_prefixed_credential_names_are_not_defeated_by_the_word_boundary(repo, key):
    # L20: the generic assignment rule anchored on \b, which a vendor prefix
    # joined by _ - or . defeats outright -- the commonest real spelling.
    secret = generic_secret()
    write(repo, "settings.txt", f"{key} = " + f'"{secret}"\n')
    stage(repo, "settings.txt")
    result = run_scan(repo, "--staged")
    assert result.returncode == 1
    assert "[literal-credential]" in result.stderr
    assert secret not in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        "token_expiry_days = 30\n",
        "# the access token is issued by the provider, then discarded\n",
        "see https://ntfy" + ".sh/docs for the publish API\n",
        "digest = " + '"' + "a" * 64 + '"' + "  # sha256 of a fixture, not a secret\n",
    ],
)
def test_ordinary_prose_and_short_values_are_not_credential_findings(repo, line):
    # The loosened key grammar must not start refusing ordinary text; an
    # over-refusing scanner is the mechanism by which the guard gets bypassed.
    write(repo, "notes.md", line)
    stage(repo, "notes.md")
    result = run_scan(repo, "--staged")
    assert result.returncode == 0, result.stderr


def test_findings_past_the_hundredth_can_be_retrieved(repo):
    # L25. The report truncated at 100 and printed a remainder count with no
    # way to see the rest, which is a dead end for whoever has 150 of them.
    secrets = ["rpa_" + f"{index:024d}" for index in range(150)]
    write(repo, "dump.txt", "".join(f"line {index}: {s}\n" for index, s in enumerate(secrets)))
    stage(repo, "dump.txt")

    truncated = run_scan(repo, "--staged")
    assert truncated.returncode == 1
    assert truncated.stderr.count("[runpod-api-key]") == 100
    assert "50 more" in truncated.stderr
    assert "--max-findings" in truncated.stderr

    full = run_scan(repo, "--staged", "--max-findings", "0")
    assert full.returncode == 1
    assert full.stderr.count("[runpod-api-key]") == 150
    assert "more issue" not in full.stderr
    for secret in secrets:
        assert secret not in full.stderr

    refused = run_scan(repo, "--staged", "--max-findings", "-1")
    assert refused.returncode == 2


def test_worktree_does_not_read_a_file_it_will_refuse_on_size_alone(repo, monkeypatch):
    # L24. The worktree walk read every file whole and consulted the size
    # limit afterwards, so the one file too big to accept was exactly the file
    # pulled into memory: a corpus commit crashes the scanner instead of
    # getting the clean oversize diagnosis it was built to produce. The
    # threshold is lowered here so the decision is observable without a
    # multi-gigabyte file; `data is None` is the proof that the bytes were
    # never read, and entry_size answering at all proves the size came from
    # the stat rather than from Git.
    module = scanner_module()
    monkeypatch.setattr(module, "FIXTURE_MAX_BYTES", 128)
    write(repo, "small.txt", "safe\n")
    write(repo, "big.txt", "b" * 300)
    stage(repo, "small.txt", "big.txt")
    monkeypatch.chdir(repo)

    entries = module.working_tree()
    assert entries["small.txt"].data == b"safe\n"
    big = entries["big.txt"]
    assert big.data is None, "an oversize file was read into memory before its size was consulted"
    assert module.entry_size(big) == 300


def test_an_oversize_worktree_file_still_gets_its_oversize_diagnosis(repo):
    # The saved read must not cost the diagnosis. Sparse, so it costs no real
    # disk and no real bytes.
    with open(repo / "corpus.bin", "wb") as handle:
        handle.truncate(ONE_MIB + 1)
    stage(repo, "corpus.bin")
    result = run_scan(repo, "--worktree")
    assert result.returncode == 1
    assert "[oversize]" in result.stderr


def test_blob_cache_uses_a_byte_budget_instead_of_the_legacy_64_entry_limit(monkeypatch):
    # The old 64-entry LRU thrashed on 65 blobs even when their combined
    # payload was only a few hundred bytes. A second pass over this working
    # set must be served wholly from the aggregate-byte cache.
    module = scanner_module()
    payloads = {f"{index:040x}": f"{index:08d}".encode("ascii") for index in range(65)}
    reads = []

    def fake_git(*args):
        assert args[:2] == ("cat-file", "blob")
        oid = args[2]
        reads.append(oid)
        return payloads[oid]

    cache = module.BlobDataCache(sum(map(len, payloads.values())))
    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setattr(module, "_BLOB_DATA_CACHE", cache)

    for _ in range(2):
        for oid, expected in payloads.items():
            assert module.blob_data(oid) == expected

    assert reads == list(payloads), "the second pass re-read blobs that fit the byte budget"
    assert cache.entry_count == 65
    assert cache.bytes_used == sum(map(len, payloads.values()))


def test_blob_cache_evicts_by_bytes_and_never_retains_an_oversize_blob(monkeypatch):
    module = scanner_module()
    payloads = {
        "a" * 40: b"a" * 6,
        "b" * 40: b"b" * 4,
        "c" * 40: b"c" * 7,
        "d" * 40: b"d" * 11,
    }
    reads = []

    def fake_git(*args):
        assert args[:2] == ("cat-file", "blob")
        oid = args[2]
        reads.append(oid)
        return payloads[oid]

    cache = module.BlobDataCache(10)
    monkeypatch.setattr(module, "git", fake_git)
    monkeypatch.setattr(module, "_BLOB_DATA_CACHE", cache)

    assert module.blob_data("a" * 40) == payloads["a" * 40]
    assert module.blob_data("a" * 40) == payloads["a" * 40]
    assert cache.bytes_used == 6
    assert reads.count("a" * 40) == 1, "a repeated cached OID was fetched or counted twice"

    assert module.blob_data("b" * 40) == payloads["b" * 40]
    assert cache.bytes_used == 10
    assert module.blob_data("c" * 40) == payloads["c" * 40]
    assert cache.bytes_used == 7
    assert cache.entry_count == 1

    # The cache stays unchanged while an individually oversize blob is
    # returned correctly on each request.
    assert module.blob_data("d" * 40) == payloads["d" * 40]
    assert module.blob_data("d" * 40) == payloads["d" * 40]
    assert reads.count("d" * 40) == 2
    assert cache.bytes_used == 7
    assert cache.entry_count == 1


def test_the_declared_secret_fixture_set_stays_empty():
    # L40. The source calls empty "the intended resting state" and says an
    # exemption nobody can audit inside a credential scanner is worse than no
    # exemption at all. Nothing enforced that, so a triple could be added to a
    # credential scanner with no test objecting. Adding one now has to break
    # this test, which is the moment someone has to justify it in a review.
    module = scanner_module()
    assert module.DECLARED_SECRET_FIXTURES == frozenset(), (
        "a credential exemption was added; it must arrive with its reason, and "
        "removing it must fail the scan"
    )


def test_worktree_scan_still_ignores_a_deleted_tracked_file(repo):
    # A tracked path missing from disk is a working-tree deletion, not an
    # unscannable payload; the fail-closed read must not turn it into one.
    write(repo, "gone.txt", "safe\n")
    stage(repo, "gone.txt")
    commit(repo, "add file")
    (repo / "gone.txt").unlink()
    assert run_scan(repo, "--worktree").returncode == 0
