"""The proof-page picker on a synthetic RecordGold set."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.contracts.canonical import digest_bytes, verify_self_hash

from . import CorpusRefusal
from .local_admission import admit_local_set
from .proof_pages import REPOSITORY, choose, main, pick
from .test_local_admission import _two_page_set


@pytest.fixture
def admitted(tmp_path: Path) -> tuple[Path, list[str]]:
    source = _two_page_set(tmp_path / "recordgold")
    ledger = admit_local_set(source, split="val", output_dir=tmp_path / "admission")
    shas = sorted({page["page"]["sha256"] for page in ledger["reference_pages"]})
    return tmp_path / "admission" / "ledger.json", shas


def test_declared_pages_are_copied_with_their_manifest_and_reference_truth(admitted, tmp_path):
    ledger, shas = admitted
    output = tmp_path / "proof"

    selection = pick(ledger, output, page_shas=[shas[1]])

    assert verify_self_hash(selection)
    assert [page["page_sha256"] for page in selection["pages"]] == [shas[1]]
    assert selection["rule"] == {"declared": [shas[1]]}
    [image] = (output / "pages").iterdir()
    assert digest_bytes(image.read_bytes()) == shas[1]
    manifest = json.loads((output / "submission-manifest.json").read_bytes())
    assert [entry["sha256"] for entry in manifest["files"]] == [shas[1]]
    [reference] = (output / "reference-pages.jsonl").read_text().splitlines()
    assert json.loads(reference)["page"]["sha256"] == shas[1]
    assert json.loads((output / "selection.json").read_bytes()) == selection


def test_a_seeded_draw_is_the_same_every_time_and_named_by_its_rule(admitted, tmp_path):
    ledger, shas = admitted

    first = pick(ledger, tmp_path / "a", count=1, seed="proof-1")
    again = pick(ledger, tmp_path / "b", count=1, seed="proof-1")

    assert first == again
    assert first["rule"] == {"count": 1, "seed": "proof-1", "rank": "sha256(seed:page_sha256)"}
    assert first["admitted_pages"] == 2
    assert choose(shas, count=2, seed="any") == shas


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({}, "no-selection-rule"),
        ({"count": 1}, "no-selection-rule"),
        ({"page_shas": ["f" * 64], "seed": "s"}, "no-selection-rule"),
        ({"count": 3, "seed": "s"}, "count-out-of-range"),
        ({"page_shas": ["f" * 64]}, "page-not-admitted"),
    ],
)
def test_a_selection_without_one_declared_rule_is_refused(admitted, tmp_path, kwargs, reason):
    ledger, _shas = admitted
    with pytest.raises(CorpusRefusal, match=f"^{reason}:"):
        pick(ledger, tmp_path / "proof", **kwargs)


def test_nothing_is_written_into_the_repository_outside_private(admitted, tmp_path):
    ledger, shas = admitted
    with pytest.raises(CorpusRefusal, match="^output-not-private:"):
        pick(ledger, REPOSITORY / "proof-pages-here", page_shas=shas[:1])
    assert not (REPOSITORY / "proof-pages-here").exists()

    with pytest.raises(CorpusRefusal, match="^source-inside-repository:"):
        pick(ledger, tmp_path / "proof", set_root=REPOSITORY, page_shas=shas[:1])


def test_an_output_folder_already_holding_files_is_refused(admitted, tmp_path):
    ledger, shas = admitted
    output = tmp_path / "proof"
    output.mkdir()
    (output / "left-over").write_text("x")
    with pytest.raises(CorpusRefusal, match="^output-not-empty:"):
        pick(ledger, output, page_shas=shas[:1])


def test_a_page_whose_image_changed_since_admission_is_refused(admitted, tmp_path):
    ledger, shas = admitted
    for image in (tmp_path / "recordgold" / "pages").iterdir():
        image.write_bytes(image.read_bytes() + b"\0")
    with pytest.raises(CorpusRefusal, match="^digest-mismatch:"):
        pick(ledger, tmp_path / "proof", page_shas=shas[:1])


def test_the_command_prints_the_chosen_digests(admitted, tmp_path, capsys):
    ledger, shas = admitted
    args = ["--ledger", str(ledger), "--output-root", str(tmp_path / "proof")]

    assert main([*args, "--page-sha", shas[0], "--page-sha", shas[1]]) == 0

    assert capsys.readouterr().out.split() == shas
