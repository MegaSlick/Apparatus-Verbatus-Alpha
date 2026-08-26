"""Corpus-boundary records for facts that span otherwise isolated run trees.

Each shard remains a complete, self-contained run tree.  A boundary record may
name the *membership digest* of another shard, which is enough for a person to
pair the two records, but it must never contain an artifact path or a reading
from that tree.  The records are deliberately constructed from the corpus
partition, before either shard attempts to reconcile the other one's frames.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from common.contracts.errors import ContractError

CONTINUATION_HOLD: Final = "cross-shard-continuation-hold"
SPLIT_RESHOOT_CLUSTER: Final = "split-re-shoot-cluster"


def _digest(value: object, what: str) -> str:
    if not (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        raise ContractError(f"{what} must be a lowercase sha256 digest")
    return value


def _partition(shards: Iterable[Mapping[str, Any]]) -> tuple[dict[int, str], dict[str, list[int]]]:
    """Validate the one-owner page partition and return page/frame lookups."""
    owner: dict[int, str] = {}
    members: dict[str, list[int]] = {}
    for shard in shards:
        if not isinstance(shard, Mapping) or set(shard) != {
            "membership_digest",
            "page_ordinals",
        }:
            raise ContractError(
                "a shard boundary entry must contain membership_digest and page_ordinals"
            )
        frame = _digest(shard["membership_digest"], "shard membership_digest")
        pages = shard["page_ordinals"]
        if (
            not isinstance(pages, list)
            or not pages
            or any(
                not isinstance(page, int) or isinstance(page, bool) or page < 1 for page in pages
            )
        ):
            raise ContractError(
                "a shard boundary entry needs non-empty positive integer page_ordinals"
            )
        if pages != sorted(set(pages)):
            raise ContractError("shard boundary page_ordinals must be sorted and unique")
        if frame in members:
            raise ContractError("a corpus boundary may name one membership digest only once")
        members[frame] = pages
        for page in pages:
            if page in owner:
                raise ContractError(f"page ordinal {page} belongs to two shard memberships")
            owner[page] = frame
    if not owner:
        raise ContractError("a corpus boundary needs at least one submitted shard")
    expected = list(range(1, max(owner) + 1))
    if sorted(owner) != expected:
        missing = sorted(set(expected) - set(owner))
        raise ContractError(
            f"the shard partition omits page ordinal(s) {missing}; every page from 1 "
            "through the corpus maximum must have exactly one owner"
        )
    return owner, members


def boundary_records(
    shards: Iterable[Mapping[str, Any]],
    *,
    continuations: Iterable[Mapping[str, Any]] = (),
    re_shoot_clusters: Iterable[Mapping[str, Any]] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Return the per-membership boundary records for a corpus partition.

    A continuation produces exactly two reciprocal holds when its endpoints
    straddle a wall.  A re-shoot cluster produces a separate named finding in
    every shard containing a member when the cluster spans more than one shard.
    Neither record carries a remote path, artifact identity, or reading.
    """
    owner, members = _partition(shards)
    output = {frame: [] for frame in members}
    continuation_keys: set[str] = set()

    for continuation in continuations:
        if not isinstance(continuation, Mapping) or set(continuation) != {
            "act_key",
            "page_ordinal",
            "continuation_page_ordinal",
        }:
            raise ContractError(
                "a boundary continuation must contain act_key, page_ordinal, and continuation_page_ordinal"
            )
        key = continuation["act_key"]
        near, far = continuation["page_ordinal"], continuation["continuation_page_ordinal"]
        if not isinstance(key, str) or not key:
            raise ContractError("a boundary continuation needs a non-empty act_key")
        if key in continuation_keys:
            raise ContractError(f"a boundary continuation repeats act_key {key!r}")
        continuation_keys.add(key)
        if (
            not isinstance(near, int)
            or isinstance(near, bool)
            or not isinstance(far, int)
            or isinstance(far, bool)
        ):
            raise ContractError("a boundary continuation needs integer page ordinals")
        if near not in owner or far not in owner:
            raise ContractError("a boundary continuation names a page outside the submitted shards")
        near_frame, far_frame = owner[near], owner[far]
        if near_frame == far_frame:
            continue
        output[near_frame].append(
            {
                "kind": CONTINUATION_HOLD,
                "act_key": key,
                "page_ordinal": near,
                "continuation_page_ordinal": far,
                "own_membership_digest": near_frame,
                "other_membership_digest": far_frame,
            }
        )
        output[far_frame].append(
            {
                "kind": CONTINUATION_HOLD,
                "act_key": key,
                "page_ordinal": far,
                "continuation_page_ordinal": near,
                "own_membership_digest": far_frame,
                "other_membership_digest": near_frame,
            }
        )

    cluster_ids: set[str] = set()
    for cluster in re_shoot_clusters:
        if not isinstance(cluster, Mapping) or set(cluster) != {
            "cluster_id",
            "member_page_ordinals",
        }:
            raise ContractError("a re-shoot cluster needs cluster_id and member_page_ordinals")
        cluster_id, pages = cluster["cluster_id"], cluster["member_page_ordinals"]
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ContractError("a re-shoot cluster needs a non-empty cluster_id")
        if cluster_id in cluster_ids:
            raise ContractError(f"a boundary re-shoot cluster repeats cluster_id {cluster_id!r}")
        cluster_ids.add(cluster_id)
        if (
            not isinstance(pages, list)
            or len(pages) < 2
            or any(
                not isinstance(page, int) or isinstance(page, bool) or page < 1 for page in pages
            )
        ):
            raise ContractError(
                "re-shoot cluster member_page_ordinals must be positive integers and plural"
            )
        if pages != sorted(set(pages)):
            raise ContractError(
                "re-shoot cluster member_page_ordinals must be sorted, unique, and plural"
            )
        if any(page not in owner for page in pages):
            raise ContractError("a re-shoot cluster names a page outside the submitted shards")
        frames = sorted({owner[page] for page in pages})
        if len(frames) < 2:
            continue
        for frame in frames:
            output[frame].append(
                {
                    "kind": SPLIT_RESHOOT_CLUSTER,
                    "cluster_id": cluster_id,
                    # Each returned record is independently mutable until its caller
                    # seals it. Sharing the caller's list here made an edit to one
                    # shard's record silently rewrite its reciprocal records too.
                    "member_page_ordinals": list(pages),
                    "own_membership_digest": frame,
                    "other_membership_digests": [other for other in frames if other != frame],
                }
            )

    for records in output.values():
        records.sort(
            key=lambda record: (record["kind"], record.get("act_key", record.get("cluster_id", "")))
        )
    return output
