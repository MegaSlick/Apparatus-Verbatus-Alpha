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


def _pages(*pages) -> dict[str, bytes]:
    return {f"page-{index}.png": render_page(page) for index, page in enumerate(pages, 1)}


def _page(*acts) -> dict:
    return {
        "ordinal": 0,
        "width": 200,
        "height": 260,
        "acts": tuple(
            {"ordinal": index, "bounds": bounds, "ink": 40} for index, bounds in enumerate(acts)
        ),
    }


# Two columns, each reaching the bottom of page 1 and opening page 2. The scan
# groups side-by-side ink into one row group, so each page edge carries one
# group that two of the chair's acts share.
LEFT_COLUMN, RIGHT_COLUMN = {"x": 40, "w": 60}, {"x": 120, "w": 60}
TWO_COLUMN_HEAD = tuple({**column, "y": 150, "h": 110} for column in (LEFT_COLUMN, RIGHT_COLUMN))
TWO_COLUMN_TAIL = tuple({**column, "y": 0, "h": 60} for column in (LEFT_COLUMN, RIGHT_COLUMN))


@pytest.fixture(scope="module")
def submitted(tmp_path_factory):
    """One real submission per page set, built once and copied per test."""
    base = tmp_path_factory.mktemp("page-break")
    catalogue = _live_catalogue(base)
    templates = {}

    def template(name: str, pages: dict[str, bytes]) -> Path:
        if name not in templates:
            templates[name] = _real_submission(
                base / name, pages, "--serving-recipes-config", str(catalogue)
            )
        return templates[name]

    return template, catalogue


def _live_run(submitted, tmp_path: Path, name: str, pages: dict[str, bytes]):
    template, catalogue = submitted
    root = tmp_path / "runs"
    shutil.copytree(template(name, pages), root)
    return root, catalogue


@pytest.fixture()
def live_run(submitted, tmp_path: Path) -> tuple[Path, Path]:
    return _live_run(submitted, tmp_path, "page-break", _pages(*PAGE_BREAK_PAGES))


def _candidates(root: Path) -> list[dict]:
    return _artifacts(root, DESIGNATOR, "continuation-candidate")


def _named(rows: dict, *keys: str) -> list[dict]:
    return [{"act_id": rows[key]["act_id"], "act_key": key} for key in keys]


def _rows(root: Path) -> dict[str, dict]:
    return {row["act_key"]: row for row in _seal(root)["payload"]["expected_acts"]}


def test_an_undeclared_page_break_forms_one_candidate_and_both_acts_stay_proposed(
    live_run, tmp_path, monkeypatch
):
    root, catalogue = live_run
    _endpoint, exit_code = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(HEAD_PAGE_ACTS), _answer(TAIL_PAGE_ACTS)]
    )
    assert exit_code == EXIT_COMPLETE

    (candidate,) = _candidates(root)
    rows = _rows(root)
    assert {key: row["outcome"] for key, row in rows.items()} == {
        "proposal:1:0": "proposed",
        "proposal:1:1": "proposed",
        "proposal:2:0": "proposed",
        "proposal:2:1": "proposed",
    }
    head, tail = rows["proposal:1:1"], rows["proposal:2:0"]
    payload = candidate["payload"]
    assert candidate["outcome"] == "proposed"
    assert payload["authoritative"] is False
    assert payload["page_a"] == {"page_id": head["page_id"], "page_ordinal": 1}
    assert payload["page_b"] == {"page_id": tail["page_id"], "page_ordinal": 2}
    assert payload["acts_a"] == _named(rows, "proposal:1:1")
    assert payload["acts_b"] == _named(rows, "proposal:2:0")
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


def test_a_crossing_with_no_proposed_act_over_its_tail_is_still_recorded(
    live_run, tmp_path, monkeypatch
):
    """The chair drew nothing over the tail, so no act can be named there; the
    crossing is published with an empty side rather than dropped."""
    root, catalogue = live_run
    _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(HEAD_PAGE_ACTS), _answer(TAIL_PAGE_ACTS[1:])],
    )
    (candidate,) = _candidates(root)
    rows = _rows(root)
    assert candidate["payload"]["acts_a"] == _named(rows, "proposal:1:1")
    assert candidate["payload"]["acts_b"] == []


