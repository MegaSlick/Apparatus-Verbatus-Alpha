"""`verbatus prepare` without pagekit installed: a plain refusal, not an ImportError."""

from __future__ import annotations

import sys

from operations.operator import cli


def test_prepare_without_pagekit_says_how_to_install_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "pagekit", None)  # `import pagekit` now fails
    (tmp_path / "my scans").mkdir()

    code = cli.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "prepare",
            "--scans",
            str(tmp_path / "my scans"),
            "--out",
            str(tmp_path / "out dir"),
        ]
    )
    printed = capsys.readouterr().out

    assert code != 0
    assert "pagekit is not installed" in printed
    assert "pip install -e ~/pagekit" in printed
    assert not (tmp_path / "out dir").exists()
