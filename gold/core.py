"""Custodial records for the human-made gold corpus.

Gold selection is intentionally page-only.  These records neither receive witness
output nor express a preference among it; they select pages for people to annotate.
Every record is closed, self-hashed, and append-only so a later measurement can
identify exactly the human sample it used.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    is_sha256,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import ContractError, SchemaRefusal
from common.contracts.identities import is_well_formed

SAMPLE_SCHEMA = "gold-page-sample.v1"
MANUAL_PICK_SCHEMA = "gold-manual-pick.v1"
LAYOUT_SCHEMA = "gold-page-layout.v1"
PADDING_SCHEMA = "gold-padding-rectangles.v1"
MEASUREMENT_SCHEMA = "gold-instrument-membership.v1"
SETS = frozenset({"calibration", "locked-acceptance"})
REGION_KINDS = frozenset({"act", "non-act-text", "occlusion", "true-blank"})


def _refuse(condition: bool, message: str) -> None:
    if condition:
        raise SchemaRefusal(message)


def _sha(value: Any, field: str) -> str:
    _refuse(not is_sha256(value), f"{field} is not a lowercase sha256 digest")
    return value


def _read_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal(f"{path} is not readable JSON") from error


def _frame_from_run(run: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    _refuse(not isinstance(run, dict), "run authority is not an object")
    pages = run.get("source_manifest")
    membership = run.get("corpus_frame_membership")
    _refuse(not isinstance(pages, list), "run authority has no source_manifest")
    _refuse(
        not isinstance(membership, dict)
        or set(membership) != {"frame_digest", "page_digest", "seed"},
        "run corpus_frame_membership is not the closed R0 frame record",
    )
    for field in membership:
        _sha(membership[field], f"corpus_frame_membership.{field}")
    source = []
    for page in pages:
        _refuse(not isinstance(page, dict), "a source page is not an object")
        ordinal, page_sha = page.get("ordinal"), page.get("sha256")
        _refuse(
            not isinstance(ordinal, int) or isinstance(ordinal, bool),
            "source page ordinal is not an integer",
        )
        _sha(page_sha, "source page sha256")
        source.append({"ordinal": ordinal, "sha256": page_sha})
    source.sort(key=lambda page: page["ordinal"])
    _refuse(len({page["ordinal"] for page in source}) != len(source), "source page ordinals repeat")
    page_digest = digest_bytes(canonical_bytes(source))
    frame_digest = digest_bytes(canonical_bytes({"pages": source}))
    _refuse(
        membership["page_digest"] != page_digest,
        "R0 frame page_digest diverges from run source_manifest",
    )
    _refuse(
        membership["frame_digest"] != frame_digest,
        "R0 frame frame_digest diverges from run source_manifest",
    )
    return dict(membership), source


def load_run_frame(path: str | Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Read the source authority once and reject a forged derived frame record."""
    return _frame_from_run(_read_json(path))


def set_for_page(frame: dict[str, str], page_sha256: str) -> str:
    """A seed-driven partition: a page has exactly one possible gold set."""
    _sha(page_sha256, "page sha256")
    _sha(frame.get("seed"), "corpus frame seed")
    rank = digest_bytes(
        canonical_bytes({"seed": frame["seed"], "page_sha256": page_sha256, "purpose": "gold-set"})
    )
    return "calibration" if int(rank[0], 16) < 8 else "locked-acceptance"


def _rank(frame: dict[str, str], page_sha256: str, stratum: str) -> str:
    return digest_bytes(
        canonical_bytes(
            {
                "seed": frame["seed"],
                "page_sha256": page_sha256,
                "stratum": stratum,
                "purpose": "gold-sample",
            }
        )
    )


