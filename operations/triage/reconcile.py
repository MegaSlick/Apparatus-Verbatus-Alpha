"""Deterministic reconciliation of independent structural verdict files."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from common.contracts.canonical import canonical_bytes, digest_of, is_sha256
from common.contracts.errors import SchemaRefusal
from common.corpus_register import refuse_capture_preference

VERDICT_SCHEMA: Final = "triage-structural-verdict.v1"
EXPECTED_SCHEMA: Final = "triage-structural-expected.v2"
DISAGREEMENTS_SCHEMA: Final = "triage-structural-disagreements.v2"
_MAX_VERDICT_BYTES: Final = 16 * 1024 * 1024
_MAX_VERDICTS: Final = 32
_MAX_FACTS_PER_VERDICT: Final = 10_000
_MAX_OBSERVATIONS_PER_VERDICT: Final = 100_000
_MAX_TEXT_CHARS: Final = 4096


class ReconciliationRefusal(SchemaRefusal):
    """A verdict is not in the closed, replayable structural vocabulary."""


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT_CHARS:
        raise ReconciliationRefusal(
            f"{what} must be a non-blank string of at most {_MAX_TEXT_CHARS} characters"
        )
    return value


def _box(value: Any, act_id: str) -> dict[str, int]:
    """One act's rectangle, in per-mille of the frame it was seen on.

    Per-mille integers, not fractions: `canonical_bytes` refuses floats, and a
    reader asked for "box fractions" returns 0.31 unless the unit is named. The
    seat sheet in `measured/README.md` states this encoding, because the cost of
    discovering the mismatch at reconcile time is a second disclosure-bearing
    round of external vision calls.

    A rectangle outside [0, 1000] is refused rather than clamped: a box beyond
    the frame edge is a reader that mislocated the act, and silently squaring it
    up would assert a geometry no seat reported.
    """
    if not isinstance(value, dict) or set(value) != {"x0", "y0", "x1", "y1"}:
        raise ReconciliationRefusal(
            f"structural box for {act_id!r} must be a closed x0/y0/x1/y1 rectangle"
        )
    if not all(
        isinstance(value[key], int) and not isinstance(value[key], bool)
        for key in ("x0", "y0", "x1", "y1")
    ):
        raise ReconciliationRefusal(
            f"structural box for {act_id!r} must be per-mille integers, never fractions"
        )
    if not (0 <= value["x0"] < value["x1"] <= 1000 and 0 <= value["y0"] < value["y1"] <= 1000):
        raise ReconciliationRefusal(
            f"structural box for {act_id!r} lies outside the frame it was read on, or is "
            "degenerate; per-mille coordinates run 0 to 1000 with x0 < x1 and y0 < y1"
        )
    return value


def validate_verdict(value: Any) -> dict[str, Any]:
    fields = {"schema", "seat", "numeric_tolerance", "box_tolerance_permille", "facts"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != VERDICT_SCHEMA:
        raise ReconciliationRefusal("structural verdict has the wrong closed schema")
    seat = value["seat"]
    if not isinstance(seat, dict) or set(seat) != {"identity", "revision"}:
        raise ReconciliationRefusal("structural verdict seat must name identity and revision")
    _text(seat["identity"], "verdict seat identity")
    _text(seat["revision"], "verdict seat revision")
    for field in ("numeric_tolerance", "box_tolerance_permille"):
        tolerance = value[field]
        if (
            not isinstance(tolerance, int)
            or isinstance(tolerance, bool)
            or not 0 <= tolerance <= 1000
        ):
            raise ReconciliationRefusal(
                f"structural verdict {field} must be a per-mille integer from 0 through 1000"
            )
    if (
        not isinstance(value["facts"], dict)
        or not value["facts"]
        or len(value["facts"]) > _MAX_FACTS_PER_VERDICT
    ):
        raise ReconciliationRefusal("structural verdict facts must be a non-empty mapping")
    observation_count = 0
    for fact_id, fact in value["facts"].items():
        _text(fact_id, "structural fact id")
        if not isinstance(fact, dict) or set(fact) != {"categorical", "numeric", "acts", "boxes"}:
            raise ReconciliationRefusal(
                "structural fact must be closed categorical/numeric/acts/boxes"
            )
        if not isinstance(fact["categorical"], dict) or not all(
            isinstance(key, str)
            and 0 < len(key) <= _MAX_TEXT_CHARS
            and isinstance(item, str)
            and 0 < len(item) <= _MAX_TEXT_CHARS
            for key, item in fact["categorical"].items()
        ):
            raise ReconciliationRefusal("structural categorical facts must be non-blank strings")
        face = fact["categorical"].get("loose_document_face")
        if face is not None and face not in {
            "written-side-up",
            "written-side-down",
            "indeterminate",
            "none",
            # The first measured pass (2026-08-22) predates this closure and
            # recorded seats answering in two open vocabularies ("up"/"down",
            # "recto"/"verso"); its face disagreement is vocabulary-induced and
            # stands as recorded. From here the enum is closed so unanimity on
            # this fact is decidable by observation, not by dialect.
        }:
            raise ReconciliationRefusal(
                "loose_document_face must be one of written-side-up, written-side-down, "
                "indeterminate, none"
            )
        if not isinstance(fact["numeric"], dict) or not all(
            isinstance(key, str)
            and 0 < len(key) <= _MAX_TEXT_CHARS
            and isinstance(item, int)
            and not isinstance(item, bool)
            and 0 <= item <= 1000
            for key, item in fact["numeric"].items()
        ):
            raise ReconciliationRefusal(
                "structural numeric facts must be per-mille integers from 0 through 1000"
            )
        acts = fact["acts"]
        if (
            not isinstance(acts, list)
            or len(acts) > _MAX_OBSERVATIONS_PER_VERDICT
            or acts != [f"act-{index:03d}" for index in range(1, len(acts) + 1)]
        ):
            raise ReconciliationRefusal(
                "structural acts must be contiguous positional identifiers act-001, act-002, "
                "and so on in the seat prompt's declared reading order"
            )
        if not isinstance(fact["boxes"], dict):
            raise ReconciliationRefusal("structural boxes must be a mapping from act to rectangle")
        observation_count += (
            len(fact["categorical"]) + len(fact["numeric"]) + len(acts) + len(fact["boxes"])
        )
        if observation_count > _MAX_OBSERVATIONS_PER_VERDICT:
            raise ReconciliationRefusal("structural verdict exceeds the bounded observation count")
        for act_id, box in fact["boxes"].items():
            # The act enumeration is the coverage denominator. A box for an act the
            # seat did not enumerate would be geometry outside that denominator, so
            # the enumeration is what carries it, not the other way round.
            if act_id not in fact["acts"]:
                raise ReconciliationRefusal(
                    f"structural box names {act_id!r}, which this seat did not enumerate as an act"
                )
            _box(box, act_id)
    refuse_capture_preference(value)
    try:
        encoded = canonical_bytes(value)
    except (TypeError, ValueError) as error:
        raise ReconciliationRefusal(
            "structural verdict cannot be represented as canonical JSON"
        ) from error
    if len(encoded) > _MAX_VERDICT_BYTES:
        raise ReconciliationRefusal(
            f"structural verdict exceeds the {_MAX_VERDICT_BYTES}-byte intake limit"
        )
    return value


def reconcile(verdicts: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Unanimously assert categoricals, interval numerics and boxes, union act coverage."""
    if (
        not isinstance(verdicts, Sequence)
        or isinstance(verdicts, (str, bytes))
        or not 2 <= len(verdicts) <= _MAX_VERDICTS
    ):
        raise ReconciliationRefusal(
            f"reconciler requires between 2 and {_MAX_VERDICTS} independent verdicts"
        )
    try:
        checked = [validate_verdict(dict(verdict)) for verdict in verdicts]
    except (TypeError, ValueError) as error:
        raise ReconciliationRefusal(
            "structural verdict input is not a verdict mapping; nothing was reconciled"
        ) from error
    # Independence is keyed on the *resolved* seat — identity and revision together.
    # Two files from one identity at one revision are one reader counted twice, and
    # unanimity across them means nothing; that is refused outright rather than
    # deduplicated, because which of the two the pass meant to use is not this
    # function's call to make. Two revisions of one identity are two different
    # resolved models and are accepted as two seats — correlated, certainly, and the
    # `seats` list in both output documents spells that out verbatim for a reader
    # who wants to weigh it. A derived "these are related" flag would be a second
    # spelling of a fact already recorded.
    identities = [(item["seat"]["identity"], item["seat"]["revision"]) for item in checked]
    if len(set(identities)) != len(identities):
        raise ReconciliationRefusal("reconciler verdicts repeat a seat identity and revision")
    verdicts_sha256 = digest_of(checked)
    # An assertion has to fit every seat's declared tolerance, so the narrowest
    # declaration governs the shared numeric and geometric intervals.
    numeric_tolerance = min(item["numeric_tolerance"] for item in checked)
    box_tolerance = min(item["box_tolerance_permille"] for item in checked)
    all_fact_ids = sorted(set().union(*(set(item["facts"]) for item in checked)))
    expected_facts: dict[str, Any] = {}
    disagreements: dict[str, Any] = {}
    for fact_id in all_fact_ids:
        present = [item["facts"].get(fact_id) for item in checked]
        reported = [
            (item["seat"], fact)
            for item, fact in zip(checked, present, strict=True)
            if fact is not None
        ]
        facts = [fact for _seat, fact in reported]
        if len(reported) != len(checked):
            # The union of what *any* seat saw is the coverage denominator, and this
            # is the branch where it matters most: a frame only one seat reported on
            # is exactly where an act is likeliest to be lost. Recording only
            # "missing-fact" would drop that seat's whole enumeration, and GOALS 1
            # ranks a missed act above a poorly read one. Consensus gates what the
            # fixture asserts; it never decides what counts as present.
            disagreements[fact_id] = {
                "reason": "missing-fact",
                "act_coverage_denominator": sorted(
                    set().union(*(set(fact["acts"]) for _seat, fact in reported))
                )
                if reported
                else [],
                "reported_by": [seat for seat, _fact in reported],
                # A missing fact is itself a disagreement. Retaining only the seats'
                # names and act union would silently discard every categorical,
                # numeric, and geometric observation the reporting seats supplied.
                "per_seat": [{"seat": seat, "value": fact} for seat, fact in reported],
                "seats": [item["seat"] for item in checked],
            }
            continue
        union_acts = sorted(set().union(*(set(item["acts"]) for item in facts)))
        categorical: dict[str, str] = {}
        numeric: dict[str, list[int]] = {}
        failed: list[str] = []
        if any(item["acts"] != facts[0]["acts"] for item in facts[1:]):
            # The union remains the coverage denominator: disagreement may never
            # erase an act one seat saw. It is still disagreement, and omitting it
            # from this record would make the union look unanimous.
            failed.append("acts")
        categorical_keys = set().union(*(set(item["categorical"]) for item in facts))
        for key in sorted(categorical_keys):
            values = [item["categorical"].get(key) for item in facts]
            if None in values or len(set(values)) != 1:
                failed.append(f"categorical:{key}")
            else:
                categorical[key] = values[0]
        numeric_keys = set().union(*(set(item["numeric"]) for item in facts))
        for key in sorted(numeric_keys):
            values = [item["numeric"].get(key) for item in facts]
            if None in values or max(values) - min(values) > numeric_tolerance:
                failed.append(f"numeric:{key}")
            else:
                numeric[key] = [min(values), max(values)]
        # An act's rectangle is a numeric observation and follows the numeric rule:
        # every seat that reported this fact has to have localized it, and the spread
        # has to sit inside the smallest declared box tolerance. An act nobody boxed
        # stays in the denominator with no interval — enumerated, not located.
        boxes: dict[str, dict[str, list[int]]] = {}
        for act_id in union_acts:
            supplied = [item["boxes"].get(act_id) for item in facts]
            if all(box is None for box in supplied):
                continue
            if any(box is None for box in supplied) or any(
                max(box[key] for box in supplied) - min(box[key] for box in supplied)
                > box_tolerance
                for key in ("x0", "y0", "x1", "y1")
            ):
                failed.append(f"box:{act_id}")
                continue
            boxes[act_id] = {
                key: [min(box[key] for box in supplied), max(box[key] for box in supplied)]
                for key in ("x0", "y0", "x1", "y1")
            }
        if failed:
            # One failed sub-fact never discards the sub-facts the seats DID
            # agree on: unanimity is judged per fact, and the agreeing remainder
            # enters the expected record below exactly as if nothing beside it
            # had been asked. The disagreement carries every seat's own value
            # for each failed fact — the fact of disagreement, with its
            # evidence, never a resolution.
            def _fact_value(fact: Mapping[str, Any], label: str) -> Any:
                kind, _, name = label.partition(":")
                if kind == "categorical":
                    return fact["categorical"].get(name)
                if kind == "numeric":
                    return fact["numeric"].get(name)
                if kind == "acts":
                    return fact["acts"]
                return fact["boxes"].get(name)

            disagreements[fact_id] = {
                "reason": "not-unanimous-or-outside-tolerance",
                "failed": failed,
                "per_seat": {
                    label: [
                        {"seat": item["seat"], "value": _fact_value(fact, label)}
                        for item, fact in zip(checked, facts, strict=True)
                    ]
                    for label in failed
                },
                "act_coverage_denominator": union_acts,
                "seats": [item["seat"] for item in checked],
            }
        expected_facts[fact_id] = {
            "categorical": categorical,
            "numeric_intervals": numeric,
            "box_intervals_permille": boxes,
            "act_coverage_denominator": union_acts,
        }
    expected = {
        "schema": EXPECTED_SCHEMA,
        "verdicts_sha256": verdicts_sha256,
        "seats": [item["seat"] for item in checked],
        "facts": expected_facts,
    }
    disagreement = {
        "schema": DISAGREEMENTS_SCHEMA,
        "verdicts_sha256": verdicts_sha256,
        "seats": [item["seat"] for item in checked],
        "facts": disagreements,
    }
    reconciliation_sha256 = digest_of({"expected": expected, "disagreements": disagreement})
    expected["reconciliation_sha256"] = reconciliation_sha256
    disagreement["reconciliation_sha256"] = reconciliation_sha256
    validate_reconciliation_pair(expected, disagreement)
    refuse_capture_preference(expected)
    refuse_capture_preference(disagreement)
    return expected, disagreement


