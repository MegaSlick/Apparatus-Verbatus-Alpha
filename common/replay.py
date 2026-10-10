"""A replay: a new run that reads a saved run's recorded model replies with the current code.

A change to how the Perlector's answers are accounted can only be proven by a new
reading, and a new reading of a saved run would otherwise need the model again. A
replay run makes that reading without any model: it takes the stages before the
Perlector from its source run byte for byte, and answers every Perlector and Coniector
call from the reply the source run retained for exactly that request. Everything from
the Perlector on is derived again by the code that runs it, under a new run id, and
nothing in the source run is written.

The replay is named in the new run's `run.json` (`replay`, `REPLAY_SCHEMA`):

    {schema, source_run_id, source_run_sha256, source_repository_commit,
     imported_stages, replies: "recorded"}

`source_run_sha256` is the source authority's self-hash, so a replay can be fed only
by the run it names; `run.json`'s own `repository_commit` is the code that replayed.
Records of an imported stage keep the source's run id, and the run tree accepts that
id for those stages alone (`RunTree.holds_run_id`).

A request is answered only when the source run sent exactly those bytes. A first
reading the current code would ask differently is refused, since its page could not
be read at all; a re-ask or a reconstruction call the source never sent is recorded
as not asked, `not-replayed`, and holds or leaves unmade what it would have read.
The stages read the source run from `SOURCE_ENV`, which `operations/replay` sets.
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import is_sha256
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.stages import (
    ATTESTATORES,
    CONIECTOR,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
)

REPLAY_FIELD: Final = "replay"
REPLAY_SCHEMA: Final = "run-replay.v1"
# The stages a replay takes from its source as they were sealed; it never runs them.
IMPORTED_STAGES: Final = (DOOR, EXEMPLAR, INK_MAP, DESIGNATOR, ATTESTATORES)
# Where the source run's directory is named to the stages that answer from it.
SOURCE_ENV: Final = "VERBATUS_REPLAY_SOURCE"
# Why a re-ask or a reconstruction call of a replay was not asked.
NOT_REPLAYED: Final = "not-replayed"
RECORDED: Final = "recorded"

_FIELDS: Final = frozenset(
    {
        "schema",
        "source_run_id",
        "source_run_sha256",
        "source_repository_commit",
        "imported_stages",
        "replies",
    }
)
# The record each replayed stage keeps a call's `engine_call` on.
_CALL_KINDS: Final = {PERLECTOR: "page-reading", CONIECTOR: "reconstruction-call"}


def replay_block(
    source_run: Mapping[str, Any],
) -> dict[str, Any]:
    """The `replay` block of a run that replays `source_run`, the source's authority."""
    commit = source_run.get("repository_commit")
    block = {
        "schema": REPLAY_SCHEMA,
        "source_run_id": source_run.get("run_id"),
        "source_run_sha256": source_run.get("self_hash"),
        "source_repository_commit": commit if isinstance(commit, str) else None,
        "imported_stages": list(IMPORTED_STAGES),
        "replies": RECORDED,
    }
    validate_replay_block(block)
    return block


def validate_replay_block(block: Any) -> dict[str, Any]:
    """Refuse a `replay` block that is not exactly this schema."""
    if (
        not isinstance(block, Mapping)
        or set(block) != _FIELDS
        or block["schema"] != REPLAY_SCHEMA
        or not isinstance(block["source_run_id"], str)
        or not block["source_run_id"]
        or not is_sha256(block["source_run_sha256"])
        or not (
            block["source_repository_commit"] is None
            or isinstance(block["source_repository_commit"], str)
        )
        or list(block["imported_stages"]) != list(IMPORTED_STAGES)
        or block["replies"] != RECORDED
    ):
        raise SchemaRefusal(f"run.json's {REPLAY_FIELD!r} is not a {REPLAY_SCHEMA} block")
    return dict(block)


def replay_of(run: Mapping[str, Any]) -> dict[str, Any] | None:
    """The run's `replay` block, checked, or `None` for a run that is no replay."""
    if REPLAY_FIELD not in run:
        return None
    return validate_replay_block(run[REPLAY_FIELD])


def run_holds(run: Mapping[str, Any], run_id: Any, stage: str) -> bool:
    """Whether a record of `stage` naming `run_id` belongs to the run `run` authorizes."""
    if run_id == run.get("run_id"):
        return True
    block = replay_of(run)
    return (
        block is not None
        and run_id == block["source_run_id"]
        and (stage in block["imported_stages"])
    )


def not_replayed_problem(run: Mapping[str, Any]) -> dict[str, str]:
    """The one problem a request the source run never sent is recorded with."""
    block = replay_of(run)
    if block is None:
        raise ContractError("only a replay run leaves a request not replayed")
    return {
        "code": NOT_REPLAYED,
        "detail": (
            f"this run replays run {block['source_run_id']} from its recorded replies, and that "
            "run never sent this request; a live reading would ask it"
        ),
    }


