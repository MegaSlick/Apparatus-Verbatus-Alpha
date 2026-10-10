"""dots.mocr as a fourth witness on index and table pages only, through the whole fixture run.

A roster that seats dots.mocr (`attestator_4`, adapter `dots-mocr.v1`) under
`[witness_routing]` sends it a page when Surya tags a `Table` block on it or the
record detector finds no record on it (`common/witness_routing.py`). The
synthetic fixture's `dots-*` scenarios make page 2 such a page; page 1 stays an
act page with two records and no table. The committed roster seats no such
chair, so a run under it is the run it always was.

Every run here is the real orchestrator over the synthetic fixture; nothing is
served and nothing leaves the machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import DOTS_CHAIR, dots_models_config, dots_serving_recipes, run_orchestrator

DOTS = DOTS_CHAIR
BASE_CHAIRS = ["attestator_1", "attestator_2", "attestator_3"]
PAGE_TWO_TEXT = "SYNTHETIC ACT TWO delta epsilon zeta eta"


def _run(base: Path, scenario: str, *, roster: str = "routed") -> Path:
    """One orchestrator run: `routed` seats dots.mocr by rule, `committed` is config/ as is."""
    options: dict[str, object] = {}
    if roster != "committed":
        options = {
            "models_config": dots_models_config(base / "models", routed=roster == "routed"),
            "serving_recipes_config": dots_serving_recipes(base),
        }
    root = base / "runs"
    result = run_orchestrator(root, "r", scenario, **options)
    # The fixture's pages carry an act across their break, which no code joins,
    # so a run that reaches its export is partial (3), as `happy` always is.
    assert result.returncode == 3, result.stdout + result.stderr
    return root / "r"


def _records(run: Path, stage_dir: str, kind: str) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run / stage_dir / "artifacts" / kind).glob("*.json"))
    ]


def _testimonia(run: Path) -> dict[int, dict[str, dict]]:
    by_page: dict[int, dict[str, dict]] = {}
    for record in _records(run, "3_attestatores", "page-testimonium"):
        payload = record["payload"]
        by_page.setdefault(payload["page_ordinal"], {})[payload["chair"]] = record
    return by_page


def _feeds(run: Path) -> dict[int, dict]:
    return {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _records(run, "4_perlector", "page-feed")
    }


def _shown(feed: dict) -> dict:
    """A feed as the reading is shown it: every record reference's digest set aside.

    A record's bytes bind its run's `config_digest`, which a roster that seats
    another chair moves, so the references differ while what they name, and
    what the prompt says, does not. The rendered prompt's digest stays in.
    """

    def strip(value):
        if isinstance(value, dict):
            return {
                key: strip(item)
                for key, item in value.items()
                if key != "feed_digest" and not (key == "sha256" and "relative_path" in value)
            }
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(feed)


def _coverage(run: Path) -> dict[str, dict]:
    return {
        record["payload"]["act_key"]: record["payload"]["coverage"]
        for record in _records(run, "5_recensor", "review")
    }


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, Path]:
    """Each scenario once under the routed roster, and twice under rosters without routing."""
    made = {}
    for name, scenario, roster in (
        ("table", "dots-table", "routed"),
        ("no-record", "dots-no-record", "routed"),
        ("cut-off", "dots-cut-off", "routed"),
        ("table-committed", "dots-table", "committed"),
        ("happy-routed", "happy", "routed"),
        ("happy-committed", "happy", "committed"),
    ):
        made[name] = _run(tmp_path_factory.mktemp(name), scenario, roster=roster)
    return made


def test_a_page_surya_tags_a_table_is_read_by_dots_and_the_decision_is_recorded(runs):
    run = runs["table"]
    routing = {
        record["payload"]["page_ordinal"]: record
        for record in _records(run, "3_attestatores", "witness-routing")
    }
    assert set(routing) == {1, 2}
    page_two = routing[2]["payload"]
    assert page_two["routed"] is True and page_two["signal"] == "surya-table"
    assert page_two["rule"] == "index-and-table.v1"
    assert page_two["routed_chairs"] == [DOTS]
    assert len(page_two["surya_table_blocks"]) == 1
    assert page_two["detector_record_count"] == 1
    # The decision binds the Designator records it was read from: the census,
    # the Table block and the detector's census.
    assert len(routing[2]["inputs"]) == 3
    assert all("2_designator/" in ref["relative_path"] for ref in routing[2]["inputs"])

    testimonia = _testimonia(run)
    assert sorted(testimonia[2]) == BASE_CHAIRS + [DOTS]
    dots = testimonia[2][DOTS]
    assert dots["outcome"] == "read"
    assert dots["payload"]["payload"] == PAGE_TWO_TEXT
    assert dots["payload"]["native_capture"]["adapter"] == "dots-mocr.v1"
    assert dots["payload"]["native_capture"]["text_view"] == "dots-layout-text.v1"
    # Its one cell, mapped from the 196x252 image the processor saw back onto page 2.
    (observed,) = dots["payload"]["observed"]
    assert observed["bounds_source"] == "native"
    assert observed["bounds"] == {"x": 20, "y": 20, "w": 160, "h": 61}

    # dots.mocr's Table cell joins the page-type cross-check's table evidence.
    accounting = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _records(run, "4_perlector", "page-accounting")
    }
    assert accounting[2]["page_type"]["facts"]["witness_table_units"][DOTS] == 1
    assert DOTS not in accounting[1]["page_type"]["facts"]["witness_table_units"]

    feed = _feeds(run)[2]
    letters = {row["letter"]: row for row in feed["witnesses"]}
    assert sorted(letters) == ["A", "B", "C", "D"]
    assert letters["D"]["chair"] == DOTS
    (unit,) = letters["D"]["units"]
    assert unit["id"] == "D1" and unit["label"] == "Table"
    assert unit["text"] == PAGE_TWO_TEXT

    summary = json.loads((run / "run-health" / "witness-routing.json").read_text("utf-8"))
    assert summary["routed_pages"] == 1 and summary["pages"] == 2
    assert summary["by_signal"] == {"not-routed": 1, "surya-table": 1}
    assert [row["signal"] for row in summary["decisions"]] == [None, "surya-table"]


def test_a_page_the_detector_finds_nothing_on_is_read_by_dots_beside_dais_empty_seat(runs):
    run = runs["no-record"]
    routing = {
        record["payload"]["page_ordinal"]: record["payload"]
        for record in _records(run, "3_attestatores", "witness-routing")
    }
    assert routing[2]["signal"] == "zero-detector-records"
    assert routing[2]["detector_record_count"] == 0 and routing[2]["surya_table_blocks"] == []
    page_two = _testimonia(run)[2]
    assert page_two["attestator_2"]["outcome"] == "genuinely-empty"
    assert page_two[DOTS]["outcome"] == "read"
    coverage = _coverage(run)["p2:1"]
    assert coverage["configured"] == 4
    assert coverage["by_outcome"] == {"genuinely-empty": 1, "read": 3}
    assert coverage["under_witnessed"] is False


def test_an_act_page_is_not_routed_and_reads_exactly_as_it_does_without_dots(runs):
    for name in ("table", "no-record", "cut-off", "happy-routed"):
        run = runs[name]
        routing = {
            record["payload"]["page_ordinal"]: record["payload"]
            for record in _records(run, "3_attestatores", "witness-routing")
        }
        assert routing[1]["routed"] is False and routing[1]["signal"] is None, name
        assert sorted(_testimonia(run)[1]) == BASE_CHAIRS, name
        assert [row["chair"] for row in _feeds(run)[1]["witnesses"]] == BASE_CHAIRS, name
        assert _coverage(run)["p1:1"]["configured"] == 3, name
    # What the Perlector is shown on an act page is the committed roster's: the
    # same witnesses, units, Surya evidence and rendered prompt.
    for routed, committed in (("table", "table-committed"), ("happy-routed", "happy-committed")):
        assert _shown(_feeds(runs[routed])[1]) == _shown(_feeds(runs[committed])[1])
    # A routed roster on a run with no index or table page reads every page as before.
    happy_routed, happy_committed = _feeds(runs["happy-routed"]), _feeds(runs["happy-committed"])
    assert {page: _shown(feed) for page, feed in happy_routed.items()} == {
        page: _shown(feed) for page, feed in happy_committed.items()
    }


def test_with_the_switch_off_no_routing_is_recorded_and_no_page_seats_dots(runs):
    for name in ("table-committed", "happy-committed"):
        run = runs[name]
        assert _records(run, "3_attestatores", "witness-routing") == []
        assert not (run / "run-health" / "witness-routing.json").exists()
        assert all(sorted(chairs) == BASE_CHAIRS for chairs in _testimonia(run).values())
        run_json = json.loads((run / "run.json").read_text("utf-8"))
        assert run_json["witness_chairs"] == BASE_CHAIRS


def test_dots_counts_toward_the_floor_on_its_page_and_a_cut_off_answer_is_a_shortfall(runs):
    run = runs["cut-off"]
    dots = _testimonia(run)[2][DOTS]
    assert dots["outcome"] == "failed"
    assert dots["payload"]["native_capture"]["parse"]["state"] == "failed"
    # Retained, never salvaged: the broken JSON is the record's raw response.
    assert "never salvaged" in dots["payload"]["native_capture"]["parse"]["reason"]
    coverage = _coverage(run)["p2:1"]
    # Four seated on the routed page, three read: the 3-of-3 floor's three is met.
    assert coverage["configured"] == 4 and coverage["floor"] == 3
    assert coverage["by_outcome"] == {"failed": 1, "read": 3}
    assert coverage["shortfalls"]["failed"] == 1
    assert coverage["under_witnessed"] is False
    # On the table run, all four read.
    assert _coverage(runs["table"])["p2:1"]["by_outcome"] == {"read": 4}
