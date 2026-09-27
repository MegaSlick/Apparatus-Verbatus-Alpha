"""F004: a top-level flag typed after the verb gets a fix, not a bare 'unrecognized'."""

from __future__ import annotations

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


def test_clear_leftovers_lists_then_removes_only_publication_leftovers(tmp_path, capsys):
    root = tmp_path / "run"
    (root / "pages").mkdir(parents=True)
    leftovers = [root / "pages" / ".IMG.tif.tmp-a1b2", root / ".delivery.publishing-x9"]
    leftovers[0].write_bytes(b"partial")
    (leftovers[1] / "inner").mkdir(parents=True)
    kept = [
        root / "pages" / "IMG.tmp-1.tif",
        root / "pages" / ".hidden",
        root / ".tmp-",
        root / ".publishing-x",
        root / "run.json",
    ]
    for path in kept:
        path.write_bytes(b"real")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".x.tmp-1").write_bytes(b"not under the root")
    (root / ".linked.tmp-1").symlink_to(outside / ".x.tmp-1")
    (root / "mount").symlink_to(outside)
    common = ["--workspace", str(tmp_path), "--state-dir", str(tmp_path / "state")]

    assert cli.main([*common, "clear-leftovers", "--root", str(root)]) == 0
    assert all(path.exists() for path in leftovers)
    assert "2 leftover(s)" in capsys.readouterr().out

    assert cli.main([*common, "clear-leftovers", "--root", str(root), "--apply"]) == 0
    assert not any(path.exists() for path in leftovers)
    assert all(path.read_bytes() == b"real" for path in kept)
    assert (outside / ".x.tmp-1").exists() and (root / ".linked.tmp-1").is_symlink()


def test_clear_leftovers_refuses_a_symlinked_root(tmp_path, capsys):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / ".a.tmp-1").write_bytes(b"x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    arguments = ["--workspace", str(tmp_path), "--state-dir", str(tmp_path / "state")]

    assert (
        cli.main([*arguments, "clear-leftovers", "--root", str(tmp_path / "link"), "--apply"]) == 2
    )
    assert (tmp_path / "real" / ".a.tmp-1").exists()
