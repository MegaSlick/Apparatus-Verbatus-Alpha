"""The Coniector's layer in the Armarium: beneath each delivered act, labelled with who
made it, and recomputed on a clean machine.

The tree is the fixture's `happy` scenario read page by page, with the Coniector's
mode on, taken through the real Recensor, Archetypus, Coniector and Armarium.
"""

from __future__ import annotations

import copy
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import armarium_export
import pytest
from armarium_export import EXPORT_MANIFEST_NAME, _zip_bytes, verify_export_bundle
from coniector_layer import CONIECTOR_MEMBER, export_rows

from common.armarium_formats import ArmariumFormats
from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from common.contracts.errors import SchemaRefusal
from common.reconstruction_records import LABEL
from common.runtree.store import RunTree
from common.stage import verify_final_seal
from conftest import build_page_tree, run_stage

RUN_ID = "r"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("coniector-layer")
    config = Path("config/reconstruction.toml")
    root, options = build_page_tree(base, "happy", reconstruction_config=config)
    for program in (
        "pipeline/5_recensor/run.py",
        "pipeline/6_archetypus/run.py",
        "pipeline/4b_coniector/run.py",
        "pipeline/7_armarium/run.py",
    ):
        result = run_stage(root, RUN_ID, "happy", program, **options)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    tree = RunTree(root, RUN_ID)
    export = verify_final_seal(tree)
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return {"data": data, "members": members, "export": export}


def _rows(members: dict) -> dict[tuple[str, ...], dict]:
    rows = [json.loads(line) for line in members[CONIECTOR_MEMBER].decode("utf-8").splitlines()]
    return {tuple(row["act_keys"]): row for row in rows}


def _text(members: dict) -> str:
    [name] = [name for name in members if name.startswith("text/")]
    return members[name].decode("utf-8")


def _repacked(members: dict) -> bytes:
    manifest = json.loads(members[EXPORT_MANIFEST_NAME])
    manifest["members"] = [row for row in manifest["members"] if row["path"] in members]
    for row in manifest["members"]:
        row["sha256"] = digest_bytes(members[row["path"]])
        row["bytes"] = len(members[row["path"]])
    manifest["self_hash"] = self_hash(
        {key: value for key, value in manifest.items() if key != "self_hash"}
    )
    members[EXPORT_MANIFEST_NAME] = canonical_bytes(manifest)
    return _zip_bytes(members)


def test_each_reconstruction_stands_beneath_its_delivered_act_labelled_with_its_maker(bundle):
    rows = _rows(bundle["members"])
    assert sorted(rows) == [("p1:1",), ("p1:2",), ("p2:1",)]
    row = rows[("p1:1",)]
    assert row["label"] == LABEL and row["made"] is True
    assert row["maker"]["kind"] == "model" and row["maker"]["chair"] == "reconstructor"
    assert row["reconstruction_text"] == "SYNTHETIC ACT ONE alpha beta gamma"
    assert rows[("p1:2",)]["flags"] == [
        {"code": "cut-at-page-break", "reason": "the act runs on past the page"}
    ]
    text = _text(bundle["members"])
    section = text[text.index("act-id: " + row["act_ids"][0]) :]
    section = section[: section.index("\n\n")]
    assert 'reconstruction_maker:\n"model, chair reconstructor' in section
    assert 'reconstruction_text:\n"SYNTHETIC ACT ONE alpha beta gamma"' in section
    # The delivered text is the diplomatic reading, untouched by its reconstruction.
    delivered = {act["act_key"]: act["text"] for act in bundle["export"]["payload"]["delivered"]}
    assert delivered["p2:1"] == "SYNTHETIC ACT TWO delta epsilon zeta eta"
    assert rows[("p2:1",)]["reconstruction_text"] == "SYNTHETIC ACT TWO delta epsilon zeta theta"


def test_the_clean_verifier_accepts_the_layer_it_recomputes(bundle, tmp_path):
    verify_export_bundle(bundle["data"], tmp_path / "clean")