def validate_reconciliation_pair(expected: Any, disagreements: Any) -> None:
    """Verify that two derived documents are the complete pair their digest seals."""
    fields = {"schema", "verdicts_sha256", "seats", "facts", "reconciliation_sha256"}
    if (
        not isinstance(expected, dict)
        or not isinstance(disagreements, dict)
        or set(expected) != fields
        or set(disagreements) != fields
        or expected.get("schema") != EXPECTED_SCHEMA
        or disagreements.get("schema") != DISAGREEMENTS_SCHEMA
    ):
        raise ReconciliationRefusal(
            "structural reconciliation outputs have the wrong closed schemas"
        )
    expected_digest = expected.get("reconciliation_sha256")
    disagreement_digest = disagreements.get("reconciliation_sha256")
    if expected_digest != disagreement_digest or not is_sha256(expected_digest):
        raise ReconciliationRefusal("structural reconciliation outputs do not share one digest")
    if expected.get("verdicts_sha256") != disagreements.get("verdicts_sha256") or not is_sha256(
        expected.get("verdicts_sha256")
    ):
        raise ReconciliationRefusal("structural reconciliation outputs name different verdict sets")
    unsigned_expected = {
        key: value for key, value in expected.items() if key != "reconciliation_sha256"
    }
    unsigned_disagreements = {
        key: value for key, value in disagreements.items() if key != "reconciliation_sha256"
    }
    actual = digest_of({"expected": unsigned_expected, "disagreements": unsigned_disagreements})
    if actual != expected_digest:
        raise ReconciliationRefusal(
            "structural reconciliation output bytes do not match their shared digest"
        )