def _catalog(rows: Any, source: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _refuse(not isinstance(rows, list), "catalog must be a JSON list")
    expected = {(page["ordinal"], page["sha256"]) for page in source}
    found: set[tuple[int, str]] = set()
    result = []
    for row in rows:
        _refuse(
            not isinstance(row, dict) or set(row) != {"ordinal", "sha256", "stratum"},
            "catalog row has the wrong closed schema",
        )
        ordinal, page_sha, stratum = row["ordinal"], row["sha256"], row["stratum"]
        _refuse(
            not isinstance(ordinal, int) or isinstance(ordinal, bool),
            "catalog ordinal is not an integer",
        )
        _sha(page_sha, "catalog sha256")
        _refuse(not isinstance(stratum, str) or not stratum.strip(), "catalog stratum is empty")
        found.add((ordinal, page_sha))
        result.append(dict(row))
    _refuse(
        found != expected or len(found) != len(rows),
        "catalog does not cover each sealed source page exactly once",
    )
    return sorted(result, key=lambda row: (row["stratum"], row["ordinal"]))


def build_sample(
    frame: dict[str, str], page: dict[str, Any], *, selection_basis: str, method: str
) -> dict[str, Any]:
    """Build the source-derived restatement once; validation replays it exactly."""
    _refuse(method not in {"stratified-seed", "manual"}, "sample method is not recognized")
    _refuse(
        not isinstance(selection_basis, str) or not selection_basis.strip(),
        "selection_basis is empty",
    )
    page_sha = page["sha256"]
    gold_set = set_for_page(frame, page_sha)
    record = {
        "schema": SAMPLE_SCHEMA,
        "method": method,
        "selection_basis": selection_basis,
        "frame": dict(frame),
        "page": {"ordinal": page["ordinal"], "sha256": page_sha, "stratum": page["stratum"]},
        "set": gold_set,
    }
    record["sample_digest"] = digest_bytes(canonical_bytes(record))
    record["self_hash"] = self_hash(record)
    return record


def sample_stratified(run_path: str | Path, catalog_rows: Any, plan: Any) -> list[dict[str, Any]]:
    """Select quota pages by seed ranking only after the structural set partition."""
    frame, source = load_run_frame(run_path)
    catalog = _catalog(catalog_rows, source)
    _refuse(
        not isinstance(plan, dict) or set(plan) != SETS,
        "sampling plan must contain exactly both gold sets",
    )
    selected = []
    for gold_set in sorted(SETS):
        quotas = plan[gold_set]
        _refuse(not isinstance(quotas, dict) or not quotas, f"{gold_set} plan is empty")
        for stratum, quota in quotas.items():
            _refuse(not isinstance(stratum, str) or not stratum, "plan stratum is empty")
            _refuse(
                not isinstance(quota, int) or isinstance(quota, bool) or quota < 1,
                "sample quota is not a positive integer",
            )
            eligible = [
                page
                for page in catalog
                if page["stratum"] == stratum and set_for_page(frame, page["sha256"]) == gold_set
            ]
            eligible.sort(key=lambda page: _rank(frame, page["sha256"], stratum))
            _refuse(
                len(eligible) < quota,
                f"{gold_set}/{stratum} has {len(eligible)} structurally eligible pages for quota {quota}",
            )
            selected.extend(
                build_sample(
                    frame, page, selection_basis="seeded-stratified-v1", method="stratified-seed"
                )
                for page in eligible[:quota]
            )
    return selected


def ingest_manual_pick(run_path: str | Path, pick: Any) -> dict[str, Any]:
    """Record Tyrel's choice without selecting or replacing it."""
    frame, source = load_run_frame(run_path)
    _refuse(
        not isinstance(pick, dict) or set(pick) != {"schema", "selection_basis", "page", "set"},
        "manual pick has the wrong closed schema",
    )
    _refuse(pick["schema"] != MANUAL_PICK_SCHEMA, "manual pick schema is not recognized")
    page = pick["page"]
    _refuse(
        not isinstance(page, dict) or set(page) != {"ordinal", "sha256", "stratum"},
        "manual pick page has the wrong closed schema",
    )
    _refuse(
        not isinstance(page["ordinal"], int) or isinstance(page["ordinal"], bool),
        "manual pick page ordinal is not an integer",
    )
    _sha(page["sha256"], "manual pick page sha256")
    _refuse(
        not isinstance(page["stratum"], str) or not page["stratum"].strip(),
        "manual pick stratum is empty",
    )
    _refuse(
        not isinstance(pick["selection_basis"], str) or not pick["selection_basis"].strip(),
        "manual pick selection_basis is empty",
    )
    _refuse(
        (page.get("ordinal"), page.get("sha256"))
        not in {(p["ordinal"], p["sha256"]) for p in source},
        "manual pick page is outside the sealed corpus frame",
    )
    _refuse(pick["set"] not in SETS, "manual pick set is not recognized")
    expected_set = set_for_page(frame, page["sha256"])
    _refuse(
        pick["set"] != expected_set,
        "manual pick set conflicts with the seed-derived disjoint partition",
    )
    sample = build_sample(frame, page, selection_basis=pick["selection_basis"], method="manual")
    return validate_sample(sample, run_path)


def bind_instrument(sample: Any, act_identity: str, protocol_digest: str) -> dict[str, Any]:
    """Append a measurement binding; the source sample remains immutable."""
    validate_sample(sample)
    _refuse(
        not is_well_formed(act_identity) or not act_identity.startswith("act_"),
        "instrument act_identity is not an act identity",
    )
    _sha(protocol_digest, "instrument protocol_digest")
    record = {
        "schema": MEASUREMENT_SCHEMA,
        "sample_digest": sample["sample_digest"],
        "act_identity": act_identity,
        "protocol_digest": protocol_digest,
    }
    record["self_hash"] = self_hash(record)
    return record


def _rectangle(value: Any, label: str) -> None:
    _refuse(
        not isinstance(value, dict) or set(value) != {"x", "y", "w", "h"},
        f"{label} is not a closed rectangle",
    )
    for field in ("x", "y", "w", "h"):
        number = value[field]
        _refuse(
            not isinstance(number, int) or isinstance(number, bool) or number < 0,
            f"{label}.{field} is not a non-negative integer",
        )
    _refuse(value["w"] == 0 or value["h"] == 0, f"{label} has zero area")


def _validate_page_bound_record(record: Any, schema: str, extra: set[str]) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict) or set(record) != {"schema", "sample", *extra, "self_hash"},
        "gold record has the wrong closed schema",
    )
    _refuse(record["schema"] != schema, "gold record schema is not recognized")
    validate_sample(record["sample"])
    _refuse(not verify_self_hash(record), "gold record fails its self-hash")
    return record


