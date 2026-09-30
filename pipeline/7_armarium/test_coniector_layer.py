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

import pytest
from armarium_export import EXPORT_MANIFEST_NAME, _zip_bytes, verify_export_bundle
from coniector_layer import CONIECTOR_MEMBER, export_rows

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
    config = base / "reconstruction.toml"
    config.write_text(
        Path("config/reconstruction.toml")
        .read_text(encoding="utf-8")
        .replace('mode = "off"', 'mode = "on"'),
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
    assert "reconstruction_maker: model, chair reconstructor" in section
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
        (lambda members: members.pop(CONIECTOR_MEMBER), "show different reconstructions"),
        (
            lambda members: _change_text_bundle(
                members,
                'reconstruction_text:\n"SYNTHETIC ACT ONE alpha beta gamma"',
                'reconstruction_text:\n"SYNTHETIC ACT ONE alpha beta delta"',
            ),
            "do not say what its row says",
        ),
    ],
    ids=["text", "diplomatic", "label", "maker", "member-dropped", "text-bundle-line"],
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