def reconcile_files(
    paths: Sequence[str | Path], expected_path: str | Path, disagreements_path: str | Path
) -> None:
    """Replay verdicts into distinct, atomically published derived documents."""
    try:
        source_paths = [Path(path) for path in paths]
        expected = Path(expected_path)
        disagreements = Path(disagreements_path)
    except TypeError as error:
        raise ReconciliationRefusal("structural reconciliation paths are malformed") from error
    source_paths, expected, disagreements = _canonical_distinct_paths(
        source_paths, expected, disagreements
    )
    try:
        verdicts = [json.loads(_read_verdict_bytes(path).decode("utf-8")) for path in source_paths]
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ReconciliationRefusal("structural verdict file could not be read") from error
    expected_value, disagreement_value = reconcile(verdicts)
    # Publish the disagreement record first. A failure before the expected record
    # lands cannot expose new positive assertions without their dissent document.
    _atomic_write(disagreements, canonical_bytes(disagreement_value))
    _atomic_write(expected, canonical_bytes(expected_value))


def _case_insensitive_key(path: Path) -> str:
    return unicodedata.normalize("NFC", os.fspath(path)).casefold()


def _canonical_distinct_paths(
    sources: Sequence[Path], expected: Path, disagreements: Path
) -> tuple[list[Path], Path, Path]:
    """Resolve parents once and refuse aliases before immutable sources are read."""
    labelled = [(f"source verdict {index}", path) for index, path in enumerate(sources)] + [
        ("expected output", expected),
        ("disagreements output", disagreements),
    ]
    canonical: list[tuple[str, Path]] = []
    spellings: dict[str, str] = {}
    identities: dict[tuple[int, int], str] = {}
    for role, path in labelled:
        try:
            target = path.parent.resolve(strict=False) / path.name
        except (OSError, RuntimeError) as error:
            raise ReconciliationRefusal(
                "structural reconciliation path could not be resolved; nothing was written. "
                "Repair the named path and retry."
            ) from error
        key = _case_insensitive_key(target)
        prior = spellings.get(key)
        if prior is not None:
            raise ReconciliationRefusal(
                f"structural reconciliation paths for {prior} and {role} resolve to one file "
                "or collide on a case-insensitive filesystem; "
                "nothing was written. Give each input and output its own path."
            )
        spellings[key] = role
        try:
            status = os.lstat(target)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ReconciliationRefusal(
                "structural reconciliation path identity could not be verified; nothing was "
                "written. Restore readable paths and retry."
            ) from error
        else:
            if stat.S_ISLNK(status.st_mode):
                raise ReconciliationRefusal(
                    f"structural reconciliation path for {role} is a symbolic link; nothing "
                    "was written. Supply a direct path."
                )
            if not stat.S_ISREG(status.st_mode):
                raise ReconciliationRefusal(
                    f"structural reconciliation path for {role} is not a regular file; "
                    "nothing was written. Supply a direct file path."
                )
            identity = (status.st_dev, status.st_ino)
            prior = identities.get(identity)
            if prior is not None:
                raise ReconciliationRefusal(
                    f"structural reconciliation paths for {prior} and {role} name one file; "
                    "nothing was written. Give each input and output its own path."
                )
            identities[identity] = role
        canonical.append((role, target))
    source_count = len(sources)
    return (
        [path for _role, path in canonical[:source_count]],
        canonical[source_count][1],
        canonical[source_count + 1][1],
    )