def validate_sample(record: Any, run_path: str | Path | None = None) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict)
        or set(record)
        != {
            "schema",
            "method",
            "selection_basis",
            "frame",
            "page",
            "set",
            "sample_digest",
            "self_hash",
        },
        "sample has the wrong closed schema",
    )
    _refuse(record["schema"] != SAMPLE_SCHEMA, "sample schema is not recognized")
    _refuse(
        record["method"] not in {"stratified-seed", "manual"}, "sample method is not recognized"
    )
    _refuse(
        not isinstance(record["selection_basis"], str) or not record["selection_basis"].strip(),
        "sample selection_basis is empty",
    )
    frame = record["frame"]
    _refuse(
        not isinstance(frame, dict) or set(frame) != {"frame_digest", "page_digest", "seed"},
        "sample frame has the wrong closed schema",
    )
    for field in frame:
        _sha(frame[field], f"sample frame {field}")
    page = record["page"]
    _refuse(
        not isinstance(page, dict) or set(page) != {"ordinal", "sha256", "stratum"},
        "sample page has the wrong closed schema",
    )
    _refuse(
        not isinstance(page["ordinal"], int) or isinstance(page["ordinal"], bool),
        "sample page ordinal is not an integer",
    )
    _sha(page["sha256"], "sample page sha256")
    _refuse(
        not isinstance(page["stratum"], str) or not page["stratum"].strip(),
        "sample stratum is empty",
    )
    _refuse(
        record["set"] not in SETS or record["set"] != set_for_page(frame, page["sha256"]),
        "sample set conflicts with the seed-derived partition",
    )
    without = {
        key: value for key, value in record.items() if key not in {"sample_digest", "self_hash"}
    }
    _refuse(
        record["sample_digest"] != digest_bytes(canonical_bytes(without)),
        "sample_digest does not bind the sample record",
    )
    _refuse(not verify_self_hash(record), "sample fails its self-hash")
    if run_path is not None:
        run_frame, source = load_run_frame(run_path)
        _refuse(frame != run_frame, "sample frame diverges from the R0 run authority")
        _refuse(
            (page["ordinal"], page["sha256"]) not in {(p["ordinal"], p["sha256"]) for p in source},
            "sample page is outside the R0 run authority",
        )
    return record


def validate_layout(record: Any) -> dict[str, Any]:
    result = _validate_page_bound_record(record, LAYOUT_SCHEMA, {"regions"})
    regions = result["regions"]
    _refuse(not isinstance(regions, list), "layout regions is not a list")
    for region in regions:
        _refuse(
            not isinstance(region, dict) or set(region) != {"kind", "rect"},
            "layout region has the wrong closed schema",
        )
        _refuse(region["kind"] not in REGION_KINDS, "layout region kind is not recognized")
        _rectangle(region["rect"], "layout region rect")
    return result


def validate_padding(record: Any) -> dict[str, Any]:
    result = _validate_page_bound_record(
        record, PADDING_SCHEMA, {"rectangles", "calibrated_for_this_corpus"}
    )
    _refuse(
        not isinstance(result["calibrated_for_this_corpus"], bool),
        "padding calibration flag is not boolean",
    )
    _refuse(not isinstance(result["rectangles"], list), "padding rectangles is not a list")
    for rectangle in result["rectangles"]:
        _rectangle(rectangle, "padding rectangle")
    return result


def validate_measurement(record: Any) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict)
        or set(record)
        != {"schema", "sample_digest", "act_identity", "protocol_digest", "self_hash"},
        "instrument membership has the wrong closed schema",
    )
    _refuse(
        record["schema"] != MEASUREMENT_SCHEMA, "instrument membership schema is not recognized"
    )
    _sha(record["sample_digest"], "sample_digest")
    _refuse(
        not is_well_formed(record["act_identity"]) or not record["act_identity"].startswith("act_"),
        "instrument act_identity is not an act identity",
    )
    _sha(record["protocol_digest"], "instrument protocol_digest")
    _refuse(not verify_self_hash(record), "instrument membership fails its self-hash")
    return record


def write_append_only(path: str | Path, record: dict[str, Any]) -> Path:
    """Atomically create a record; any existing path is a named refusal."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(record) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".gold-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    try:
        # `link` is create-if-absent: a reader sees either no record or completed
        # immutable bytes, never a partially written target.
        os.link(temporary, target)
    except FileExistsError as error:
        raise ContractError(f"gold artifact already exists: {target}") from error
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