def test_every_act_tied_at_the_edge_is_named(submitted, tmp_path, monkeypatch):
    """Two columns share one scanned group at each edge; neither act is chosen."""
    root, catalogue = _live_run(
        submitted, tmp_path, "two-column", _pages(_page(*TWO_COLUMN_HEAD), _page(*TWO_COLUMN_TAIL))
    )
    _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [
            _answer(tuple((bounds, "HEAD") for bounds in TWO_COLUMN_HEAD)),
            _answer(tuple((bounds, "TAIL") for bounds in TWO_COLUMN_TAIL)),
        ],
    )
    (candidate,) = _candidates(root)
    rows = _rows(root)
    assert candidate["payload"]["acts_a"] == _named(rows, "proposal:1:0", "proposal:1:1")
    assert candidate["payload"]["acts_b"] == _named(rows, "proposal:2:0", "proposal:2:1")


def test_a_fallback_tiled_page_never_forms_a_candidate(live_run, tmp_path, monkeypatch):
    """Fallback tiles touch both edges by construction and would pair any two pages."""
    root, catalogue = live_run
    _endpoint, _exit = _run_designator(
        root, catalogue, tmp_path, monkeypatch, [_answer(HEAD_PAGE_ACTS), _blank_page_answer()]
    )
    assert _artifacts(root, DESIGNATOR, "page-fallback")
    assert _candidates(root) == []


def test_a_page_whose_scan_fell_back_to_tiles_never_forms_a_candidate(
    submitted, tmp_path, monkeypatch
):
    """The chair drew an act on an ink-free page, so the page is proposed but its
    scan groups are the fallback grid, which touches the top edge by construction."""
    root, catalogue = _live_run(
        submitted, tmp_path, "blank-tail", _pages(PAGE_BREAK_PAGES[0], _page())
    )
    _run_designator(
        root,
        catalogue,
        tmp_path,
        monkeypatch,
        [_answer(HEAD_PAGE_ACTS), _answer(TAIL_PAGE_ACTS[:1])],
    )
    assert "proposal:2:0" in _rows(root)
    assert _candidates(root) == []


def test_a_resumed_pass_publishes_the_same_candidate_once(live_run, tmp_path, monkeypatch):
    root, catalogue = live_run
    answers = [_answer(HEAD_PAGE_ACTS), _answer(TAIL_PAGE_ACTS)]
    _run_designator(root, catalogue, tmp_path, monkeypatch, answers)
    before = _candidates(root)
    endpoint, exit_code = _run_designator(root, catalogue, tmp_path, monkeypatch, [])
    assert exit_code == EXIT_COMPLETE
    assert endpoint.requests == []
    assert _candidates(root) == before
    assert len(before) == 1


def _fixture_pass(tmp_path: Path, monkeypatch, *, drop_declared_continuation: bool):
    fixture_designator = _load_designator()
    root = tmp_path / "runs"
    grouping_config = ROOT / "config" / "designator_grouping.toml"
    _base_run(root, grouping_config)
    context = _designator_context(root, fixture_designator, grouping_config)
    if drop_declared_continuation:
        # In memory only, after the run bound the declaration: a2 now reaches
        # the break undeclared, with nothing proposed over its tail on page 2.
        context.fixture["continuation"] = []
    _substitute_page_break(fixture_designator, monkeypatch)
    fixture_designator.initial_pass(context)
    return context


def test_a_declared_continuation_forms_no_candidate(tmp_path, monkeypatch):
    """The fixture declares a2's continuation onto page 2; declared is already linked."""
    context = _fixture_pass(tmp_path, monkeypatch, drop_declared_continuation=False)
    (group,) = [
        record for record in _records(context, "act-group") if record["payload"]["continuation"]
    ]
    assert group["payload"]["act_key"] == "a2"
    assert group["payload"]["continuation"]["geometric_corroboration"] is True
    assert _records(context, "continuation-candidate") == []


def test_the_fixture_pass_records_an_undeclared_crossing(tmp_path, monkeypatch):
    context = _fixture_pass(tmp_path, monkeypatch, drop_declared_continuation=True)
    (candidate,) = _records(context, "continuation-candidate")
    assert [act["act_key"] for act in candidate["payload"]["acts_a"]] == ["a2"]
    assert candidate["payload"]["acts_b"] == []
