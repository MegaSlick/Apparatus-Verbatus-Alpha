"""The named desktop handoff records geometry but never makes a choice."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from operations.operator import scantailor, scantailor_worker

PROJECT = b"""<project version="4" outputDirectory="out" layoutDirection="LTR">
<directories><directory id="1" path="masters"/></directories>
<files><file id="2" dirId="1" name="spread.tif"/></files>
<images><image id="3" subPages="2" fileId="2" fileImage="0"><size width="1200" height="800"/><dpi horizontal="300" vertical="300"/></image></images>
<pages><page id="4" imageId="3" subPage="left"/><page id="5" imageId="3" subPage="right"/></pages><file-name-disambiguation/><filters><page-split defaultLayoutType="two-pages"><image id="3" layoutType="two-pages"><params mode="manual"><pages type="two-pages"><outline><point x="0" y="0"/><point x="1200" y="0"/><point x="1200" y="800"/><point x="0" y="800"/><point x="0" y="0"/></outline><cutter1><p1 x="600" y="0"/><p2 x="600" y="800"/></cutter1></pages><dependencies><rotation degrees="0"/><size width="1200" height="800"/><layoutType>two-pages</layoutType></dependencies></params></image></page-split></filters>
</project>"""


def test_real_project_shape_round_trips_geometry_without_reducing_it(tmp_path: Path) -> None:
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    parsed = scantailor_worker.parse(PROJECT, project)
    assert parsed["project_version"] == 4
    assert parsed["geometry"][0]["layout_type"] == "two-pages"
    assert len(parsed["geometry"][0]["outline"]) == 5
    assert parsed["geometry"][0]["outline"][0] == parsed["geometry"][0]["outline"][-1]
    assert parsed["geometry"][0]["cutters"][0]["p1"] == {"x": "600", "y": "0"}
    assert "winner" not in json.dumps(parsed)


@pytest.mark.parametrize("bad", (b"<project", PROJECT.replace(b'<point x="0" y="800"/>', b"")))
def test_malformed_or_truncated_project_refuses_by_name(tmp_path: Path, bad: bytes) -> None:
    with pytest.raises(ValueError, match="ScanTailor project refusal"):
        scantailor_worker.parse(bad, tmp_path / "broken.ScanTailor")


def test_entity_laden_project_refuses_by_name(tmp_path: Path) -> None:
    bomb = b'<?xml version="1.0"?>\n<!DOCTYPE project [<!ENTITY a "x">]>\n' + PROJECT
    with pytest.raises(ValueError, match="DOCTYPE or entity"):
        scantailor_worker.parse(bomb, tmp_path / "bomb.ScanTailor")


def test_utf16_entity_laden_project_refuses_before_xml_parsing(tmp_path: Path) -> None:
    text = PROJECT.decode("utf-8")
    bomb = (
        '<?xml version="1.0" encoding="UTF-16"?>\n<!DOCTYPE project [<!ENTITY a "x">]>\n' + text
    ).encode("utf-16")
    with pytest.raises(ValueError, match="DOCTYPE or entity"):
        scantailor_worker.parse(bomb, tmp_path / "utf16-bomb.ScanTailor")


def test_removed_half_is_retained_as_geometry_without_being_applied(tmp_path: Path) -> None:
    removed = PROJECT.replace(b'fileImage="0"', b'fileImage="0" removed="L"').replace(
        b'<page id="4" imageId="3" subPage="left"/>', b""
    )
    parsed = scantailor_worker.parse(removed, tmp_path / "removed.ScanTailor")
    assert parsed["geometry"][0]["image"]["removed_half"] == "left"


def test_oversized_project_refuses_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scantailor_worker, "_MAX_PROJECT_BYTES", 8)
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    with pytest.raises(ValueError, match="project file is larger than 8 bytes"):
        scantailor_worker._bounded_bytes(project, "project file")


def test_two_geometries_for_one_page_refuses_rather_than_picking(tmp_path: Path) -> None:
    second_variant = (
        b'<image id="3" layoutType="single-uncut"><params mode="manual">'
        b'<pages type="single-uncut"><outline>'
        b'<point x="0" y="0"/><point x="1200" y="0"/><point x="1200" y="800"/><point x="0" y="800"/>'
        b"</outline></pages><dependencies/></params></image>"
    )
    offering_two_variants = PROJECT.replace(b"</page-split>", second_variant + b"</page-split>")
    with pytest.raises(ValueError, match="more than one geometry for the same image"):
        scantailor_worker.parse(offering_two_variants, tmp_path / "two-variants.ScanTailor")


def test_surface_names_the_external_gap_and_imports_only_through_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "scan.ScanTailor"
    output = tmp_path / "geometry"
    project.write_bytes(PROJECT)
    output.mkdir()
    messages: list[str] = []
    monkeypatch.setattr(scantailor, "run_confined", lambda *a, **k: _worker(*a, **k))
    scantailor.import_in_custody(
        project=project, output_dir=output, workspace=tmp_path, printer=messages.append
    )
    assert "separate desktop program" in scantailor.instruction(project)
    document = next(output.glob("scantailor-geometry-*.json"))
    assert json.loads(document.read_text())["geometry"][0]["layout_type"] == "two-pages"
    assert "not a selected or applied result" in messages[-1]


def test_parent_refuses_a_committed_summary_whose_project_digest_does_not_match_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confined child checks its own pin before answering "committed"; the
    parent must not take that answer on faith. A worker that reports success
    for a project other than the one it was pinned to must still be refused
    here, the same way every other field of its response is re-checked rather
    than trusted.
    """
    from subprocess import CompletedProcess

    from operations.operator.errors import ErrorCode, OperatorError

    project = tmp_path / "scan.ScanTailor"
    output = tmp_path / "geometry"
    project.write_bytes(PROJECT)
    output.mkdir()

    def tampered(command, *, writable, cwd, input_text):
        backend, completed = _worker(command, writable=writable, cwd=cwd, input_text=input_text)
        if json.loads(input_text)["operation"] != "commit":
            return backend, completed
        response = json.loads(completed.stdout)
        response["summary"]["project_sha256"] = "f" * 64
        return backend, CompletedProcess(
            completed.args, completed.returncode, json.dumps(response), completed.stderr
        )

    monkeypatch.setattr(scantailor, "run_confined", tampered)
    with pytest.raises(OperatorError) as raised:
        scantailor.import_in_custody(
            project=project, output_dir=output, workspace=tmp_path, printer=lambda _: None
        )
    assert raised.value.code is ErrorCode.SCANTAILOR_UNRESOLVED
    assert "committed a project digest other than the one it was pinned to" in raised.value.render()


