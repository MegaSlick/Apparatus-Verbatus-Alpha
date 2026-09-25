"""An act running across a page break is recorded as a candidate, never linked.

The structure chair answers one page at a time, so an act crossing a page break
comes back as two acts: a head with no tail and a tail with no heading. The
Designator names such a pair in a `continuation-candidate` record, found by
geometry alone, and leaves both acts proposed; whether they are one act is the
Recensor's decision.

The pages are `proof/synthetic_pages.PAGE_BREAK_PAGES`, admitted as a real
submission for the live pass and substituted into the fixture run's two sealed
pages for the declared path: the committed fixture keeps its acts off page
breaks, and its declaration is sealed into every fixture run's digest.
"""

import shutil
from pathlib import Path

import pytest
from test_page_residual_bound import (
    _base_run,
    _designator_context,
    _load_designator,
    _records,
    _substitute_page_pixels,
)
from test_structure_pass import (
    RUN_ID,
    _answer,
    _artifacts,
    _blank_page_answer,
    _live_catalogue,
    _real_submission,
    _run_designator,
    _seal,
    designator,
)

from common.contracts.identities import artifact_id
from common.contracts.stages import DESIGNATOR
from common.runtree.store import RunTree
from common.stage import EXIT_COMPLETE
from proof.synthetic_pages import PAGE_BREAK_PAGES, render_page

ROOT = Path(__file__).resolve().parents[2]
HEAD_PAGE, TAIL_PAGE = PAGE_BREAK_PAGES
HEAD_PAGE_ACTS = tuple(
    (act["bounds"], f"HEAD PAGE ACT {act['ordinal']}") for act in HEAD_PAGE["acts"]
)
TAIL_PAGE_ACTS = tuple(
    (act["bounds"], f"TAIL PAGE ACT {act['ordinal']}") for act in TAIL_PAGE["acts"]
)


def _substitute_page_break(module, monkeypatch) -> None:
    for page in PAGE_BREAK_PAGES:
        _substitute_page_pixels(module, monkeypatch, page["ordinal"], render_page(page))


@pytest.fixture(scope="module")
def chained(tmp_path_factory) -> tuple[Path, Path]:
    base = tmp_path_factory.mktemp("page-break")
    catalogue = _live_catalogue(base)
    pages = {f"page-{page['ordinal']}.png": render_page(page) for page in PAGE_BREAK_PAGES}
    return _real_submission(base, pages, "--serving-recipes-config", str(catalogue)), catalogue


@pytest.fixture()
def live_run(chained, tmp_path: Path) -> tuple[Path, Path]:
    template, catalogue = chained
    shutil.copytree(template, tmp_path / "runs")
    return tmp_path / "runs", catalogue


def _candidates(root: Path) -> list[dict]:
    return _artifacts(root, DESIGNATOR, "continuation-candidate")


def test_an_undeclared_page_break_forms_one_candidate_and_both_acts_stay_proposed(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(HEAD_PAGE_ACTS), _answer(TAIL_PAGE_ACTS)]
    )
    assert exit_code == EXIT_COMPLETE

    (candidate,) = _candidates(root)
    rows = {row["act_key"]: row for row in _seal(root)["payload"]["expected_acts"]}
    assert {key: row["outcome"] for key, row in rows.items()} == {
        "proposal:1:0": "proposed",
        "proposal:1:1": "proposed",
        "proposal:2:0": "proposed",
        "proposal:2:1": "proposed",
    }
    head, tail = rows["proposal:1:1"], rows["proposal:2:0"]
    payload = candidate["payload"]
    assert candidate["outcome"] == "proposed"
    assert candidate["subject_id"] == head["act_id"]
    assert payload["authoritative"] is False
    assert payload["page_a"] == {"page_id": head["page_id"], "page_ordinal": 1}
    assert payload["page_b"] == {"page_id": tail["page_id"], "page_ordinal": 2}
    assert payload["act_a"] == {"act_id": head["act_id"], "act_key": "proposal:1:1"}
    assert payload["act_b"] == {"act_id": tail["act_id"], "act_key": "proposal:2:0"}
    # The scan's own groups: the head reaches the bottom edge, the tail the top.
    assert payload["group_a_bounds"]["y"] + payload["group_a_bounds"]["h"] >= 260 - 7
    assert payload["group_b_bounds"]["y"] <= 7
    assert payload["edge_reach_a_px"] == payload["edge_reach_b_px"] == 7
    assert (
        payload["grouping_config_sha256"]
        == designator.grouping_config.load_grouping_config(
            str(ROOT / "config" / "designator_grouping.toml")
        )["config_sha256"]
    )

    # Cited, not restated: both pages' status and both acts' grouping evidence.
    cited = {reference["relative_path"] for reference in candidate["inputs"]}
    tree = RunTree(root, RUN_ID)
    tree_paths = {
        tree.artifact_path(DESIGNATOR, kind, artifact_id(DESIGNATOR, kind, subject))
        for kind, subject in (
            ("structure-status", head["page_id"]),
            ("structure-status", tail["page_id"]),
            ("act-group", head["act_id"]),
            ("act-group", tail["act_id"]),
        )
    }
    assert cited == tree_paths
    # Enters no seal: the seal's evidence never cites the candidate.
    seal_inputs = {reference["relative_path"] for reference in _seal(root)["inputs"]}
    assert not any("continuation-candidate" in path for path in seal_inputs)


def test_a_fallback_tiled_page_never_forms_a_candidate(live_run, tmp_path, monkeypatch):
    """Fallback tiles touch both edges by construction and would pair any two pages."""
    root, catalogue = live_run
    _endpoint, _exit = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(HEAD_PAGE_ACTS), _blank_page_answer()]
    )
    assert _artifacts(root, DESIGNATOR, "page-fallback")
    assert _candidates(root) == []


def test_a_declared_continuation_forms_no_candidate(tmp_path, monkeypatch):
    """The fixture declares a2's continuation onto page 2; declared is already linked."""
    fixture_designator = _load_designator()
    root = tmp_path / "runs"
    grouping_config = ROOT / "config" / "designator_grouping.toml"
    _base_run(root, grouping_config)
    context = _designator_context(root, fixture_designator, grouping_config)
    _substitute_page_break(fixture_designator, monkeypatch)
    fixture_designator.initial_pass(context)

    (group,) = [
        record for record in _records(context, "act-group") if record["payload"]["continuation"]
    ]
    assert group["payload"]["act_key"] == "a2"
    assert group["payload"]["continuation"]["geometric_corroboration"] is True
    assert _records(context, "continuation-candidate") == []
