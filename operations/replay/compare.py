"""What a replay changed: its records set beside its source run's, and the holds of each.

    python operations/replay/compare.py <source run directory> <replay run directory>

Two runs never carry byte-identical records, since every record names its own run
and the records it cites by their digests. So a record is compared with its run id,
its self-hash and the digests of the run's own records it cites left out (a stage
seal's inventory of them too), and with everything else exactly: payload, outcome,
attempt, every input path and every blob. A `decode-environment` names the machine
that ran the stage, so a replay on another machine differs there and nowhere else.

The holds are read from each run's Recensor reviews: the pages held, the pages whose
reading was thrown away whole (`page-unread`), and every hold code by units and
pages. The numbers are the runs' own; nothing here judges them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.contracts.stages import (  # noqa: E402
    CONIECTOR,
    PERLECTOR,
    RECENSOR,
    writing_directory,
)
from common.replay import IMPORTED_STAGES  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402

COMPARED_STAGES = (PERLECTOR, CONIECTOR, RECENSOR)
# Records that describe the pass itself rather than what it derived.
_PASS_KINDS = ("decode-environment", "stage-seal")
_IMPORTED_DIRECTORIES = frozenset(writing_directory(stage) for stage in IMPORTED_STAGES)


def _own_record(path: str) -> bool:
    """Whether a referenced path is one of the run's own records, which name its run."""
    return path.endswith(".json") and path.split("/", 1)[0] not in _IMPORTED_DIRECTORIES


def normalized(value: Any) -> Any:
    """`value` with the digest of every reference to the run's own records left out."""
    if isinstance(value, dict):
        if set(value) == {"relative_path", "sha256"} and _own_record(str(value["relative_path"])):
            return {"relative_path": value["relative_path"]}
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def _records(tree: RunTree, stage: str) -> dict[str, dict[str, Any]]:
    manifest = tree.build_manifest(stage, verify_inputs=False)
    return {
        entry["artifact_id"]: tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
        for entry in manifest["artifacts"]
    }


# A stage seal's digests of the run's own records, which name the run.
_SEAL_OWN_DIGESTS = ("artifact_inventory", "decode_environment_sha256")


def _comparable(record: dict[str, Any]) -> Any:
    kept = {k: v for k, v in record.items() if k not in ("run_id", "self_hash")}
    if kept.get("kind") == "stage-seal" and isinstance(kept.get("payload"), dict):
        kept["payload"] = {k: v for k, v in kept["payload"].items() if k not in _SEAL_OWN_DIGESTS}
    return normalized(kept)


def compare_records(source: RunTree, replay: RunTree) -> dict[str, Any]:
    """Per stage and kind: records in both and equal, in both and different, or in one only."""
    result: dict[str, Any] = {}
    for stage in COMPARED_STAGES:
        before, after = _records(source, stage), _records(replay, stage)
        kinds: dict[str, Counter] = defaultdict(Counter)
        differing: list[str] = []
        for identifier in sorted(set(before) | set(after)):
            record = after.get(identifier) or before[identifier]
            kind = record["kind"]
            if identifier not in after:
                kinds[kind]["source-only"] += 1
            elif identifier not in before:
                kinds[kind]["replay-only"] += 1
            elif _comparable(before[identifier]) == _comparable(after[identifier]):
                kinds[kind]["equal"] += 1
            else:
                kinds[kind]["different"] += 1
                if kind not in _PASS_KINDS:
                    differing.append(f"{kind}/{identifier}")
        blobs = {
            name: {blob["name"] for blob in _blob_names(tree, stage)}
            for name, tree in (("source", source), ("replay", replay))
        }
        result[stage] = {
            "kinds": {kind: dict(counts) for kind, counts in sorted(kinds.items())},
            "differing": differing,
            "blobs": {
                "equal": len(blobs["source"] & blobs["replay"]),
                "source-only": len(blobs["source"] - blobs["replay"]),
                "replay-only": len(blobs["replay"] - blobs["source"]),
            },
        }
    return result


def _blob_names(tree: RunTree, stage: str) -> list[dict[str, str]]:
    directory = tree.root / writing_directory(stage) / "blobs" / "sha256"
    if not directory.exists():
        return []
    return [{"name": path.name} for path in sorted(directory.iterdir()) if path.is_file()]


def holds(tree: RunTree) -> dict[str, Any]:
    """The Recensor's holds of one run: pages held, pages unread, and each code's reach."""
    reviews = [
        record["payload"]
        for record in _records(tree, RECENSOR).values()
        if record["kind"] == "review"
    ]
    pages = {review["page_ordinal"] for review in reviews}
    held = {review["page_ordinal"] for review in reviews if review["hold_codes"]}
    # Review flags (`flag_codes`, a review of this code or later): recorded, not held.
    flagged = {review["page_ordinal"] for review in reviews if review.get("flag_codes")} - held
    units: Counter = Counter()
    flag_units: Counter = Counter()
    reach: dict[str, set[int]] = defaultdict(set)
    flag_reach: dict[str, set[int]] = defaultdict(set)
    for review in reviews:
        for code in review["hold_codes"]:
            units[code] += 1
            reach[code].add(review["page_ordinal"])
        for code in review.get("flag_codes", ()):
            flag_units[code] += 1
            flag_reach[code].add(review["page_ordinal"])
    return {
        "pages": len(pages),
        "units": len(reviews),
        "held_units": sum(1 for review in reviews if review["hold_codes"]),
        "held_pages": len(held),
        "flagged_pages": sorted(flagged),
        "flagged_units": sum(
            1 for review in reviews if review.get("flag_codes") and not review["hold_codes"]
        ),
        "clean_pages": sorted(pages - held - flagged),
        "page_unread_pages": len(reach.get("page-unread", ())),
        "codes": {
            code: {"units": count, "pages": len(reach[code])} for code, count in units.most_common()
        },
        "flags": {
            code: {"units": count, "pages": len(flag_reach[code])}
            for code, count in flag_units.most_common()
        },
    }


def _tree(directory: Path) -> RunTree:
    directory = directory.absolute()
    return RunTree(directory.parent, directory.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("replay", type=Path)
    parser.add_argument("--json", type=Path, default=None, help="also write the result here")
    args = parser.parse_args(argv)
    source, replay = _tree(args.source), _tree(args.replay)
    result = {
        "source": source.run_id,
        "replay": replay.run_id,
        "records": compare_records(source, replay),
        "holds": {"source": holds(source), "replay": holds(replay)},
    }
    if args.json is not None:
        args.json.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
    for stage, found in result["records"].items():
        print(f"{stage}: {json.dumps(found['kinds'], sort_keys=True)}; blobs {found['blobs']}")
    for name in ("source", "replay"):
        summary = result["holds"][name]
        print(
            f"{name} {result[name]}: {summary['held_pages']} of {summary['pages']} pages held, "
            f"{summary['page_unread_pages']} unread, {summary['held_units']} of "
            f"{summary['units']} units held, {len(summary['flagged_pages'])} pages flagged only, "
            f"clean pages {summary['clean_pages']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