def test_instruction_anchors_a_relative_project_to_the_selected_workspace(tmp_path: Path) -> None:
    rendered = scantailor.instruction(Path("projects/scan.ScanTailor"), workspace=tmp_path)
    assert str(tmp_path / "projects" / "scan.ScanTailor") in rendered


def _invoke_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, request: dict
) -> tuple[int, dict]:
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    code = scantailor_worker.main()
    return code, json.loads(capsys.readouterr().out)


def test_commit_lands_through_the_write_boundary_durably_and_as_a_fixed_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    output = tmp_path / "geometry"
    output.mkdir()
    preview_request = {
        "operation": "preview",
        "project": str(project),
        "output_dir": str(output),
        "expected_project_sha256": None,
    }
    code, preview = _invoke_main(monkeypatch, capsys, preview_request)
    assert code == 0 and preview["status"] == "preview"
    commit_request = {
        **preview_request,
        "operation": "commit",
        "expected_project_sha256": preview["summary"]["project_sha256"],
    }
    code, committed = _invoke_main(monkeypatch, capsys, commit_request)
    assert code == 0 and committed["status"] == "committed"
    document = Path(committed["summary"]["document_path"])
    data = document.read_bytes()
    assert scantailor_worker.canonical_bytes(json.loads(data)) + b"\n" == data
    code, recommitted = _invoke_main(monkeypatch, capsys, commit_request)
    assert code == 0 and recommitted["status"] == "committed"
    assert recommitted["summary"] == committed["summary"]