def _change_row(members: dict, change) -> None:
    lines = members[CONIECTOR_MEMBER].decode("utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    change(rows)
    members[CONIECTOR_MEMBER] = (
        "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n"
    ).encode("utf-8")


def _change_text_bundle(members: dict, old: str, new: str) -> None:
    [name] = [name for name in members if name.startswith("text/")]
    text = members[name].decode("utf-8")
    assert text.count(old) == 1
    members[name] = text.replace(old, new).encode("utf-8")


def _swap_blocks(members: dict) -> None:
    """Move p1:1's reconstruction beneath p1:2 and p1:2's beneath p1:1."""
    [name] = [name for name in members if name.startswith("text/")]
    lines = members[name].decode("utf-8").split("\n")
    starts = [index for index, line in enumerate(lines) if line.startswith("reconstruction_label:")]
    ends = [index + 2 for index, line in enumerate(lines) if line == "reconstruction_row:"]
    first, second = lines[starts[0] : ends[0]], lines[starts[1] : ends[1]]
    lines = lines[: starts[0]] + second + lines[ends[0] : starts[1]] + first + lines[ends[1] :]
    members[name] = "\n".join(lines).encode("utf-8")


def _drop_from_sources(members: dict) -> None:
    sources = json.loads(members["sources.json"])
    sources["reconstructions"] = sources["reconstructions"][1:]
    members["sources.json"] = canonical_bytes(sources)


def _first(rows):
    return next(row for row in rows if row["act_keys"] == ["p1:1"])


@pytest.mark.parametrize(
    "change, refusal",
    [
        (
            lambda members: _change_row(
                members, lambda rows: _first(rows).update(reconstruction_raw="invented")
            ),
            "not its departures applied",
        ),
        (
            lambda members: _change_row(
                members,
                lambda rows: _first(rows).update(diplomatic_raw_pieces=["another reading"]),
            ),
            "departs from a text other than its literal",
        ),
        (
            lambda members: _change_row(members, lambda rows: _first(rows).update(label="final")),
            "not labelled as a reconstruction",
        ),
        (
            lambda members: _change_row(
                members, lambda rows: _first(rows)["maker"].update(kind="oracle")
            ),
            "does not say who made it",
        ),
        (lambda members: members.pop(CONIECTOR_MEMBER), r"missing=\['coniector.jsonl'\]"),
        (
            lambda members: _change_row(members, lambda rows: rows.remove(_first(rows))),
            "other reconstructions than its sources record",
        ),
        (_drop_from_sources, "other reconstructions than its sources record"),
        (
            lambda members: _change_text_bundle(
                members,
                'reconstruction_text:\n"SYNTHETIC ACT ONE alpha beta gamma"',
                'reconstruction_text:\n"SYNTHETIC ACT ONE alpha beta delta"',
            ),
            "do not say what its row says",
        ),
        (_swap_blocks, "beneath another act's section"),
        (
            lambda members: _change_row(
                members, lambda rows: _first(rows).update(act_keys=["p9:9"])
            ),
            "by another act's key",
        ),
        (
            lambda members: _change_row(members, lambda rows: _first(rows).update(act_ids=[["x"]])),
            "does not name its pieces",
        ),
    ],
    ids=[
        "text",
        "diplomatic",
        "label",
        "maker",
        "member-dropped",
        "row-dropped",
        "sources-row-dropped",
        "text-bundle-line",
        "moved-block",
        "act-key",
        "untyped",
    ],
)
def test_the_clean_verifier_refuses_a_tampered_reconstruction(bundle, tmp_path, change, refusal):
    members = copy.deepcopy(bundle["members"])
    change(members)
    with pytest.raises(SchemaRefusal, match=refusal):
        verify_export_bundle(_repacked(members), tmp_path / "clean")


def _record(act_id: str, clean: str) -> dict:
    return {
        "unit": "act",
        "act_ids": [act_id],
        "act_keys": ["p1:1"],
        "label": LABEL,
        "made": False,
        "maker": {},
        "diplomatic_clean_sha256s": [digest_bytes(clean.encode("utf-8"))],
        "reconstruction_raw": None,
        "reconstruction_text": None,
        "reconstruction_uncertainty": None,
        "continues": None,
        "departures": [],
        "findings": [],
        "not_made": [{"code": "reply-malformed", "detail": "x"}],
    }


def test_a_reconstruction_is_shown_only_beneath_its_delivered_reading():
    record = _record("act_a", "the reading")
    refs = {"act_a": {"relative_path": "r", "sha256": "0" * 64}}
    assert export_rows([record], {"act_a": "the reading"}, {}, refs) == []
    (row,) = export_rows([record], {"act_a": "the reading"}, {"act_a": "the reading"}, refs)
    assert row["not_made"] == [{"code": "reply-malformed", "detail": "x"}]
    with pytest.raises(SchemaRefusal, match="over a reading other than the one delivered"):
        export_rows([record], {"act_a": "the reading"}, {"act_a": "another"}, refs)


def test_free_text_is_one_json_line_so_no_reason_can_start_a_line_the_parser_reads():
    from coniector_layer import reconstruction_lines, text_bundle_placements

    row = {
        "schema": "armarium-coniector-reconstruction.v1",
        "unit": "act",
        "act_ids": ["act_a"],
        "act_keys": ["p1:1"],
        "label": LABEL,
        "made": False,
        "maker": {
            "kind": "model",
            "chair": "reconstructor",
            "chair_state": "configured",
            "resolved_identity": None,
            "resolved_revision": None,
            "receipt_ref": None,
        },
        "diplomatic_raw_pieces": ["the reading"],
        "reconstruction_raw": None,
        "reconstruction_text": None,
        "reconstruction_uncertainty": None,
        "continues": None,
        "departures": [],
        "flags": [{"code": "other", "reason": "line one\nact-id: act_b\n## p9:9"}],
        "not_made": [{"code": "reply-malformed", "detail": "x\ny"}],
        "record_ref": {"availability": "requires-retained-run-access"},
    }
    lines = reconstruction_lines(row)
    assert not any(line.startswith(("act-id: ", "## ")) for line in lines)
    acts, placed = text_bundle_placements(["## p1:1 (act_a)", "act-id: act_a", *lines])
    assert acts == {"act_a"} and placed == [("act:act_a", row)]


@pytest.fixture(scope="module")
def joined(tmp_path_factory) -> dict:
    base = tmp_path_factory.mktemp("coniector-join")
    config = base / "reconstruction.toml"
    config.write_text(
        Path("config/reconstruction.toml")
        .read_text(encoding="utf-8")
        .replace("pages_are_consecutive = false", "pages_are_consecutive = true"),
        encoding="utf-8",
    )
    root, options = build_page_tree(base, "happy", reconstruction_config=config)
    for program in (
        "pipeline/5_recensor/run.py",
        "pipeline/6_archetypus/run.py",
        "pipeline/4b_coniector/run.py",
        "pipeline/7_armarium/run.py",
    ):
        result = run_stage(root, RUN_ID, "happy", program, **options)
        assert result.returncode in (0, 3), f"{program}: {result.stderr}"
    tree = RunTree(root, RUN_ID)
    export = verify_final_seal(tree)
    data = tree.read_bytes(export["payload"]["bundle"]["reference"]["relative_path"])
    with ZipFile(BytesIO(data)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return {"data": data, "members": members}


def test_a_join_on_consecutive_pages_is_its_own_section_and_verifies(joined, tmp_path):
    verify_export_bundle(joined["data"], tmp_path / "clean")
    rows = _rows(joined["members"])
    join = rows[("p1:2", "p2:1")]
    assert join["unit"] == "join" and join["continues"] is True
    assert "## JOIN RECONSTRUCTION p1:2 + p2:1 (not an act)" in _text(joined["members"])
    members = copy.deepcopy(joined["members"])
    _change_text_bundle(
        members, "## JOIN RECONSTRUCTION p1:2 + p2:1", "## JOIN RECONSTRUCTION p2:1 + p1:2"
    )
    with pytest.raises(SchemaRefusal, match="join section is not its row's"):
        verify_export_bundle(_repacked(members), tmp_path / "forged")


def _verify_two_folders(monkeypatch, tmp_path, shown_by_folder: dict[str, list[dict]]) -> None:
    """Run the text-bundle reconstruction check over two folders that section `act_a`."""
    monkeypatch.setattr(armarium_export, "_text_bundle_records", lambda _root: {})
    monkeypatch.setattr(armarium_export, "_package_lines", lambda path, _label: [str(path)])
    monkeypatch.setattr(
        armarium_export,
        "text_bundle_placements",
        lambda lines: (
            {"act_a"},
            [
                ("act:act_a", row)
                for folder, rows in shown_by_folder.items()
                if f"/{folder}/" in lines[0]
                for row in rows
            ],
        ),
    )
    monkeypatch.setattr(
        armarium_export, "verify_row", lambda row, _literals, _keys, _corrected=None: row
    )
    monkeypatch.setattr(armarium_export, "_verify_retained_references", lambda _row: None)
    sources = {
        "pages": [{"declared_path": f"{folder}/folio.png"} for folder in shown_by_folder],
        "reconstructions": [["act_a"]],
    }
    armarium_export._verify_coniector_layer(
        tmp_path, ArmariumFormats(("text-bundle",), False), sources, set(), {}
    )


def _shown(text: str) -> dict:
    return {"act_ids": ["act_a"], "act_keys": ["p1:1"], "reconstruction_text": text}


def test_the_text_bundle_check_accepts_one_row_shown_alike_in_every_folder(monkeypatch, tmp_path):
    _verify_two_folders(monkeypatch, tmp_path, {"a": [_shown("one")], "b": [_shown("one")]})


@pytest.mark.parametrize(
    "shown_by_folder, refusal",
    [
        ({"a": [_shown("one")], "b": [_shown("two")]}, "differently in two places"),
        ({"a": [_shown("one")], "b": []}, "exactly once in every folder"),
        ({"a": [_shown("one"), _shown("one")], "b": [_shown("one")]}, "exactly once"),
    ],
    ids=["shown-differently", "missing-from-a-folder", "shown-twice-in-a-folder"],
)
def test_the_text_bundle_check_refuses_a_row_not_shown_once_alike_in_every_folder(
    monkeypatch, tmp_path, shown_by_folder, refusal
):
    with pytest.raises(SchemaRefusal, match=refusal):
        _verify_two_folders(monkeypatch, tmp_path, shown_by_folder)
