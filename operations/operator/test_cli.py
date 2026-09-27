"""F004: a top-level flag typed after the verb gets a fix, not a bare 'unrecognized'."""

from __future__ import annotations

import os

import pytest

from operations.operator.errors import ErrorCode, OperatorError

from . import cli


@pytest.mark.parametrize(
    "message",
    (
        "argument verb: invalid choice: 'launch'",
        "the following arguments are required: --run-root",
    ),
)
def test_a_non_unrecognized_argparse_message_is_untouched(message):
    assert cli._annotate_unrecognized(message) == message


def test_an_unrecognized_flag_that_is_not_top_level_only_is_untouched():
    message = "unrecognized arguments: --typo-flag value"
    assert cli._annotate_unrecognized(message) == message


@pytest.mark.parametrize("flag", ("--workspace", "--state-dir", "--notify"))
def test_an_unrecognized_top_level_flag_names_the_fix(flag):
    message = f"unrecognized arguments: {flag} value"
    annotated = cli._annotate_unrecognized(message)
    assert annotated.startswith(message)
    assert flag in annotated
    assert "before the word" in annotated
    assert " is accepted only" in annotated


def test_two_unrecognized_top_level_flags_are_named_together_and_pluralized():
    message = "unrecognized arguments: --state-dir /records --notify"
    annotated = cli._annotate_unrecognized(message)
    assert "--state-dir, --notify are accepted only" in annotated


def test_a_flag_value_form_is_still_matched():
    message = "unrecognized arguments: --state-dir=/records"
    annotated = cli._annotate_unrecognized(message)
    assert "--state-dir is accepted only" in annotated


def test_a_state_dir_typed_after_the_verb_is_refused_with_the_fix_named():
    parser = cli.build_parser()

    with pytest.raises(OperatorError) as excinfo:
        parser.parse_args(["review", "--run-root", "/runs", "--run-id", "r", "--state-dir", "/x"])

    assert excinfo.value.code is ErrorCode.INVALID_COMMAND
    assert "--state-dir is accepted only" in excinfo.value.render()
    assert "before the word" in excinfo.value.render()


def test_a_genuine_typo_after_the_verb_still_reads_as_a_plain_refusal():
    parser = cli.build_parser()

    with pytest.raises(OperatorError) as excinfo:
        parser.parse_args(["review", "--run-root", "/runs", "--run-id", "r", "--typo-flag", "x"])

    assert excinfo.value.code is ErrorCode.INVALID_COMMAND
    assert "is accepted only" not in excinfo.value.render()


def _clear(tmp_path, root, *extra):
    common = ["--workspace", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    return cli.main([*common, "clear-leftovers", "--root", str(root), *extra])


def _age(*paths):
    for path in paths:
        os.utime(path, (1, 1), follow_symlinks=False)


def test_clear_leftovers_lists_then_removes_only_publication_leftovers(tmp_path, capsys):
    root = tmp_path / "run"
    (root / "pages").mkdir(parents=True)
    temporaries = [root / "pages" / ".IMG.tif.tmp-a1b2c3d4", root / (".run.json.tmp-" + "f" * 32)]
    staging = root / ".delivery.publishing-x9y8z7_6"
    (staging / "inner").mkdir(parents=True)
    fresh = root / ".manifest.json.tmp-abcdefgh"
    for path in (*temporaries, fresh):
        path.write_bytes(b"partial")
    kept_files = [
        root / "pages" / "IMG.tmp-1.tif",
        root / "pages" / ".hidden",
        root / ".tmp-",
        root / ".notes.tmp-short",
        root / "run.json",
    ]
    for path in kept_files:
        path.write_bytes(b"real")
    kept_folders = [root / ".publishing-abcdefgh", root / ".photos.publishing-old"]
    for path in kept_folders:
        path.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".x.tmp-abcdefgh").write_bytes(b"not under the root")
    (root / ".linked.tmp-abcdefgh").symlink_to(outside / ".x.tmp-abcdefgh")
    (root / ".x.publishing-abcdefgh").symlink_to(outside)
    _age(*temporaries, staging, *kept_files, *kept_folders, outside / ".x.tmp-abcdefgh")
    _age(root / ".linked.tmp-abcdefgh", root / ".x.publishing-abcdefgh")

    assert _clear(tmp_path, root) == 0
    assert all(path.exists() for path in (*temporaries, staging))
    assert "3 leftover(s)" in capsys.readouterr().out

    assert _clear(tmp_path, root, "--apply") == 0
    assert not any(path.exists() for path in (*temporaries, staging))
    assert "Left alone, changed within the last hour" in capsys.readouterr().out
    assert fresh.exists()
    assert all(path.read_bytes() == b"real" for path in kept_files)
    assert all(path.is_dir() for path in kept_folders)
    assert (outside / ".x.tmp-abcdefgh").exists()
    assert (root / ".linked.tmp-abcdefgh").is_symlink()
    assert (root / ".x.publishing-abcdefgh").is_symlink()


def test_clear_leftovers_refuses_a_symlinked_root(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / ".a.tmp-abcdefgh").write_bytes(b"x")
    _age(tmp_path / "real" / ".a.tmp-abcdefgh")
    (tmp_path / "link").symlink_to(tmp_path / "real")

    assert _clear(tmp_path, tmp_path / "link", "--apply") == 2
    assert (tmp_path / "real" / ".a.tmp-abcdefgh").exists()


def test_clear_leftovers_refuses_a_folder_it_cannot_read(tmp_path, capsys):
    (tmp_path / "run" / "locked").mkdir(parents=True)
    (tmp_path / "run" / "locked").chmod(0)
    try:
        assert _clear(tmp_path, tmp_path / "run") == 2
    finally:
        (tmp_path / "run" / "locked").chmod(0o700)
    assert "0 leftover(s)" not in capsys.readouterr().out