def test_commit_refuses_when_an_existing_document_does_not_match_its_own_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    output = tmp_path / "geometry"
    output.mkdir()
    preview_request = {
        "operation": "preview",
        "project": str(project),
        "output_dir": str(output),
        "expected_project_sha256": None,
    }
    _, preview = _invoke_main(monkeypatch, capsys, preview_request)
    document_path = Path(preview["summary"]["document_path"])
    document_path.write_bytes(b'{"tampered":true}')
    commit_request = {
        **preview_request,
        "operation": "commit",
        "expected_project_sha256": preview["summary"]["project_sha256"],
    }
    code, response = _invoke_main(monkeypatch, capsys, commit_request)
    assert code == 2 and response["status"] == "refusal"
    assert "does not match its digest" in response["reason"]


def test_two_ids_naming_one_physical_page_refuses_rather_than_picking(tmp_path: Path) -> None:
    """Distinct project ids for one file frame must not become competing records.

    Two `<image>` rows on one `fileId`/`fileImage` are two labels for one physical
    page. Each may carry its own outline, so id-only uniqueness cannot enforce the
    prohibition on selecting between geometries.
    """
    twin = (
        b'<image id="9" subPages="2" fileId="2" fileImage="0">'
        b'<size width="1200" height="800"/><dpi horizontal="300" vertical="300"/></image>'
    )
    other_cut = (
        b'<image id="9" layoutType="two-pages"><params mode="manual">'
        b'<pages type="two-pages"><outline>'
        b'<point x="0" y="0"/><point x="1200" y="0"/><point x="1200" y="800"/><point x="0" y="800"/>'
        b'</outline><cutter1><p1 x="333" y="0"/><p2 x="333" y="800"/></cutter1></pages>'
        b"<dependencies/></params></image>"
    )
    two_labels = PROJECT.replace(b"</images>", twin + b"</images>").replace(
        b"</page-split>", other_cut + b"</page-split>"
    )
    with pytest.raises(ValueError, match="more than one geometry for the same physical page"):
        scantailor_worker.parse(two_labels, tmp_path / "twin.ScanTailor")


def _case_variant_project(tmp_path: Path) -> bytes:
    """Two distinct `<file>` rows whose names differ only by case."""
    twin_file = b'<file id="6" dirId="1" name="SPREAD.tif"/></files>'
    twin_image = (
        b'<image id="7" subPages="2" fileId="6" fileImage="0">'
        b'<size width="1200" height="800"/><dpi horizontal="300" vertical="300"/></image></images>'
    )
    twin_split = (
        b'<image id="7" layoutType="single-uncut"><params mode="manual">'
        b'<pages type="single-uncut"><outline>'
        b'<point x="0" y="0"/><point x="1200" y="0"/><point x="1200" y="800"/><point x="0" y="800"/>'
        b'<point x="0" y="0"/></outline></pages><dependencies/></params></image>'
    )
    return (
        PROJECT.replace(b"</files>", twin_file)
        .replace(b"</images>", twin_image)
        .replace(b"</page-split>", twin_split + b"</page-split>")
    )


def test_case_variant_paths_for_one_physical_page_refuse_on_default_apfs_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`spread.tif` and `SPREAD.tif` name one file on macOS's default APFS.

    Without folding case for this identity check, two `<file>` rows spelled
    differently would each carry a saved geometry for what is, on disk, one
    physical page -- exactly the picker rule 8 forbids, reached through a
    filesystem property instead of a project-id collision.
    """
    monkeypatch.setattr(scantailor_worker.sys, "platform", "darwin")
    hostile = _case_variant_project(tmp_path)
    with pytest.raises(ValueError, match="more than one geometry for the same physical page"):
        scantailor_worker.parse(hostile, tmp_path / "case-variant.ScanTailor")


def test_case_variant_paths_are_distinct_pages_off_darwin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off darwin, two differently-cased paths are two real files, not one page.

    Driven at the platform seam rather than by the host: on a Mac the fold is
    the previous test's refusal, so this one pins the non-darwin reading of the
    same project explicitly.
    """
    monkeypatch.setattr(scantailor_worker.sys, "platform", "linux")
    distinct = _case_variant_project(tmp_path)
    parsed = scantailor_worker.parse(distinct, tmp_path / "case-variant.ScanTailor")
    assert len(parsed["geometry"]) == 2


