"""The ScanTailor pin refusals disown the tree; here they are measured against it.

`operations/operator/test_scantailor.py` proves each refusal is named and reaches the
console. This module asks the other half of the question the message raises: the two
pin failures tell the operator that nothing was written, and that sentence is the one
they act on.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from operations.operator import scantailor_worker

PROJECT = b"""<project version="4" outputDirectory="out" layoutDirection="LTR">
<directories><directory id="1" path="masters"/></directories>
<files><file id="2" dirId="1" name="spread.tif"/></files>
<images><image id="3" subPages="2" fileId="2" fileImage="0"><size width="1200" height="800"/><dpi horizontal="300" vertical="300"/></image></images>
<pages><page id="4" imageId="3" subPage="left"/><page id="5" imageId="3" subPage="right"/></pages><file-name-disambiguation/><filters><page-split defaultLayoutType="two-pages"><image id="3" layoutType="two-pages"><params mode="manual"><pages type="two-pages"><outline><point x="0" y="0"/><point x="1200" y="0"/><point x="1200" y="800"/><point x="0" y="800"/><point x="0" y="0"/></outline><cutter1><p1 x="600" y="0"/><p2 x="600" y="800"/></cutter1></pages><dependencies><rotation degrees="0"/><size width="1200" height="800"/><layoutType>two-pages</layoutType></dependencies></params></image></page-split></filters>
</project>"""


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Every byte under `root`, so a refusal's own claim can be checked.

    Both pin failures tell the operator that nothing was written. That is a statement
    about this directory, and until it is compared against the directory it is a
    statement the suite takes on trust -- exactly the shape GOVERNANCE 10 refuses,
    since a commit that published the geometry document and then noticed the pin had
    moved would still print it.
    """
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stray_writes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """The paths a refusal moved, named -- a count would not say which file to look at."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )


def _invoke_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, request: dict
) -> tuple[int, dict]:
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    code = scantailor_worker.main()
    return code, json.loads(capsys.readouterr().out)


def _replaced_output_folder(_project: Path, output: Path) -> None:
    output.rename(output.with_name("original-geometry"))
    output.mkdir()


def _edited_project(project: Path, _output: Path) -> None:
    project.write_bytes(PROJECT.replace(b'x="600"', b'x="601"'))


@pytest.mark.parametrize(
    ("disturb", "named"),
    (
        pytest.param(_replaced_output_folder, "output folder changed", id="output-replaced"),
        pytest.param(_edited_project, "project changed after the preview", id="project-edited"),
    ),
)
def test_a_commit_refused_on_its_pin_wrote_nothing_it_disowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, disturb, named
) -> None:
    """The two pin refusals' "nothing was written", measured against the tree.

    The pin is the whole of the shown-before-written promise: it exists so that a
    folder or a project swapped between the two launches is refused rather than
    committed. The refusal says nothing was written, and the operator's next move is
    to preview again -- which lands a document under a digest-named path. A commit
    that published before noticing the pin had moved would leave a geometry document
    the operator never saw approved, in a folder they never approved, and the message
    would still print.

    The comparison covers the whole tree rather than the output folder alone, since
    the project file is the operator's own work and a commit has no business touching
    it. It is over content rather than names, because a document rewritten under a
    name that already exists moves no name. The assertion names the paths that moved,
    because "one file changed" does not say which.
    """
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    output = tmp_path / "geometry"
    output.mkdir()
    preview_request = {
        "operation": "preview",
        "project": str(project),
        "output_dir": str(output),
        "expected_project_sha256": None,
        "expected_output_device": None,
        "expected_output_inode": None,
    }
    code, preview = _invoke_main(monkeypatch, capsys, preview_request)
    assert code == 0 and preview["status"] == "preview"
    commit_request = {
        **preview_request,
        "operation": "commit",
        "expected_project_sha256": preview["summary"]["project_sha256"],
        "expected_output_device": preview["output_identity"]["device"],
        "expected_output_inode": preview["output_identity"]["inode"],
    }
    disturb(project, output)
    before = _tree_snapshot(tmp_path)

    code, response = _invoke_main(monkeypatch, capsys, commit_request)

    assert code == 2 and response["status"] == "refusal"
    assert named in response["reason"]
    assert "nothing was written" in response["reason"]
    assert _stray_writes(before, _tree_snapshot(tmp_path)) == [], (
        "the refusal wrote to the tree it disowned"
    )
