"""Three-shard proof for reciprocal boundary records."""

from __future__ import annotations

from itertools import permutations

from common.contracts.errors import ContractError
from common.shard_boundary import (
    CONTINUATION_HOLD,
    SPLIT_RESHOOT_CLUSTER,
    boundary_records,
)

ONE, TWO, THREE = "1" * 64, "2" * 64, "3" * 64


def _three_shards():
    return [
        {"membership_digest": ONE, "page_ordinals": [1, 2]},
        {"membership_digest": TWO, "page_ordinals": [3, 4]},
        {"membership_digest": THREE, "page_ordinals": [5, 6]},
    ]


def test_three_shards_emit_reciprocal_holds_and_a_distinct_split_cluster_finding():
    records = boundary_records(
        _three_shards(),
        continuations=[
            {"act_key": "across-first-wall", "page_ordinal": 2, "continuation_page_ordinal": 3},
            {"act_key": "across-second-wall", "page_ordinal": 4, "continuation_page_ordinal": 5},
        ],
        re_shoot_clusters=[{"cluster_id": "leaf-cluster", "member_page_ordinals": [2, 3, 6]}],
    )

    first_hold = next(record for record in records[ONE] if record["act_key"] == "across-first-wall")
    second_hold = next(
        record for record in records[TWO] if record["act_key"] == "across-first-wall"
    )
    assert first_hold["kind"] == second_hold["kind"] == CONTINUATION_HOLD
    assert first_hold["own_membership_digest"] == second_hold["other_membership_digest"] == ONE
    assert first_hold["other_membership_digest"] == second_hold["own_membership_digest"] == TWO

    # The cluster finding is not an ordinary continuation hold, and each of the
    # three affected shards names precisely the other membership digests.
    for own, others in ((ONE, [TWO, THREE]), (TWO, [ONE, THREE]), (THREE, [ONE, TWO])):
        finding = next(record for record in records[own] if record["kind"] == SPLIT_RESHOOT_CLUSTER)
        assert finding["cluster_id"] == "leaf-cluster"
        assert finding["other_membership_digests"] == others
        assert "artifact" not in finding and "path" not in finding


def test_every_shard_pair_cites_the_other_membership_in_either_partition_order():
    """A reciprocal hold is a pair of facts, not an input-order coincidence."""
    continuations = [
        {"act_key": "one-two", "page_ordinal": 2, "continuation_page_ordinal": 3},
        {"act_key": "two-three", "page_ordinal": 4, "continuation_page_ordinal": 5},
        {"act_key": "three-one", "page_ordinal": 6, "continuation_page_ordinal": 1},
    ]
    owners = {1: ONE, 2: ONE, 3: TWO, 4: TWO, 5: THREE, 6: THREE}

    expected = None
    for ordering in permutations(_three_shards()):
        records = boundary_records(ordering, continuations=continuations)
        if expected is None:
            expected = records
        else:
            assert records == expected

        for continuation in continuations:
            near = owners[continuation["page_ordinal"]]
            far = owners[continuation["continuation_page_ordinal"]]
            pair = [
                record
                for shard_records in records.values()
                for record in shard_records
                if record.get("act_key") == continuation["act_key"]
            ]
            assert {record["own_membership_digest"]: record for record in pair} == {
                near: {
                    "kind": CONTINUATION_HOLD,
                    "act_key": continuation["act_key"],
                    "page_ordinal": continuation["page_ordinal"],
                    "continuation_page_ordinal": continuation["continuation_page_ordinal"],
                    "own_membership_digest": near,
                    "other_membership_digest": far,
                },
                far: {
                    "kind": CONTINUATION_HOLD,
                    "act_key": continuation["act_key"],
                    "page_ordinal": continuation["continuation_page_ordinal"],
                    "continuation_page_ordinal": continuation["page_ordinal"],
                    "own_membership_digest": far,
                    "other_membership_digest": near,
                },
            }


def test_a_split_cluster_finding_fires_only_for_members_that_straddle_shards():
    records = boundary_records(
        _three_shards(),
        re_shoot_clusters=[
            {"cluster_id": "same-shard", "member_page_ordinals": [1, 2]},
            {"cluster_id": "straddled", "member_page_ordinals": [2, 3]},
        ],
    )

    findings = [
        record
        for shard_records in records.values()
        for record in shard_records
        if record["kind"] == SPLIT_RESHOOT_CLUSTER
    ]
    assert {finding["cluster_id"] for finding in findings} == {"straddled"}
    assert {finding["own_membership_digest"] for finding in findings} == {ONE, TWO}
    assert findings == [
        {
            "kind": SPLIT_RESHOOT_CLUSTER,
            "cluster_id": "straddled",
            "member_page_ordinals": [2, 3],
            "own_membership_digest": ONE,
            "other_membership_digests": [TWO],
        },
        {
            "kind": SPLIT_RESHOOT_CLUSTER,
            "cluster_id": "straddled",
            "member_page_ordinals": [2, 3],
            "own_membership_digest": TWO,
            "other_membership_digests": [ONE],
        },
    ]


def test_a_boundary_refuses_a_page_claimed_by_two_shards():
    try:
        boundary_records(
            [
                {"membership_digest": ONE, "page_ordinals": [1]},
                {"membership_digest": TWO, "page_ordinals": [1]},
            ]
        )
    except ContractError as error:
        assert "belongs to two shard memberships" in str(error)
    else:  # pragma: no cover - makes the intended refusal explicit
        raise AssertionError("overlapping shards must not manufacture reciprocal records")