def _read_verdict_bytes(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise ReconciliationRefusal("structural verdict path is not a regular file")
        data = handle.read(_MAX_VERDICT_BYTES + 1)
    if len(data) > _MAX_VERDICT_BYTES:
        raise ReconciliationRefusal(
            f"structural verdict exceeds the {_MAX_VERDICT_BYTES}-byte intake limit"
        )
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace one derived document with complete durable bytes, never a torn file."""
    temporary: Path | None = None
    replaced = False
    failure: ReconciliationRefusal | None = None
    cause: OSError | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        state = "was replaced but is not proven durable" if replaced else "was not replaced"
        failure = ReconciliationRefusal(f"structural reconciliation output {path} {state}")
        cause = error
    cleanup_error = _temporary_cleanup_error(temporary)
    if failure is not None:
        if cleanup_error is not None:
            failure.add_note(
                f"structural reconciliation temporary {temporary} also could not be removed: "
                f"{cleanup_error}"
            )
        raise failure from cause
    if cleanup_error is not None:
        raise ReconciliationRefusal(
            f"structural reconciliation temporary {temporary} could not be removed"
        ) from cleanup_error


def _temporary_cleanup_error(path: Path | None) -> OSError | None:
    """Return cleanup failure so the reconciliation refusal remains primary."""
    if path is None:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return error
    return None