@pytest.mark.parametrize("coordinate", (b"not-a-number", b"1e999", b"nan", b" 600", b"1_0", b""))
def test_a_coordinate_that_is_not_a_finite_number_refuses_by_name(
    tmp_path: Path, coordinate: bytes
) -> None:
    """The exact spelling is preserved, so the spelling has to be checked.

    Nothing converts these strings before they are published, which means an
    unchecked attribute puts arbitrary text -- or a value that is no number at
    all -- inside a digest-bound geometry record.
    """
    hostile = PROJECT.replace(b'<point x="0" y="0"/>', b'<point x="' + coordinate + b'" y="0"/>', 1)
    with pytest.raises(ValueError, match="coordinate is not a finite decimal number"):
        scantailor_worker.parse(hostile, tmp_path / "coordinate.ScanTailor")


def test_the_preview_counts_source_images_and_geometry_records_separately(
    tmp_path: Path,
) -> None:
    """A project may hold images it has no saved split for, and the preview says so."""

    second_page = (
        b'<file id="4" dirId="1" name="second.tif"/></files>',
        b'<image id="5" subPages="2" fileId="4" fileImage="0">'
        b'<size width="1200" height="800"/><dpi horizontal="300" vertical="300"/></image></images>',
    )
    unsplit = PROJECT.replace(b"</files>", second_page[0]).replace(b"</images>", second_page[1])
    parsed = scantailor_worker.parse(unsplit, tmp_path / "partial.ScanTailor")
    assert parsed["source_image_count"] == 2
    assert len(parsed["geometry"]) == 1


def test_a_missing_output_folder_refuses_before_the_operator_is_shown_a_pinned_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The preview holds no write allowance, so it cannot be the thing that finds this.

    Left to the commit, a mistyped folder surfaces as the kernel refusing to build
    a Landlock rule over a path that is not there -- a custody fault, reported
    after a person has already been shown a digest and a pinned promise.
    """
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    code, response = _invoke_main(
        monkeypatch,
        capsys,
        {
            "operation": "preview",
            "project": str(project),
            "output_dir": str(tmp_path / "not-there"),
            "expected_project_sha256": None,
        },
    )
    assert code == 2 and response["status"] == "refusal"
    assert "output folder does not exist" in response["reason"]


def test_a_symlinked_output_folder_refuses_rather_than_writing_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The kernel allowance is built from the resolved path; the write uses the name.

    A symlink makes those two the same only for as long as it points where it did,
    and nothing rechecks it between the preview and the commit.
    """
    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    code, response = _invoke_main(
        monkeypatch,
        capsys,
        {
            "operation": "preview",
            "project": str(project),
            "output_dir": str(tmp_path / "link"),
            "expected_project_sha256": None,
        },
    )
    assert code == 2 and response["status"] == "refusal"
    assert "symbolic link" in response["reason"]
    assert not any((tmp_path / "real").iterdir())


def test_an_existing_document_that_is_not_a_regular_file_refuses_rather_than_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The repeated-commit comparison reads a file in an operator-named folder.

    That read is no more trustworthy than the project file's. A `read_bytes` by
    name -- or a blocking open -- on a planted FIFO hangs the confined child
    having printed nothing, instead of refusing it. This test only terminates
    because the open does not block.
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
    }
    _, preview = _invoke_main(monkeypatch, capsys, preview_request)
    os.mkfifo(Path(preview["summary"]["document_path"]))
    code, response = _invoke_main(
        monkeypatch,
        capsys,
        {
            **preview_request,
            "operation": "commit",
            "expected_project_sha256": preview["summary"]["project_sha256"],
        },
    )
    assert code == 2 and response["status"] == "refusal"
    assert "existing geometry document" in response["reason"]


