"""The witness-routing switch in the models configuration, and the rule's signals."""

from __future__ import annotations

from pathlib import Path

import pytest

from common.chairs.config import load_models_toml, parse_models_config
from common.chairs.errors import ConfigurationRefusal
from common.chairs.models import ModelsConfig
from common.witness_routing import (
    SIGNAL_BOTH,
    SIGNAL_NO_DETECTOR_RECORD,
    SIGNAL_SURYA_TABLE,
    routing_summary,
    signal_of,
)

PIN = "0" * 64
ROOT = Path(__file__).resolve().parents[1]


def _chair(role: str, **extra) -> dict:
    return {
        "state": "configured",
        "source": "local-repository",
        "path": role,
        "digest_manifest": PIN,
        "manifest": f"manifests/{role}.json",
        "serving_recipe": "fixture-recipe-v0",
        "license_note": "fixture",
        **extra,
    }


def _witness(role: str, adapter: str) -> dict:
    return _chair(role, witness_adapter=adapter, witness_scope="page")


def _raw(**top) -> dict:
    return {
        "witness_floor": 3,
        "model_root": "model-fixtures",
        "chairs": {
            "attestator_1": _witness("attestator_1", "chandra.v1"),
            "attestator_2": _witness("attestator_2", "dai.v1"),
            "attestator_3": _witness("attestator_3", "churro.v1"),
            "attestator_4": _witness("attestator_4", "dots-mocr.v1"),
            "designator_surya": _chair("designator_surya"),
            "secondary_proposer": _chair("secondary_proposer"),
        },
        **top,
    }


def test_the_switch_is_a_table_in_the_models_configuration_sealed_only_when_set():
    routed = parse_models_config(_raw(witness_routing={"attestator_4": "index-and-table.v1"}))
    assert dict(routed.witness_routing) == {"attestator_4": "index-and-table.v1"}
    assert routed.to_record()["witness_routing"] == {"attestator_4": "index-and-table.v1"}
    # Off: nothing in the sealed record, so a run that routes nothing seals what it did before.
    off = parse_models_config(_raw())
    assert dict(off.witness_routing) == {}
    assert "witness_routing" not in off.to_record()
    assert routed.models_digest != off.models_digest


@pytest.mark.parametrize(
    ("routing", "chairs", "words"),
    [
        ({"attestator_4": "every-page"}, {}, "the rules are"),
        ({"attestator_9": "index-and-table.v1"}, {}, "no configured witness chair"),
        ({"designator_surya": "index-and-table.v1"}, {}, "no configured witness chair"),
        (
            {f"attestator_{n}": "index-and-table.v1" for n in (1, 2, 3, 4)},
            {},
            "routes every configured witness chair",
        ),
        (
            {"attestator_4": "index-and-table.v1"},
            {"secondary_proposer": {"state": "absent", "reason": "no detector"}},
            "'secondary_proposer' chair",
        ),
        (
            {"attestator_4": "index-and-table.v1"},
            {"designator_surya": {"state": "absent", "reason": "no Surya"}},
            "'designator_surya' chair",
        ),
    ],
)
def test_a_routing_nothing_could_decide_or_that_empties_a_page_is_refused(routing, chairs, words):
    raw = _raw(witness_routing=routing)
    raw["chairs"].update(chairs)
    with pytest.raises(ConfigurationRefusal, match=words):
        parse_models_config(raw)


def test_a_routing_that_leaves_act_pages_below_the_floor_is_refused_when_it_loads():
    """With `attestator_3` absent, only two witnesses read an act page: the routed
    dots.mocr cannot make up the floor of 3 there, so every act page would be held."""
    raw = _raw(witness_routing={"attestator_4": "index-and-table.v1"})
    raw["chairs"]["attestator_3"] = {"state": "absent", "reason": "not served"}
    with pytest.raises(ConfigurationRefusal, match="below the witness floor"):
        parse_models_config(raw)
    raw["witness_floor"] = 2
    status = parse_models_config(raw).witness_floor_status()
    assert status.configured_roles == ("attestator_1", "attestator_2")
    assert status.routed_roles == ("attestator_4",)
    assert status.meets_floor


def test_the_floor_status_never_counts_a_routed_chair():
    """Built without the load-time check, as a caller holding a `ModelsConfig` could."""
    raw = _raw(witness_routing={"attestator_4": "index-and-table.v1"}, witness_floor=2)
    raw["chairs"]["attestator_3"] = {"state": "absent", "reason": "not served"}
    loaded = parse_models_config(raw)
    config = ModelsConfig(
        witness_floor=3,
        chairs=loaded.chairs,
        witness_routing=loaded.witness_routing,
        model_root=loaded.model_root,
    )
    status = config.witness_floor_status()
    assert status.configured_count == 2
    assert (status.deficit, status.meets_floor) == (1, False)


def test_every_committed_roster_still_loads_and_meets_its_floor():
    for path in sorted((ROOT / "config").glob("models*.toml")):
        status = load_models_toml(path).witness_floor_status()
        assert status.meets_floor and status.routed_roles == (), path


def test_either_signal_routes_a_page_and_a_page_with_neither_is_not_routed():
    assert signal_of(["s-1"], 3) == SIGNAL_SURYA_TABLE
    assert signal_of([], 0) == SIGNAL_NO_DETECTOR_RECORD
    assert signal_of(["s-1"], 0) == SIGNAL_BOTH
    assert signal_of([], 2) is None


def test_the_run_health_summary_counts_pages_by_signal_in_page_order():
    decisions = [
        {
            "schema": "witness-routing.v1",
            "page_id": f"pg_{ordinal}",
            "page_ordinal": ordinal,
            "rule": "index-and-table.v1",
            "routed_chairs": ["attestator_4"],
            "routed": signal is not None,
            "signal": signal,
            "surya_table_blocks": tables,
            "detector_record_count": count,
        }
        for ordinal, signal, tables, count in (
            (3, SIGNAL_BOTH, ["a", "b"], 0),
            (1, None, [], 2),
            (2, SIGNAL_SURYA_TABLE, ["c"], 1),
        )
    ]
    summary = routing_summary(decisions)
    assert summary["pages"] == 3 and summary["routed_pages"] == 2
    assert summary["by_signal"] == {"both": 1, "not-routed": 1, "surya-table": 1}
    assert [row["page_ordinal"] for row in summary["decisions"]] == [1, 2, 3]
    assert summary["decisions"][2]["surya_table_blocks"] == 2