def refuse_imported_stage(run: Mapping[str, Any], stage: str) -> None:
    """A replay run never runs a stage it took from its source."""
    block = replay_of(run)
    if block is not None and stage in block["imported_stages"]:
        raise ContractError(
            f"this run replays run {block['source_run_id']}, and its {stage} records are that "
            "run's, imported as sealed; the stage is never run here. Replay from the Perlector"
        )


class Source:
    """The source run a replay answers from, opened read-only and indexed by request."""

    def __init__(self, tree, run: Mapping[str, Any]) -> None:
        self.tree = tree
        self.run = run
        self._readings: dict[tuple[str, int], dict[str, Any]] | None = None
        self._calls: dict[str, list[dict[str, Any]]] | None = None

    @property
    def run_id(self) -> str:
        return self.tree.run_id

    def _records(self, stage: str, kind: str) -> list[dict[str, Any]]:
        manifest = self.tree.build_manifest(stage, verify_inputs=False)
        return [
            self.tree.read_artifact(stage, kind, entry["artifact_id"])
            for entry in manifest["artifacts"]
            if entry["kind"] == kind
        ]

    def reading(self, page_id: str, attempt: int) -> dict[str, Any] | None:
        """The source's page-reading payload of one page at one attempt, if it has one."""
        if self._readings is None:
            self._readings = {
                (record["subject_id"], record["payload"]["attempt_ordinal"]): record["payload"]
                for record in self._records(PERLECTOR, _CALL_KINDS[PERLECTOR])
            }
        return self._readings.get((page_id, attempt))

    def answered_calls(self, page_id: str) -> list[dict[str, Any]]:
        """Every reconstruction-call payload the source asked for this page and got answered."""
        if self._calls is None:
            self._calls = {}
            for record in self._records(CONIECTOR, _CALL_KINDS[CONIECTOR]):
                if record["payload"].get("engine_call") is not None:
                    self._calls.setdefault(record["subject_id"], []).append(record["payload"])
        return list(self._calls.get(page_id, []))

    def engine_calls(self, stage: str) -> list[dict[str, Any]]:
        """Every `engine_call` the source's records of `stage` name: its answered calls."""
        return [
            record["payload"]["engine_call"]
            for record in self._records(stage, _CALL_KINDS[stage])
            if isinstance(record["payload"], Mapping)
            and record["payload"].get("engine_call") is not None
        ]

    def read(self, reference: Mapping[str, str]) -> bytes:
        """The bytes a source reference names, refused unless they still hash to it."""
        from common.contracts.envelope import read_verified

        return read_verified(self.tree.read_bytes, dict(reference), "a recorded reply")


def open_source(run: Mapping[str, Any]) -> Source:
    """The source run this replay run names, from `SOURCE_ENV`, refused unless it is that run."""
    block = replay_of(run)
    if block is None:
        raise ContractError("this run is no replay; it has no source run to answer from")
    named = os.environ.get(SOURCE_ENV)
    if not named:
        raise ContractError(
            f"this run replays run {block['source_run_id']} and answers only from its recorded "
            f"replies, but {SOURCE_ENV} names no source run directory; run it with "
            "operations/replay/replay.py, which names it"
        )
    return _opened(str(Path(named).absolute()), block["source_run_id"], block["source_run_sha256"])


@functools.lru_cache(maxsize=4)
def _opened(directory: str, run_id: str, sha256: str) -> Source:
    from common.runtree.store import RunTree

    path = Path(directory)
    if path.name != run_id:
        raise ContractError(
            f"{SOURCE_ENV} names {directory}, which is not run {run_id}'s directory; a replay "
            "answers only from the run it names"
        )
    tree = RunTree(path.parent, run_id)
    run = tree.read_run()
    if run.get("self_hash") != sha256:
        raise ContractError(
            f"the run at {directory} is not the run this replay names: its authority's "
            "self-hash differs, so its replies are not the ones the replay was made from"
        )
    return Source(tree, run)


def call_record(source: Source, engine_call: Mapping[str, Any]) -> tuple[dict, bytes, bytes]:
    """One recorded call: its call record, the record's bytes and the response's bytes."""
    record_bytes = source.read(engine_call["call_record_ref"])
    response_bytes = source.read(engine_call["raw_response_ref"])
    try:
        record = json.loads(record_bytes)
    except ValueError as error:
        raise ContractError(f"a recorded call record of run {source.run_id} is not JSON") from error
    if not isinstance(record, dict):
        raise ContractError(f"a recorded call record of run {source.run_id} is not an object")
    return record, record_bytes, response_bytes
