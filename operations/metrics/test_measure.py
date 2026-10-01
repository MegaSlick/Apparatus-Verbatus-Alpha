import subprocess

from operations.metrics import measure

SOURCE = '''"""A module docstring
over two lines."""

# a comment line
import os  # noqa: F401


def helper():
    """One line."""
    return os.sep  # type: ignore
'''

TEST = """# pragma: no cover
def test_nothing():
    assert "# noqa" in "a string, not a comment"
"""


def _stage(root, files):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)


def test_counts_lines_comments_docstrings_and_markers_per_kind(tmp_path, capsys):
    _stage(
        tmp_path,
        {
            "pkg/module.py": SOURCE,
            "pkg/test_module.py": TEST,
            "private/secret.py": SOURCE,
            "gold/answer.py": SOURCE,
            "notes.txt": "# not python\n",
        },
    )
    (tmp_path / "untracked.py").write_text("# ignored\n", encoding="utf-8")

    measure.main(tmp_path)

    header, rule, src, test = capsys.readouterr().out.splitlines()
    assert header == (
        "| | files | lines | comments | docstrings | # noqa | # type: ignore | # pragma: no cover |"
    )
    assert rule == "|---" * 8 + "|"
    assert src == "| src | 1 | 10 | 1 | 3 | 1 | 1 | 0 |"
    assert test == "| test | 1 | 3 | 1 | 0 | 0 | 0 | 1 |"