def test_a_boundary_that_never_came_up_is_not_reported_as_a_project_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two failures send an operator to different places, so they are told apart.

    A custody launcher that could not establish its boundary never read the
    project at all. Reported as a ScanTailor refusal, its copy asks a person to
    correct a named project detail -- in a file nothing looked at.
    """
    from subprocess import CompletedProcess

    from operations.operator import custody
    from operations.operator.errors import ErrorCode, OperatorError

    class _FailedLauncher:
        def launcher_failure(self, completed):
            return "the Landlock launcher could not establish its boundary"

    monkeypatch.setattr(
        scantailor,
        "run_confined",
        lambda command, **k: (
            _FailedLauncher(),
            CompletedProcess(command, custody.SETPRIV_PRIVILEGE_FAILURE_EXIT, "", "setpriv: ..."),
        ),
    )
    with pytest.raises(OperatorError) as raised:
        scantailor.import_in_custody(
            project=tmp_path / "scan.ScanTailor",
            output_dir=tmp_path,
            workspace=tmp_path,
            printer=lambda _: None,
        )
    assert raised.value.code is ErrorCode.CONSOLE_CUSTODY_REFUSED


@pytest.mark.parametrize(
    ("returncode", "response"),
    (
        (2, {"status": "refusal", "reason": "directory fsync failed after publication"}),
        (0, {"status": "preview", "summary": {}}),
    ),
)
def test_a_commit_without_an_exact_committed_result_is_reported_as_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    response: dict,
) -> None:
    from subprocess import CompletedProcess

    from operations.operator.errors import ErrorCode, OperatorError

    class _Backend:
        def launcher_failure(self, completed):
            return None

    monkeypatch.setattr(
        scantailor,
        "run_confined",
        lambda command, **kwargs: (
            _Backend(),
            CompletedProcess(command, returncode, json.dumps(response), ""),
        ),
    )
    with pytest.raises(OperatorError) as raised:
        scantailor._call({}, "commit", writable=tmp_path, workspace=tmp_path)
    assert raised.value.code is ErrorCode.SCANTAILOR_UNRESOLVED
    assert "may have been written" in raised.value.render()


def test_parent_refuses_a_commit_summary_that_cannot_name_the_written_document(
    tmp_path: Path,
) -> None:
    from operations.operator.errors import ErrorCode, OperatorError

    invalid = {
        "status": "committed",
        "summary": {
            "project_sha256": "a" * 64,
            "image_count": 1,
            "geometry_count": 1,
            "document_path": str(tmp_path / "not-the-content-addressed-name.json"),
            "document_sha256": "b" * 64,
        },
    }
    with pytest.raises(OperatorError) as raised:
        scantailor._summary(invalid, operation="commit", output_dir=tmp_path)
    assert raised.value.code is ErrorCode.SCANTAILOR_UNRESOLVED


def test_a_vanished_output_folder_is_not_recreated_by_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from operations.pod import durable

    project = tmp_path / "scan.ScanTailor"
    project.write_bytes(PROJECT)
    output = tmp_path / "geometry"
    output.mkdir()
    request = {
        "operation": "preview",
        "project": str(project),
        "output_dir": str(output),
        "expected_project_sha256": None,
    }
    _, preview = _invoke_main(monkeypatch, capsys, request)
    real_exclusive_write = durable.exclusive_write

    def vanish_then_write(path, payload, **kwargs):
        output.rmdir()
        return real_exclusive_write(path, payload, **kwargs)

    monkeypatch.setattr(scantailor_worker, "exclusive_write", vanish_then_write)
    code, response = _invoke_main(
        monkeypatch,
        capsys,
        {
            **request,
            "operation": "commit",
            "expected_project_sha256": preview["summary"]["project_sha256"],
        },
    )
    assert code == 2 and response["status"] == "refusal"
    assert not output.exists()


def test_documented_word_count_matches_the_scantailor_extended_table() -> None:
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    words = [line for line in readme.splitlines() if line.startswith("| `")]
    assert "## The eleven words" in readme
    assert "Nine things this tool can do" in readme
    assert len(words) == 11


def _worker(command: list[str], *, writable: Path | None, cwd: Path, input_text: str):
    from subprocess import CompletedProcess

    request = json.loads(input_text)
    project = Path(request["project"])
    document = scantailor_worker.parse(project.read_bytes(), project)
    encoded = scantailor_worker.canonical_bytes(document) + b"\n"
    digest = hashlib.sha256(encoded).hexdigest()
    path = Path(request["output_dir"]) / f"scantailor-geometry-{digest}.json"
    if request["operation"] == "commit":
        path.write_bytes(encoded)
    summary = {
        "project_sha256": document["project_sha256"],
        "image_count": document["source_image_count"],
        "geometry_count": 1,
        "document_path": str(path),
        "document_sha256": digest,
    }
    return object(), CompletedProcess(
        command,
        0,
        json.dumps(
            {
                "status": "committed" if request["operation"] == "commit" else "preview",
                "summary": summary,
            }
        ),
        "",
    )
