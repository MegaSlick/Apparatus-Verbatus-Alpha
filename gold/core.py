"""Custodial records for the human-made gold corpus.

Gold selection is intentionally page-only.  These records neither receive witness
output nor express a preference among it; they select pages for people to annotate.
Every record is closed, self-hashed, and append-only so a later measurement can
identify exactly the human sample it used.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
import unicodedata
from pathlib import Path
from typing import Any

from common.contracts.canonical import (
    SCHEMA_LABEL,
    canonical_bytes,
    digest_bytes,
    is_sha256,
    self_hash,
    verify_self_hash,
)
from common.contracts.errors import IncompatibleReuse, SchemaRefusal
from common.contracts.identities import is_well_formed

SAMPLE_SCHEMA = "gold-page-sample.v2"
DRAW_SCHEMA = "gold-sampling-draw.v2"
MANUAL_PICK_SCHEMA = "gold-manual-pick.v2"
LAYOUT_SCHEMA = "gold-page-layout.v2"
PADDING_SCHEMA = "gold-padding-rectangles.v2"
MEASUREMENT_SCHEMA = "gold-instrument-membership.v1"
TRANSCRIPTION_SCHEMA = "gold-transcription.v1"
ADJUDICATION_SCHEMA = "gold-adjudication.v1"
_DIMENSIONLESS_SCHEMAS = frozenset(
    {
        "gold-page-sample.v1",
        "gold-sampling-draw.v1",
        "gold-manual-pick.v1",
        "gold-page-layout.v1",
        "gold-padding-rectangles.v1",
    }
)
# The one spelling of "I cannot read this", reserved so an unreadable span is
# counted rather than guessed at, and never quietly dropped from a transcription.
ILLEGIBLE = "[ILLEGIBLE]"
# U+FEFF. `str.strip` does not remove it and it renders as nothing, so a Windows
# editor's "UTF-8 with signature" would otherwise make one transcriber's reading
# differ from an identical one by a character no reviewer can see.
BYTE_ORDER_MARK = "\ufeff"
SETS = frozenset({"calibration", "locked-acceptance"})
REGION_KINDS = frozenset({"act", "non-act-text", "occlusion", "true-blank"})

# These errors mean atomic publication by hard link is unavailable; other OS errors
# retain their native diagnostics because they do not establish that constraint.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})
# Gold inputs are records and short human transcriptions, never image payloads. A
# 64 MiB ceiling is deliberately generous for a 1,000-page frame while keeping a
# special file or hostile JSON document from turning one validation into an
# unbounded read. The descriptor read below enforces it even if a file grows after
# it is opened.
_MAX_INPUT_BYTES = 64 * 1024 * 1024


def _refuse(condition: bool, message: str) -> None:
    if condition:
        raise SchemaRefusal(message)


def _refuse_dimensionless_schema(schema: Any) -> None:
    _refuse(
        isinstance(schema, str) and schema in _DIMENSIONLESS_SCHEMAS,
        f"gold schema {schema!r} predates required page dimensions. This reader cannot "
        "prove that its rectangles lie on their pages. Preserve the v1 bytes unchanged "
        "and use their historical reader or an explicit, provenance-preserving migration; "
        "do not edit the record in place",
    )


def _sha(value: Any, field: str) -> str:
    _refuse(not is_sha256(value), f"{field} is not a lowercase sha256 digest")
    return value


def _refuse_json_float(literal: str) -> Any:
    """Refuse a float literal where it is read, not where it is later hashed.

    `canonical_bytes` refuses floats outright, and every gold quantity — an
    ordinal, a quota, a pixel bound — is an integer. But that refusal is a
    `TypeError` raised from inside `self_hash`, and some validators reach the
    self-hash before they reach the field: a layout region with `"x": 1.5`
    escaped `python -m gold.cli validate` as a traceback and exit 1 rather than
    a named refusal and exit 2. Refusing it at the door lets the error name the
    file that carries it. `parse_constant` covers `NaN` and `Infinity`, which
    json accepts by default and which are floats by another spelling.
    """
    raise SchemaRefusal(
        f"a gold record carries integers, not the float {literal}; a float's JSON form "
        "is not stable enough to hash against"
    )


def _read_regular_bytes(
    path: str | Path,
    kind: str,
    *,
    directory_descriptor: int | None = None,
    display_path: str | Path | None = None,
) -> bytes:
    """Read one bounded regular file without following its final component.

    Validation is about the bytes in the named evidence file, not whatever a
    symlink, FIFO, or concurrent pathname replacement chooses to supply. Opening
    once with ``O_NOFOLLOW`` and reading through that descriptor closes the
    check/use gap; comparing descriptor metadata before and after the bounded read
    refuses an in-place rewrite rather than parsing a torn record.
    """
    shown = display_path if display_path is not None else path
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SchemaRefusal(f"safe {kind} reads require O_NOFOLLOW support; {shown} was not read")
    flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags, dir_fd=directory_descriptor)
    except OSError as error:
        raise SchemaRefusal(
            f"{shown} is not a readable regular {kind} file without following links"
        ) from error
    try:
        before = os.fstat(descriptor)
        _refuse(not stat.S_ISREG(before.st_mode), f"{shown} is not a regular {kind} file")
        _refuse(
            before.st_size > _MAX_INPUT_BYTES,
            f"{shown} exceeds the {_MAX_INPUT_BYTES}-byte gold {kind} input limit",
        )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(_MAX_INPUT_BYTES + 1)
        after = os.fstat(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            # Preserve the input refusal or read failure; close is cleanup and
            # must not replace the reason the evidence was rejected.
            pass
        raise
    else:
        os.close(descriptor)
    _refuse(
        len(data) > _MAX_INPUT_BYTES,
        f"{shown} exceeds the {_MAX_INPUT_BYTES}-byte gold {kind} input limit",
    )
    _refuse(
        (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
        f"{shown} changed while its {kind} bytes were being read; no mixed version was accepted",
    )
    return data


def read_json(
    path: str | Path,
    *,
    directory_descriptor: int | None = None,
    display_path: str | Path | None = None,
) -> Any:
    """Read one JSON file, refusing unreadable or malformed input by name.

    Public so every caller — this module's own frame loader and the CLI's
    argument parsing alike — raises the same named `SchemaRefusal` instead of
    a bare traceback for a malformed catalog, plan, pick, or record file.

    `RecursionError` sits beside the obvious two because it is the same fact —
    this file could not be read — arriving by a route the tuple did not name.
    json's scanner recurses per nesting level, and `_records_in` reads every
    `*.json` in a directory, so one deeply nested file was enough to end
    `validate-corpus` or `verify-sampling` in a traceback rather than a named
    refusal naming it. The depth it fires at is the scanner's own and is not
    pinned here.
    """
    shown = display_path if display_path is not None else path
    raw = _read_regular_bytes(
        path,
        "JSON",
        directory_descriptor=directory_descriptor,
        display_path=shown,
    )
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_float=_refuse_json_float,
            parse_constant=_refuse_json_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise SchemaRefusal(f"{shown} is not readable JSON") from error


def read_transcription_text(path: str | Path) -> str:
    """Read one transcription from a file a person actually typed.

    Exactly one trailing newline is dropped, because a text editor writes it and
    it belongs to the file rather than to the reading. That is the only liberty
    taken with the bytes: everything else — a second blank line, CRLF, a
    non-NFC composition — is the transcriber's own and is refused by name rather
    than tidied away where nobody would see it.
    """
    try:
        text = _read_regular_bytes(path, "UTF-8 text").decode("utf-8")
    except UnicodeDecodeError as error:
        raise SchemaRefusal(f"{path} is not readable UTF-8 text") from error
    return text[:-1] if text.endswith("\n") else text


def load_run_frame(path: str | Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Read the source authority and reject a forged derived frame record."""
    run = read_json(path)
    _refuse(not isinstance(run, dict), "run authority is not an object")
    _refuse(
        not verify_self_hash(run),
        "run authority fails its self-hash; its recorded seed and membership cannot be trusted",
    )
    _refuse(run.get("schema") != SCHEMA_LABEL, "run authority is not the current R0 schema")
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
        # This must match RunTree's precedence: container pages share a declared
        # file digest but carry distinct computed membership digests.
        ordinal = page.get("ordinal")
        page_sha = page.get("computed_sha256")
        if page_sha is None:
            page_sha = page.get("sha256")
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
    expected_seed = digest_bytes(canonical_bytes({"page_digest": page_digest, "purpose": "frame"}))
    # The self-hash proves the record was resealed by somebody; only the
    # rederivation proves the seed is the one this run's own pages produce.
    _refuse(
        membership["seed"] != expected_seed,
        "R0 frame seed diverges from its derivation over the run's own pages",
    )
    _refuse(
        membership["page_digest"] != page_digest,
        "R0 frame page_digest diverges from run source_manifest",
    )
    _refuse(
        membership["frame_digest"] != frame_digest,
        "R0 frame frame_digest diverges from run source_manifest",
    )
    return dict(membership), source


def set_for_page(frame: dict[str, str], page_sha256: str) -> str:
    """A content-driven partition: a page has one set across every corpus frame.

    The frame seed still drives the within-stratum draw order in `_rank`. It must
    not drive this boundary: R0 derives a new seed whenever frame membership
    changes, so a seed-partitioned page could otherwise move from calibration to
    locked acceptance when the same source page appeared in a later frame.
    """
    _sha(page_sha256, "page sha256")
    _sha(frame.get("seed"), "corpus frame seed")
    rank = digest_bytes(canonical_bytes({"page_sha256": page_sha256, "purpose": "gold-set-v1"}))
    return "calibration" if int(rank[0], 16) < 8 else "locked-acceptance"


def _rank(frame: dict[str, str], page: dict[str, Any]) -> str:
    """One rank per catalog page, not per distinct byte content.

    The ordinal is part of the key because the corpus legitimately admits the
    same bytes at two ordinals (one page scanned twice); ranked by sha alone
    the two rows would tie exactly and the draw could not tell them apart.
    """
    return digest_bytes(
        canonical_bytes(
            {
                "seed": frame["seed"],
                "ordinal": page["ordinal"],
                "page_sha256": page["sha256"],
                "stratum": page["stratum"],
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
            not isinstance(row, dict)
            or set(row) != {"ordinal", "sha256", "stratum", "width", "height"},
            "catalog row has the wrong closed schema. Each row must carry exactly ordinal, "
            "sha256, stratum, width, and height. Add the missing fields or remove extras "
            "and retry",
        )
        ordinal, page_sha, stratum = row["ordinal"], row["sha256"], row["stratum"]
        _refuse(
            not isinstance(ordinal, int) or isinstance(ordinal, bool),
            "catalog ordinal is not an integer",
        )
        _sha(page_sha, "catalog sha256")
        _refuse(not isinstance(stratum, str) or not stratum.strip(), "catalog stratum is empty")
        for dimension in ("width", "height"):
            _refuse(
                not isinstance(row[dimension], int)
                or isinstance(row[dimension], bool)
                or row[dimension] <= 0,
                f"catalog page {dimension} is not a positive integer. Rectangle bounds "
                "cannot be checked without a real page size. Record the page's positive "
                f"integer {dimension} in the catalog and retry",
            )
        found.add((ordinal, page_sha))
        result.append(dict(row))
    _refuse(
        found != expected or len(found) != len(rows),
        "catalog does not cover each sealed source page exactly once",
    )
    return sorted(result, key=lambda row: (row["stratum"], row["ordinal"]))


def _method_facts(method: Any, claimed_set: Any, sampling: Any) -> None:
    """Refuse a sample whose method and its provenance fields disagree.

    Each method produces exactly one shape, so a record cannot claim one origin
    while carrying another's evidence. A seeded draw has no human claim to record
    and must name the catalog and plan it was drawn from; a manual pick has a
    stated set and no catalog or plan behind it. Without this, a page chosen by
    hand could be minted as `stratified-seed`, and "the gold was drawn by the
    seed" would be an unfalsifiable label rather than a replayable fact
    (GOVERNANCE 10).
    """
    _refuse(
        not isinstance(method, str) or method not in {"stratified-seed", "manual"},
        "sample method is not recognized. Its selection provenance therefore cannot be "
        "interpreted. Use 'stratified-seed' or 'manual' and retry",
    )
    _refuse(
        claimed_set is not None and (not isinstance(claimed_set, str) or claimed_set not in SETS),
        "claimed_set is not a recognized gold set. The picker's stated partition would "
        "otherwise be ambiguous. Use 'calibration' or 'locked-acceptance' and retry",
    )
    if method == "stratified-seed":
        _refuse(claimed_set is not None, "an automatic draw carries no human claimed_set")
        _refuse(
            sampling is None,
            "a stratified-seed sample must name the catalog and plan it was drawn from",
        )
    else:
        _refuse(claimed_set is None, "a manual pick must record the set its picker stated")
        _refuse(sampling is not None, "a manual pick is not drawn from a catalog and plan")
    if sampling is not None:
        _refuse(
            not isinstance(sampling, dict) or set(sampling) != {"catalog_digest", "plan_digest"},
            "sampling provenance has the wrong closed schema",
        )
        for field in sorted(sampling):
            _sha(sampling[field], f"sampling {field}")


def build_sample(
    frame: dict[str, str],
    page: dict[str, Any],
    *,
    selection_basis: str,
    method: str,
    claimed_set: str | None = None,
    sampling: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the source-derived restatement once; validation replays it exactly.

    `set` is always the page-derived partition — never a caller's assertion — so
    calibration/locked-acceptance disjointness stays enforced by construction
    no matter which method produced the sample. `claimed_set` is the
    separate, honest record of what a manual picker believed the set was at pick
    time; it is carried unchanged even when it disagrees with `set`, so a pick
    made before the frame/seed existed is never silently corrected or discarded
    (GOVERNANCE 2). `sampling` binds a seeded draw to the exact catalog and plan
    that produced it, so `verify_stratified_selection` can replay the draw
    instead of taking `method` at its word.
    """
    _method_facts(method, claimed_set, sampling)
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
        "page": {
            "ordinal": page["ordinal"],
            "sha256": page_sha,
            "stratum": page["stratum"],
            "width": page["width"],
            "height": page["height"],
        },
        "set": gold_set,
        "claimed_set": claimed_set,
        "sampling": dict(sampling) if sampling is not None else None,
    }
    record["sample_digest"] = digest_bytes(canonical_bytes(record))
    record["self_hash"] = self_hash(record)
    return record


def _quotas(plan: Any, strata: set[str]) -> dict[str, dict[str, int]]:
    """The plan, checked to account for every stratum the catalog declares.

    A stratum the plan does not name contributes nothing to gold, and does so
    without saying anything — the silent shortfall GOVERNANCE 2 forbids. So the
    plan must name every stratum in both sets, and a quota of 0 is how a stratum
    is deliberately left unsampled: still a declaration, still visible in the
    plan file.
    """
    _refuse(
        not isinstance(plan, dict) or set(plan) != SETS,
        "sampling plan must contain exactly both gold sets",
    )
    quotas = {}
    for gold_set in sorted(SETS):
        declared = plan[gold_set]
        _refuse(not isinstance(declared, dict), f"{gold_set} plan is not an object")
        _refuse(
            set(declared) != strata,
            f"{gold_set} plan names {sorted(declared)} but the catalog stratifies the "
            f"corpus into {sorted(strata)}; every stratum is quota'd in both sets, and "
            "0 is how one is deliberately left unsampled",
        )
        for quota in declared.values():
            _refuse(
                not isinstance(quota, int) or isinstance(quota, bool) or quota < 0,
                "sample quota is not a non-negative integer",
            )
        quotas[gold_set] = dict(declared)
    return quotas


def sample_stratified(run_path: str | Path, catalog_rows: Any, plan: Any) -> list[dict[str, Any]]:
    """Select quota pages by seed ranking only after the structural set partition."""
    frame, source = load_run_frame(run_path)
    catalog = _catalog(catalog_rows, source)
    plan = _quotas(plan, {row["stratum"] for row in catalog})
    return _select_stratified(frame, catalog, plan)


def _select_stratified(
    frame: dict[str, str], catalog: list[dict[str, Any]], plan: dict[str, dict[str, int]]
) -> list[dict[str, Any]]:
    """Select from normalized inputs so retained draw bytes can be replayed without files."""
    # The normalized catalog and plan, so a row or key reordering of the same
    # stratification digests the same and a replay of the draw still matches.
    sampling = {
        "catalog_digest": digest_bytes(canonical_bytes(catalog)),
        "plan_digest": digest_bytes(canonical_bytes(plan)),
    }
    selected = []
    for gold_set in sorted(SETS):
        for stratum, quota in sorted(plan[gold_set].items()):
            eligible = [
                page
                for page in catalog
                if page["stratum"] == stratum and set_for_page(frame, page["sha256"]) == gold_set
            ]
            eligible.sort(key=lambda page: _rank(frame, page))
            _refuse(
                len(eligible) < quota,
                f"{gold_set}/{stratum} has {len(eligible)} structurally eligible pages "
                f"for quota {quota}",
            )
            selected.extend(
                build_sample(
                    frame,
                    page,
                    selection_basis="seeded-stratified-v1",
                    method="stratified-seed",
                    sampling=sampling,
                )
                for page in eligible[:quota]
            )
    return selected


def build_sampling_draw(
    run_path: str | Path, catalog_rows: Any, plan: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Retain every input and selected member needed to replay one seeded draw."""
    frame, source = load_run_frame(run_path)
    catalog = _catalog(catalog_rows, source)
    normalized_plan = _quotas(plan, {row["stratum"] for row in catalog})
    selected = _select_stratified(frame, catalog, normalized_plan)
    record = {
        "schema": DRAW_SCHEMA,
        "frame": frame,
        "catalog": catalog,
        "plan": normalized_plan,
        "members": sorted(sample["sample_digest"] for sample in selected),
    }
    record["self_hash"] = self_hash(record)
    return validate_sampling_draw(record, run_path), selected


def validate_sampling_draw(record: Any, run_path: str | Path | None = None) -> dict[str, Any]:
    """Recompute selected membership; no restated member count is trusted.

    Without `run_path` (the CLI's `--run`), this checks only the record's
    internal consistency -- its embedded catalog and frame digests against each
    other. It does not prove the draw originated from a real run; only the
    run-authority comparison that `run_path` enables can say that.
    """
    _refuse(
        not isinstance(record, dict)
        or set(record) != {"schema", "frame", "catalog", "plan", "members", "self_hash"},
        "sampling draw has the wrong closed schema",
    )
    _refuse_dimensionless_schema(record["schema"])
    _refuse(record["schema"] != DRAW_SCHEMA, "sampling draw schema is not recognized")
    frame = record["frame"]
    _refuse(
        not isinstance(frame, dict) or set(frame) != {"frame_digest", "page_digest", "seed"},
        "sampling draw frame has the wrong closed schema",
    )
    for field in frame:
        _sha(frame[field], f"sampling draw frame {field}")
    # The seed is a derivation, not a free field: even without a run authority
    # it must be the one the draw's own retained page_digest produces, or a
    # replaced seed would replay a different ranking under an internally
    # consistent record.
    _refuse(
        frame["seed"]
        != digest_bytes(canonical_bytes({"page_digest": frame["page_digest"], "purpose": "frame"})),
        "sampling draw frame seed diverges from its derivation over its own page_digest",
    )
    raw_catalog = record["catalog"]
    _refuse(not isinstance(raw_catalog, list), "sampling draw catalog is not a list")
    source = []
    for row in raw_catalog:
        _refuse(
            not isinstance(row, dict)
            or set(row) != {"ordinal", "sha256", "stratum", "width", "height"},
            "sampling draw catalog row has the wrong closed schema",
        )
        ordinal, page_sha = row["ordinal"], row["sha256"]
        _refuse(
            not isinstance(ordinal, int) or isinstance(ordinal, bool),
            "sampling draw catalog ordinal is not an integer",
        )
        _sha(page_sha, "sampling draw catalog sha256")
        source.append({"ordinal": ordinal, "sha256": page_sha})
    source.sort(key=lambda page: page["ordinal"] if isinstance(page["ordinal"], int) else -1)
    catalog = _catalog(raw_catalog, source)
    page_digest = digest_bytes(canonical_bytes(source))
    _refuse(
        frame["page_digest"] != page_digest
        or frame["frame_digest"] != digest_bytes(canonical_bytes({"pages": source})),
        "sampling draw frame diverges from its retained catalog membership",
    )
    plan = _quotas(record["plan"], {row["stratum"] for row in catalog})
    _refuse(catalog != record["catalog"], "sampling draw catalog is not in canonical order")
    _refuse(plan != record["plan"], "sampling draw plan is not normalized")
    members = record["members"]
    _refuse(not isinstance(members, list), "sampling draw members is not a list")
    for member in members:
        _sha(member, "sampling draw member")
    _refuse(len(set(members)) != len(members), "sampling draw repeats a selected member")
    _refuse(members != sorted(members), "sampling draw members are not in canonical order")
    expected = sorted(
        sample["sample_digest"] for sample in _select_stratified(frame, catalog, plan)
    )
    _refuse(
        members != expected, "sampling draw membership diverges from its seed, catalog, and plan"
    )
    if run_path is not None:
        run_frame, run_source = load_run_frame(run_path)
        _refuse(frame != run_frame, "sampling draw frame diverges from the R0 run authority")
        _refuse(source != run_source, "sampling draw catalog diverges from the R0 run membership")
    _refuse(not verify_self_hash(record), "sampling draw fails its self-hash")
    return record


def verify_recorded_draw(records: Any, draw: Any, run_path: str | Path) -> list[dict[str, Any]]:
    """Verify a draw only from its retained inputs, sample records, and R0 authority.

    Every sample handed in is validated, but only the seed-selected ones are
    reconciled against the draw's retained membership. A manual pick is not a
    claim about the draw and never was: refusing the whole verification because
    one sits in the directory made `verify-sampling` unusable on exactly the
    directory `ingest-manual` reconciles against and `validate-corpus` reads —
    a false accusation ("a sample that was not seed-selected") against a record
    that never asserted it was.

    Nothing is lost by the narrowing. `sample_digest` binds `method`, so a page
    chosen by hand and minted as `stratified-seed` still fails below as a
    member the draw did not produce, and a drawn record re-minted as `manual`
    still fails as a member the draw produced and the directory lacks.
    """
    validate_sampling_draw(draw, run_path)
    _refuse(not isinstance(records, list), "sample records are not a list")
    seeded = []
    for record in records:
        validate_sample(record, run_path)
        if record["method"] == "stratified-seed":
            seeded.append(record)
    present = sorted(sample["sample_digest"] for sample in seeded)
    _refuse(
        present != draw["members"],
        "sample records diverge from the membership retained by the sampling draw",
    )
    return sorted(seeded, key=lambda sample: sample["sample_digest"])


def verify_stratified_selection(
    records: Any, run_path: str | Path, catalog_rows: Any, plan: Any
) -> list[dict[str, Any]]:
    """Replay the draw and refuse a set of samples that is not exactly its result.

    A valid sample record proves its page belongs to the sealed corpus and lands
    in the set the seed puts it in. It does not, by itself, prove the *sampler*
    chose it: `build_sample` will mint any page in the frame. This replays the
    seeded draw from the same three inputs and compares the whole seeded
    selection. Manual records are validated but excluded because they never claim
    draw membership; a hand-picked page wearing `method: stratified-seed`, a
    dropped page, and a catalog re-described after the fact are all refused by
    name.
    """
    expected = {
        record["sample_digest"]: record
        for record in sample_stratified(run_path, catalog_rows, plan)
    }
    _refuse(not isinstance(records, list), "sample records are not a list")
    present = {}
    for record in records:
        validate_sample(record, run_path)
        if record["method"] != "stratified-seed":
            continue
        _refuse(
            record["sample_digest"] in present,
            f"sample {record['sample_digest']} appears twice in the selection",
        )
        present[record["sample_digest"]] = record
    missing = sorted(set(expected) - set(present))
    unexplained = sorted(set(present) - set(expected))
    _refuse(
        bool(missing) or bool(unexplained),
        f"the selection does not replay: {len(missing)} record(s) the draw produced are "
        f"absent and {len(unexplained)} present record(s) the draw did not produce "
        f"(first absent {missing[:1]}, first unexplained {unexplained[:1]})",
    )
    return [present[digest] for digest in sorted(present)]


def ingest_manual_pick(run_path: str | Path, pick: Any) -> dict[str, Any]:
    """Record Tyrel's choice without selecting or replacing it.

    A manual pick's stated `set` is his provenance, not an assertion this
    function polices: B1 picks are made in week one, before the R0 frame or
    its seed exist, so there is no partition to check them against yet. The
    persisted sample's `set` is always the page-derived partition, so
    calibration and locked-acceptance membership remain disjoint; his original
    stated set is kept alongside as `claimed_set` so a pick
    that turns out to land in the other set is an honest, visible, recorded
    disagreement — never a silent reclassification and never a refusal that
    would force him to redo real annotation hours.
    """
    frame, source = load_run_frame(run_path)
    _refuse(
        not isinstance(pick, dict) or set(pick) != {"schema", "selection_basis", "page", "set"},
        "manual pick has the wrong closed schema",
    )
    _refuse_dimensionless_schema(pick["schema"])
    _refuse(pick["schema"] != MANUAL_PICK_SCHEMA, "manual pick schema is not recognized")
    page = pick["page"]
    _refuse(
        not isinstance(page, dict)
        or set(page) != {"ordinal", "sha256", "stratum", "width", "height"},
        "manual pick page has the wrong closed schema. Its page must carry exactly "
        "ordinal, sha256, stratum, width, and height. Add the missing fields or remove "
        "extras and retry",
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
    for dimension in ("width", "height"):
        _refuse(
            not isinstance(page[dimension], int)
            or isinstance(page[dimension], bool)
            or page[dimension] <= 0,
            f"manual pick page {dimension} is not a positive integer. Rectangle bounds "
            "cannot be checked without a real page size. Record the page's positive "
            f"integer {dimension} and retry",
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
    _refuse(
        not isinstance(pick["set"], str) or pick["set"] not in SETS,
        "manual pick set is not recognized. The picker's stated partition would otherwise "
        "be ambiguous. Use 'calibration' or 'locked-acceptance' and retry",
    )
    sample = build_sample(
        frame,
        page,
        selection_basis=pick["selection_basis"],
        method="manual",
        claimed_set=pick["set"],
    )
    return validate_sample(sample, run_path)


def _act_identity(value: Any, label: str) -> str:
    """Shape only: well-formed and specifically an act identity.

    R7a samples pages, and no stage before R2 derives an act, so there is no act
    authority here to check existence against. Shared by every gold record that
    names an act so they all refuse the same shapes for the same reason.
    """
    _refuse(
        not is_well_formed(value) or not value.startswith("act_"),
        f"{label} act_identity is not an act identity",
    )
    return value


def bind_instrument(
    sample: Any, act_identity: str, protocol_digest: str, run_path: str | Path | None = None
) -> dict[str, Any]:
    """Append a measurement binding; the source sample remains immutable.

    `act_identity` is checked for shape only: well-formed and specifically
    `act_`-prefixed, per `common/contracts/identities.py`. It is not, and at
    R7a cannot be, verified against a real Designator proposal's bindings —
    no stage in the build order before R2 produces an act, so R7a has no act
    authority to invent or check against. A syntactically well-formed but
    never-derived act id will pass. Pass `run_path` to additionally re-check the
    bound sample's frame and page against the R0 run authority; it does not and
    cannot reach act existence.
    """
    validate_sample(sample, run_path)
    _act_identity(act_identity, "instrument")
    _sha(protocol_digest, "instrument protocol_digest")
    record = {
        "schema": MEASUREMENT_SCHEMA,
        "sample_digest": sample["sample_digest"],
        "act_identity": act_identity,
        "protocol_digest": protocol_digest,
    }
    record["self_hash"] = self_hash(record)
    return record


def _person(value: Any, label: str) -> str:
    """A human's name on a gold record: present, trimmed, and not a chair.

    Gold is made by people. Nothing in this module can prove a string was typed by
    one, but a name shaped like a pipeline identity is the mistake worth catching:
    a model's output entering the gold corpus would make every later measurement
    circular, since these records are what the pipeline is measured *against*
    (GOVERNANCE 3; GOALS 2's "not against what a witness reported").
    """
    _refuse(not isinstance(value, str) or not value.strip(), f"{label} is empty")
    _refuse(value != value.strip(), f"{label} has surrounding whitespace")
    _refuse(
        is_well_formed(value),
        f"{label} is a pipeline identity, not a person; gold is what the pipeline is "
        "measured against and may not be made of its output",
    )
    return value


def _escaped_illegibilities(value: str, label: str) -> set[int]:
    """Parse the escapes once, left to right, and say where each escaped word starts.

    `\\` is the only escape character and it has exactly two uses: `\\illegible`
    is the literal source word, and `\\\\` is a literal backslash. A backslash
    before anything else escapes nothing and is refused.

    One parse decides both questions, because asking them separately is what made
    the convention ambiguous: reading `\\\\illegible` as a literal backslash, then
    separately asking whether the word behind it is escaped by looking at the
    character in front of it, answers yes to both — the text is at once a literal
    backslash followed by an unescaped illegibility *and* an escaped literal word.
    Nothing in the record decided between them. Left to right, `\\\\` consumes both
    marks and the `illegible` after it is unescaped, so it is refused and the
    transcriber writes `\\\\\\illegible` for a backslash before the literal word.
    These records are immutable and append-only, so an ambiguity admitted now is
    one Tyrel's hours could never be re-recorded out of.
    """
    starts: set[int] = set()
    position = 0
    while position < len(value):
        if value[position] != "\\":
            position += 1
            continue
        rest = value[position + 1 :]
        if rest.startswith("\\"):
            position += 2
            continue
        if rest[: len("illegible")].casefold() == "illegible":
            starts.add(position + 1)
            position += 1 + len("illegible")
            continue
        _refuse(
            True,
            f"{label} carries a backslash that escapes nothing. A lone escape would make "
            f"the stored reading ambiguous. Write \\illegible for the literal source word "
            "or \\\\ for a literal backslash and retry",
        )
    return starts


def _gold_text(value: Any, label: str) -> str:
    """One human reading of the ink, stored so two of them can be compared exactly.

    `[ILLEGIBLE]` is reserved: it is the one spelling of "I cannot read this", so a
    later measure can count unreadable spans instead of guessing which of
    `illegible`, `[illeg.]` or `(ILLEGIBLE?)` meant it. A literal source word is
    written as `\\illegible`, keeping its source meaning visibly separate from
    the reserved token, and a literal backslash is written `\\\\`; those are the
    only two escapes, so the stored reading maps back to the ink one way only.
    Reserving the token also keeps a transcriber from silently dropping what they
    could not read — an empty transcription is refused, and an unreadable act is
    transcribed as the token.

    The exactness rules exist because agreement between two transcribers is
    decided by equality: surrounding whitespace, a CRLF line ending, or a page
    typed in NFD rather than NFC would summon an adjudicator for two identical
    readings, and the disagreement rate is a measure this corpus reports. None of
    them is repaired here — repairing evidence silently is the thing this module
    exists not to do — each is refused by name and its own author fixes it.
    """
    _refuse(
        not isinstance(value, str) or not value.strip(),
        f"{label} is empty; an act nobody can read is transcribed {ILLEGIBLE}, never blank",
    )
    _refuse(value != value.strip(), f"{label} begins or ends with whitespace")
    _refuse("\r" in value, f"{label} carries a CR; gold text uses LF line endings only")
    _refuse(
        BYTE_ORDER_MARK in value,
        f"{label} carries a byte-order mark; it is invisible, it is not part of the "
        "reading, and one transcriber's editor writing it would make two identical "
        "readings compare unequal. Save the file as UTF-8 without a signature",
    )
    _refuse(
        value != unicodedata.normalize("NFC", value),
        f"{label} is not in Unicode NFC; two readings of the same ink must compare equal",
    )
    escaped = _escaped_illegibilities(value, label)
    # Reserved-token spans are carved out by position, not deleted, so a legitimate
    # `[ILLEGIBLE]` sitting between two ordinary word fragments cannot be mistaken
    # for a bad spelling by splicing those fragments back together (e.g. deleting
    # the token from "peril[ILLEGIBLE]legible" used to leave "perillegible", which
    # contains "illegible" and was refused even though the token was used correctly).
    reserved_spans = []
    start = 0
    while True:
        found = value.find(ILLEGIBLE, start)
        if found == -1:
            break
        reserved_spans.append((found, found + len(ILLEGIBLE)))
        start = found + len(ILLEGIBLE)
    # Casefold each fixed-width source slice: folding the whole string changes
    # indexes after characters such as `ß` and `ﬁ`, both of which survive NFC.
    for position in range(len(value) - len("illegible") + 1):
        if value[position : position + len("illegible")].casefold() != "illegible":
            continue
        end = position + len("illegible")
        within_reserved_token = any(
            span_start <= position and end <= span_end for span_start, span_end in reserved_spans
        )
        _refuse(
            not within_reserved_token and position not in escaped,
            f"{label} spells an illegibility some way other than the reserved {ILLEGIBLE}; "
            "write a literal source word as \\illegible",
        )
    return value


def transcribe(
    sample: Any,
    act_identity: str,
    transcriber: str,
    text: str,
    run_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record one transcriber's reading of one act on a sampled page.

    Two of these, made independently, are what an adjudication reconciles. Like
    `bind_instrument` this carries the sample's digest rather than the sample, and
    checks the act identity for shape only; pass `run_path` to also re-check the
    sample against the R0 run authority.
    """
    validate_sample(sample, run_path)
    record = {
        "schema": TRANSCRIPTION_SCHEMA,
        "sample_digest": sample["sample_digest"],
        "act_identity": _act_identity(act_identity, "transcription"),
        "transcriber": _person(transcriber, "transcriber"),
        "text": _gold_text(text, "transcription text"),
    }
    record["self_hash"] = self_hash(record)
    return record


def validate_transcription(record: Any) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict)
        or set(record)
        != {"schema", "sample_digest", "act_identity", "transcriber", "text", "self_hash"},
        "transcription has the wrong closed schema",
    )
    _refuse(record["schema"] != TRANSCRIPTION_SCHEMA, "transcription schema is not recognized")
    _sha(record["sample_digest"], "transcription sample_digest")
    _act_identity(record["act_identity"], "transcription")
    _person(record["transcriber"], "transcriber")
    _gold_text(record["text"], "transcription text")
    _refuse(not verify_self_hash(record), "transcription fails its self-hash")
    return record


def _adjudication_facts(
    transcriptions: Any, outcome: Any, adjudicator: Any, text: Any
) -> tuple[list[dict[str, Any]], str, str | None, str]:
    """The whole rule, applied identically when building and when reading back.

    `outcome` is derived from the two transcriptions, never asserted: a record
    claiming agreement over two readings that differ is refused, exactly as a
    sample claiming the wrong set is.
    """
    _refuse(
        not isinstance(transcriptions, list) or len(transcriptions) != 2,
        "an adjudication reconciles exactly two transcriptions",
    )
    pair = [validate_transcription(record) for record in transcriptions]
    first, second = pair
    _refuse(
        first["sample_digest"] != second["sample_digest"],
        "the two transcriptions are of different gold samples",
    )
    _refuse(
        first["act_identity"] != second["act_identity"],
        "the two transcriptions are of different acts",
    )
    _refuse(
        first["transcriber"] == second["transcriber"],
        "both transcriptions name the same transcriber; the second reading is not "
        "independent of the first",
    )
    _refuse(
        first["transcriber"] > second["transcriber"],
        "adjudicated transcriptions are ordered by transcriber, so the record does not "
        "depend on which reading arrived first",
    )
    if first["text"] == second["text"]:
        _refuse(
            outcome != "agreed",
            f"the two transcriptions are identical, so the outcome is 'agreed', not {outcome!r}",
        )
        _refuse(
            adjudicator is not None,
            "the transcriptions agree; there is nothing for an adjudicator to establish",
        )
        _refuse(
            text != first["text"],
            "the transcriptions agree, so the established text is the text they agree on",
        )
        return pair, "agreed", None, first["text"]
    _refuse(
        outcome != "adjudicated",
        f"the two transcriptions differ, so the outcome is 'adjudicated', not {outcome!r}",
    )
    _refuse(
        adjudicator is None,
        "the transcriptions differ; reconciling them records the adjudicator and the "
        "reading they established from the ink",
    )
    _person(adjudicator, "adjudicator")
    _refuse(
        adjudicator in {first["transcriber"], second["transcriber"]},
        "the adjudicator is one of the two transcribers; a reading cannot be its own "
        "reconciliation",
    )
    return pair, "adjudicated", adjudicator, _gold_text(text, "adjudicated text")


def adjudicate(
    first: Any, second: Any, *, adjudicator: str | None = None, text: str | None = None
) -> dict[str, Any]:
    """Reconcile two independent transcriptions of one act into one gold reading.

    **This is not a picker** (hard rule 8, GOVERNANCE 3), and the distinction is
    the same one the architecture makes everywhere else. The transcribers are
    people making the corpus the pipeline is measured against, not Attestatores,
    and no model output reaches these records. Where the two readings differ, the
    adjudicator does not choose the better transcription: they read the ink and
    record what they read, which may match one, both in part, or neither. Both
    transcriptions are retained inside the record, unaltered, whatever it says
    (GOVERNANCE 4).

    Where the two readings are identical there is nothing to reconcile: the
    outcome is `agreed`, no adjudicator is recorded, and passing one is refused —
    an adjudicator's name on an unadjudicated act would overstate the custody the
    record actually has.
    """
    pair = sorted(
        (validate_transcription(first), validate_transcription(second)),
        key=lambda record: record["transcriber"],
    )
    agreed = pair[0]["text"] == pair[1]["text"]
    pair, outcome, adjudicator, established = _adjudication_facts(
        pair,
        "agreed" if agreed else "adjudicated",
        adjudicator,
        pair[0]["text"] if agreed and text is None else text,
    )
    record = {
        "schema": ADJUDICATION_SCHEMA,
        "sample_digest": pair[0]["sample_digest"],
        "act_identity": pair[0]["act_identity"],
        "transcriptions": pair,
        "outcome": outcome,
        "adjudicator": adjudicator,
        "text": established,
    }
    record["self_hash"] = self_hash(record)
    return record


def validate_adjudication(record: Any) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict)
        or set(record)
        != {
            "schema",
            "sample_digest",
            "act_identity",
            "transcriptions",
            "outcome",
            "adjudicator",
            "text",
            "self_hash",
        },
        "adjudication has the wrong closed schema",
    )
    _refuse(record["schema"] != ADJUDICATION_SCHEMA, "adjudication schema is not recognized")
    pair = _adjudication_facts(
        record["transcriptions"], record["outcome"], record["adjudicator"], record["text"]
    )[0]
    _refuse(
        record["sample_digest"] != pair[0]["sample_digest"],
        "adjudication names a different sample than the transcriptions it carries",
    )
    _refuse(
        record["act_identity"] != pair[0]["act_identity"],
        "adjudication names a different act than the transcriptions it carries",
    )
    _refuse(not verify_self_hash(record), "adjudication fails its self-hash")
    return record


def _rectangle(value: Any, label: str, width: int, height: int) -> None:
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
    _refuse(
        value["x"] + value["w"] > width or value["y"] + value["h"] > height,
        f"{label} lies outside its {width}x{height} sample page. The annotation cannot "
        "describe pixels beyond the declared page. Regenerate an unpublished annotation "
        "with the corrected rectangle or catalog dimensions; preserve a published record "
        "and hold its corpus for review",
    )


def _validate_page_bound_record(
    record: Any, schema: str, extra: set[str], run_path: str | Path | None = None
) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict) or set(record) != {"schema", "sample", *extra, "self_hash"},
        "gold record has the wrong closed schema",
    )
    _refuse_dimensionless_schema(record["schema"])
    _refuse(record["schema"] != schema, "gold record schema is not recognized")
    validate_sample(record["sample"], run_path)
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
            "claimed_set",
            "sampling",
            "sample_digest",
            "self_hash",
        },
        "sample has the wrong closed schema",
    )
    _refuse_dimensionless_schema(record["schema"])
    _refuse(record["schema"] != SAMPLE_SCHEMA, "sample schema is not recognized")
    _method_facts(record["method"], record["claimed_set"], record["sampling"])
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
    # The same rule the draw record answers to: a seed is a derivation over the
    # frame's own page_digest, never a free field, and a replaced-then-resealed
    # one must be refused offline, not only when a run authority is present.
    _refuse(
        frame["seed"]
        != digest_bytes(canonical_bytes({"page_digest": frame["page_digest"], "purpose": "frame"})),
        "sample frame seed diverges from its derivation over its own page_digest",
    )
    page = record["page"]
    _refuse(
        not isinstance(page, dict)
        or set(page) != {"ordinal", "sha256", "stratum", "width", "height"},
        "sample page has the wrong closed schema. Its page must carry exactly ordinal, "
        "sha256, stratum, width, and height. Regenerate an unpublished sample from its "
        "catalog; preserve a published record and hold its corpus for review",
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
    for dimension in ("width", "height"):
        _refuse(
            not isinstance(page[dimension], int)
            or isinstance(page[dimension], bool)
            or page[dimension] <= 0,
            f"sample page {dimension} is not a positive integer. Rectangle bounds "
            "cannot be checked without a real page size. Regenerate an unpublished sample "
            f"from the catalog's {dimension}; preserve a published record and hold its "
            "corpus for review",
        )
    _refuse(
        not isinstance(record["set"], str)
        or record["set"] not in SETS
        or record["set"] != set_for_page(frame, page["sha256"]),
        "sample set conflicts with the page-derived partition. The page would otherwise "
        "belong to two gold sets. Regenerate an unpublished sample from the page sha256; "
        "preserve a published record and hold its corpus for review",
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


def validate_layout(record: Any, run_path: str | Path | None = None) -> dict[str, Any]:
    """Validate a page-layout gold record; pass `run_path` to also re-check its
    embedded sample against the R0 run authority (the same re-check `validate_sample`
    offers standalone) rather than trusting the embedded sample's self-consistency
    alone."""
    result = _validate_page_bound_record(record, LAYOUT_SCHEMA, {"regions"}, run_path)
    regions = result["regions"]
    _refuse(not isinstance(regions, list), "layout regions is not a list")
    # Page-layout gold accounts for the whole page, so "no regions" is not a
    # finding — it is an annotation that never happened, and an empty list would
    # let it read as a completed one (GOVERNANCE 2). A page with nothing on it is
    # annotated as such: that is what the `true-blank` kind is for.
    _refuse(
        not regions,
        "page-layout gold has no regions; an empty page is annotated as true-blank, "
        "not as an empty annotation",
    )
    for region in regions:
        _refuse(
            not isinstance(region, dict) or set(region) != {"kind", "rect"},
            "layout region has the wrong closed schema",
        )
        _refuse(
            not isinstance(region["kind"], str) or region["kind"] not in REGION_KINDS,
            "layout region kind is not recognized. The region therefore has no closed "
            "layout meaning. Regenerate an unpublished annotation with act, non-act-text, "
            "occlusion, or true-blank; preserve a published record for review",
        )
        _rectangle(
            region["rect"],
            "layout region rect",
            result["sample"]["page"]["width"],
            result["sample"]["page"]["height"],
        )
    return result


def validate_padding(record: Any, run_path: str | Path | None = None) -> dict[str, Any]:
    """Validate a padding-rectangles gold record; `run_path` re-checks the embedded
    sample against the R0 run authority, as `validate_layout` does."""
    result = _validate_page_bound_record(
        record, PADDING_SCHEMA, {"rectangles", "calibrated_for_this_corpus"}, run_path
    )
    _refuse(
        not isinstance(result["calibrated_for_this_corpus"], bool),
        "padding calibration flag is not boolean",
    )
    _refuse(not isinstance(result["rectangles"], list), "padding rectangles is not a list")
    _refuse(
        not result["rectangles"],
        "padding gold has no rectangles; a record that measured nothing may not carry "
        "a calibration verdict about this corpus",
    )
    for rectangle in result["rectangles"]:
        _rectangle(
            rectangle,
            "padding rectangle",
            result["sample"]["page"]["width"],
            result["sample"]["page"]["height"],
        )
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
    _act_identity(record["act_identity"], "instrument")
    _sha(record["protocol_digest"], "instrument protocol_digest")
    _refuse(not verify_self_hash(record), "instrument membership fails its self-hash")
    return record


def validate_record(record: Any, run_path: str | Path | None = None) -> dict[str, Any]:
    """Dispatch on the closed schema label; unknown record versions must refuse."""
    schema = record.get("schema") if isinstance(record, dict) else None
    _refuse_dimensionless_schema(schema)
    if schema == SAMPLE_SCHEMA:
        return validate_sample(record, run_path)
    if schema == DRAW_SCHEMA:
        return validate_sampling_draw(record, run_path)
    if schema == LAYOUT_SCHEMA:
        return validate_layout(record, run_path)
    if schema == PADDING_SCHEMA:
        return validate_padding(record, run_path)
    if schema == MEASUREMENT_SCHEMA:
        return validate_measurement(record)
    if schema == TRANSCRIPTION_SCHEMA:
        return validate_transcription(record)
    if schema == ADJUDICATION_SCHEMA:
        return validate_adjudication(record)
    raise SchemaRefusal(f"{schema!r} is not a gold record schema")


def validate_corpus(records: Any, run_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Validate a whole gold corpus, which one record at a time cannot establish.

    Disjointness is enforced by construction across corpus frames: `set_for_page`
    is a function of the sealed page digest, so the same source page cannot move
    from calibration to locked acceptance when a page is added or a shard is
    resplit. Frame mixing is still refused below: each frame has its own seed and
    therefore its own ranking and quota universe, so combining their draws would
    assert a sampling design that no one predeclared.

    The strata are the same kind of fact: the catalog is human-supplied and bound
    to no authority, so one page described as `adverse` in a sample and `ordinary`
    in the layout record that embeds a differently-drawn sample would pass every
    per-record check while making the stratification unmeasurable. A page's pixel
    size is the third of them — R0's `source_manifest` carries neither a stratum
    nor a geometry, so `--run` cannot reach either — and where the corpus retains
    a draw, the draw's whole retained catalog is the authority both are held to.

    Custody of an act is counted per *act*, not per act per sample record. One
    page can legitimately be carried by both a manual and a seeded sample, and
    keying custody on the sample record that reached the act let that page give
    one act two custody chains and two established readings. Every stored
    transcription must terminate in an adjudication, and every record using one
    shaped act identity must resolve to the same ordinal/digest page. The latter
    is only an internal contradiction check: without a Designator authority R7a
    cannot prove that the first page named is the act's true page.

    Page-level facts have the same collection boundary. One page has one sample
    record per selection method and one layout/padding fact set; manual and seeded
    provenance may coexist, and may carry the same annotation, but neither file
    order nor a missing legacy draw may turn two conflicting records into one fact.

    This proves consistency and closure among records present, not act coverage
    over a sampled page. R7a has no authority enumerating the acts that ought to
    exist, so deletion of an entire act chain leaves no local fact to contradict.
    The retained draw is the narrower exception: it enumerates seeded pages, so
    their disappearance is detectable below.

    Draw membership is checked here too, and here is the only place it can be.
    `verify-sampling` reads the sample records in a directory; a layout or padding
    record carries its own copy of a sample *inside* it, so a page the sampler
    never chose could enter gold as an annotation and be replayed by nothing —
    the exact hole "only replaying the whole draw shows that the sampler chose
    those pages" claims to close. Every seeded sample reached from any record,
    embedded or standing alone, must be one the retained draw produced.
    """
    _refuse(
        not isinstance(records, list),
        "gold corpus records is not a list. Collection custody cannot be reconciled from "
        "another shape. Pass the records from one corpus as a JSON list and retry",
    )
    _refuse(
        not records,
        "gold corpus has no records. An empty collection proves no custody facts. Supply "
        "the records from one corpus and retry",
    )
    validated = [validate_record(record, run_path) for record in records]
    samples = [
        record if record["schema"] == SAMPLE_SCHEMA else record["sample"]
        for record in validated
        if record["schema"] in {SAMPLE_SCHEMA, LAYOUT_SCHEMA, PADDING_SCHEMA}
    ]
    draws = {record["self_hash"]: record for record in validated if record["schema"] == DRAW_SCHEMA}
    _refuse(
        len(draws) > 1,
        f"these gold records retain {len(draws)} different sampling draws; a gold corpus "
        "is drawn once per corpus frame, so two draws are two designs and neither can "
        "speak for the records beside it. Keep each draw in a separate gold-record "
        "directory",
    )
    frames: dict[str, dict[str, str]] = {}
    for sample in samples:
        frame = sample["frame"]
        prior = frames.setdefault(frame["frame_digest"], frame)
        _refuse(
            prior != frame,
            f"corpus frame digest {frame['frame_digest']} carries contradictory page_digest "
            "or seed facts across gold records. One frame identity therefore denotes two "
            "different authorities. Keep immutable records unchanged and separate the "
            "frames, or regenerate an unpublished bad record from the R0 authority",
        )
    for draw in draws.values():
        frame = draw["frame"]
        prior = frames.setdefault(frame["frame_digest"], frame)
        _refuse(
            prior != frame,
            f"corpus frame digest {frame['frame_digest']} carries contradictory page_digest "
            "or seed facts across gold records. One frame identity therefore denotes two "
            "different authorities. Keep immutable records unchanged and separate the "
            "frames, or regenerate an unpublished bad record from the R0 authority",
        )
    _refuse(
        len(frames) > 1,
        f"these gold records were built under {len(frames)} different corpus frames "
        f"({', '.join(sorted(frames))}); each frame has its own ranked sampling universe, "
        "so their records cannot be combined as one predeclared draw",
    )
    for draw in draws.values():
        members = set(draw["members"])
        seeded_present = set()
        for sample in samples:
            _refuse(
                sample["method"] == "stratified-seed" and sample["sample_digest"] not in members,
                f"sample {sample['sample_digest']} claims the seeded draw chose it, but the "
                "retained sampling draw did not produce it; a page the sampler never chose "
                "may not enter gold inside an annotation record",
            )
            if sample["method"] == "stratified-seed":
                seeded_present.add(sample["sample_digest"])
        missing = sorted(members - seeded_present)
        _refuse(
            bool(missing),
            f"the retained sampling draw selected {len(missing)} page(s) that this gold "
            "corpus does not carry as a stratified-seed sample. A drawn page has vanished "
            f"or been re-minted under another method (first missing {missing[:1]}). Recover "
            "the byte-identical original seeded record, or preserve the corpus as partial "
            "and hold it for review",
        )
        # The retained whole catalog is the stratum and geometry authority even for
        # manual picks; R0 carries neither fact, and an invented page size can make
        # any rectangle appear to be on-page.
        catalog_rows = {(row["ordinal"], row["sha256"]): row for row in draw["catalog"]}
        for sample in samples:
            page = sample["page"]
            row = catalog_rows.get((page["ordinal"], page["sha256"]))
            _refuse(
                row is None,
                f"sample {sample['sample_digest']} names page {page['ordinal']}/"
                f"{page['sha256']}, which is not in the catalog the retained draw was "
                "drawn from. The sample therefore has no place in this sampling design. "
                "Keep the record unchanged and validate it only beside its matching draw; "
                "hold this corpus for review",
            )
            _refuse(
                row["stratum"] != page["stratum"],
                f"page {page['ordinal']} is stratified {page['stratum']!r} by sample "
                f"{sample['sample_digest']} and {row['stratum']!r} by the catalog the "
                "retained draw was drawn from. The pick would silently restratify the "
                "corpus the draw was designed over. Regenerate an unpublished pick from "
                "the catalog stratum; preserve a published record and hold it for review",
            )
            _refuse(
                (row["width"], row["height"]) != (page["width"], page["height"]),
                f"page {page['ordinal']} is {page['width']}x{page['height']} in sample "
                f"{sample['sample_digest']} and {row['width']}x{row['height']} in the "
                "catalog the retained draw was drawn from. Its rectangle boundary is "
                "therefore ambiguous. Regenerate an unpublished sample from the catalog "
                "dimensions; preserve a published record and hold it for review",
            )
    pages: dict[tuple[int, str], dict[str, Any]] = {}
    for sample in samples:
        page = sample["page"]
        identity = (page["ordinal"], page["sha256"])
        first = pages.setdefault(identity, page)
        _refuse(
            first["stratum"] != page["stratum"],
            f"page {page['sha256']} is stratified as {first['stratum']!r} in one record "
            f"and {page['stratum']!r} in another; the catalog was re-described between them",
        )
        _refuse(
            (first["width"], first["height"]) != (page["width"], page["height"]),
            f"page {page['ordinal']}/{page['sha256']} has dimensions "
            f"{first['width']}x{first['height']} in one record and "
            f"{page['width']}x{page['height']} in another. Its rectangle boundary is "
            "therefore ambiguous. Keep both immutable records unchanged and hold the corpus "
            "for review",
        )

    samples_by_digest: dict[str, dict[str, Any]] = {}
    for sample in samples:
        prior = samples_by_digest.setdefault(sample["sample_digest"], sample)
        _refuse(
            prior != sample,
            f"sample digest {sample['sample_digest']} describes two different samples",
        )

    # One page has one record under each selection method. The same sample is
    # legitimately embedded in several annotations, and a manual record may sit
    # beside a seeded record for the same page, but two distinct records claiming
    # the same method would count one corpus page twice. A retained draw exposes
    # this for seeded records through exact membership; legacy corpora without a
    # draw need the collection rule here.
    samples_by_origin: dict[tuple[str, int, str], str] = {}
    for sample in samples:
        page = sample["page"]
        key = (sample["method"], page["ordinal"], page["sha256"])
        prior_digest = samples_by_origin.setdefault(key, sample["sample_digest"])
        _refuse(
            prior_digest != sample["sample_digest"],
            f"page {page['ordinal']}/{page['sha256']} is carried by two different "
            f"{sample['method']} sample records. Counting both would count one corpus page "
            "twice under one method. Do not publish the second record; if both are already "
            "immutable, preserve them and hold the corpus for review",
        )

    # Layout and padding are page facts, not facts about whichever sample record
    # happened to reach the page. Manual/seeded coexistence must therefore not mint
    # two competing annotations for one page and leave file order to choose one.
    page_annotations: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in validated:
        if record["schema"] not in {LAYOUT_SCHEMA, PADDING_SCHEMA}:
            continue
        page = record["sample"]["page"]
        key = (record["schema"], page["ordinal"], page["sha256"])
        facts = (
            {"regions": record["regions"]}
            if record["schema"] == LAYOUT_SCHEMA
            else {
                "rectangles": record["rectangles"],
                "calibrated_for_this_corpus": record["calibrated_for_this_corpus"],
            }
        )
        prior = page_annotations.setdefault(key, facts)
        _refuse(
            prior != facts,
            f"page {page['ordinal']}/{page['sha256']} has two conflicting "
            f"{record['schema']} records. The page therefore has no unique gold annotation "
            "of that kind. Preserve both records and hold the corpus for review",
        )

    # Custody is keyed by act identity, not sample digest: manual and seeded
    # provenance may carry the same page, but the act still has one reading chain.
    transcriptions_by_digest: dict[str, dict[str, Any]] = {}
    transcription_keys: dict[tuple[str, str], str] = {}
    transcriptions_by_act: dict[str, set[str]] = {}
    act_pages: dict[str, tuple[int, str]] = {}

    def reconcile_act_page(record: dict[str, Any], label: str) -> None:
        """Hold one shaped act id to one page everywhere this corpus uses it.

        R7a has no Designator authority from which to rederive an act identity, so
        this cannot prove that the first page named is the true one. It can and must
        refuse a corpus that contradicts itself by placing that same identity on a
        second page. Page identity includes ordinal as well as bytes: duplicate scans
        at two ordinals are distinct pages under the sampler and under act derivation.
        """
        sample = samples_by_digest[record["sample_digest"]]
        page = sample["page"]
        identity = (page["ordinal"], page["sha256"])
        prior = act_pages.setdefault(record["act_identity"], identity)
        _refuse(
            prior != identity,
            f"act {record['act_identity']} is bound to page {prior[0]}/{prior[1]} in one "
            f"gold record and page {identity[0]}/{identity[1]} in {label}. One act identity "
            "therefore has contradictory custody. Verify the act and sample bindings, then "
            "correct the unpublished record or hold immutable records for review",
        )

    for record in validated:
        if record["schema"] != TRANSCRIPTION_SCHEMA:
            continue
        sample_digest = record["sample_digest"]
        _refuse(
            sample_digest not in samples_by_digest,
            f"transcription {record['self_hash']} names sample {sample_digest}, but that "
            "sample is absent from the gold corpus",
        )
        reconcile_act_page(record, f"transcription {record['self_hash']}")
        key = (record["act_identity"], record["transcriber"])
        prior_digest = transcription_keys.setdefault(key, record["self_hash"])
        _refuse(
            prior_digest != record["self_hash"],
            f"transcriber {record['transcriber']!r} supplied two transcription records for "
            f"act {record['act_identity']}. The act no longer has one independent reading "
            "from that person. Preserve both records and hold the corpus for review",
        )
        transcriptions_by_digest[record["self_hash"]] = record
        transcriptions_by_act.setdefault(record["act_identity"], set()).add(record["self_hash"])

    adjudications: dict[str, str] = {}
    for record in validated:
        schema = record["schema"]
        if schema == MEASUREMENT_SCHEMA:
            _refuse(
                record["sample_digest"] not in samples_by_digest,
                f"instrument membership {record['self_hash']} names sample "
                f"{record['sample_digest']}, but that sample is absent from the gold corpus",
            )
            reconcile_act_page(record, f"instrument membership {record['self_hash']}")
            continue
        if schema != ADJUDICATION_SCHEMA:
            continue
        key = record["act_identity"]
        _refuse(
            record["sample_digest"] not in samples_by_digest,
            f"adjudication {record['self_hash']} names sample {record['sample_digest']}, but "
            "that sample is absent from the gold corpus",
        )
        reconcile_act_page(record, f"adjudication {record['self_hash']}")
        embedded = {item["self_hash"] for item in record["transcriptions"]}
        absent = sorted(embedded - set(transcriptions_by_digest))
        _refuse(
            bool(absent),
            f"adjudication {record['self_hash']} embeds {len(absent)} transcription(s) that "
            f"are absent as independent gold records (first absent {absent[:1]})",
        )
        _refuse(
            transcriptions_by_act.get(key, set()) != embedded,
            f"act {record['act_identity']} does not have exactly the two independent "
            "transcriptions embedded by its adjudication",
        )
        prior_digest = adjudications.setdefault(key, record["self_hash"])
        _refuse(
            prior_digest != record["self_hash"],
            f"act {record['act_identity']} has two conflicting adjudications; gold has "
            "one established reading per act",
        )
    unadjudicated = sorted(set(transcriptions_by_act) - set(adjudications))
    _refuse(
        bool(unadjudicated),
        f"{len(unadjudicated)} act(s) have independently stored transcriptions but no "
        f"adjudication (first unadjudicated {unadjudicated[:1]}). The started gold-reading "
        "custody chain is incomplete. Collect the second independent transcription if "
        "needed, adjudicate the exact stored pair, and retry",
    )
    return validated


def _open_directory_no_follow(path: Path) -> int:
    """Open the named directory once, refusing a final symlink."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise SchemaRefusal("safe gold publication requires O_NOFOLLOW and O_DIRECTORY support")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | directory | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as error:
        raise SchemaRefusal(
            f"the gold output directory {path} could not be opened without following links"
        ) from error
    try:
        details = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if not stat.S_ISDIR(details.st_mode):
        os.close(descriptor)
        raise SchemaRefusal(f"the gold output directory {path} is not a directory")
    return descriptor


def _portable_name(name: str) -> str:
    """The spelling identity used by default case-insensitive APFS."""
    return unicodedata.normalize("NFC", name).casefold()


def _read_existing_at(directory_descriptor: int, name: str, maximum: int) -> bytes:
    """Read an existing publication without following a planted target link."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError(errno.ENOTSUP, "O_NOFOLLOW is unavailable")
    descriptor = os.open(
        name,
        os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_descriptor,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError(errno.EINVAL, "existing target is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError(errno.EBUSY, "existing target changed while it was read")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)
    return data


def write_append_only(
    path: str | Path,
    record: dict[str, Any],
    *,
    directory_descriptor: int | None = None,
) -> Path:
    """Atomically create a record. Identical bytes are reuse; different bytes refuse.

    A sample draw writes several files and must be resumable after interruption.
    Byte-for-byte republication changes no evidence; different content under one
    name is `IncompatibleReuse`, and the existing file is never touched.
    """
    target = Path(path)
    _refuse(target.name in {"", ".", ".."}, f"gold artifact path {target} has no file name")
    data = canonical_bytes(record) + b"\n"
    owns_directory = directory_descriptor is None
    if owns_directory:
        target.parent.mkdir(parents=True, exist_ok=True)
        directory_descriptor = _open_directory_no_follow(target.parent)
    assert directory_descriptor is not None
    try:
        opened = os.fstat(directory_descriptor)
        _refuse(
            not stat.S_ISDIR(opened.st_mode),
            f"the descriptor for gold output {target.parent} is not a directory",
        )
        if not owns_directory:
            try:
                named = os.stat(target.parent, follow_symlinks=False)
            except OSError as error:
                raise SchemaRefusal(
                    f"the locked gold-record directory {target.parent} no longer names the "
                    "directory whose lock is held; no redirected path was used"
                ) from error
            _refuse(
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino),
                f"the locked gold-record directory {target.parent} was replaced after it "
                "was locked; no redirected path was used",
            )
        portable = _portable_name(target.name)
        try:
            existing_names = os.listdir(directory_descriptor)
        except OSError as error:
            raise SchemaRefusal(
                f"the gold output directory {target.parent} could not be listed through "
                "the descriptor used for publication; no record was written"
            ) from error
        collision = next(
            (
                name
                for name in existing_names
                if name != target.name and _portable_name(name) == portable
            ),
            None,
        )
        _refuse(
            collision is not None,
            f"gold artifact {target} collides by case or Unicode normalization with an "
            "existing name; the corpus must have one portable spelling per record",
        )

        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise SchemaRefusal("safe gold publication requires O_NOFOLLOW support")
        temporary = ""
        temporary_descriptor: int | None = None
        for _attempt in range(100):
            candidate = f".gold-{secrets.token_hex(16)}"
            try:
                temporary_descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        if temporary_descriptor is None:
            raise SchemaRefusal(
                f"the gold output directory {target.parent} could not allocate a unique "
                "temporary name after 100 attempts"
            )
        try:
            with os.fdopen(temporary_descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except OSError:
                # A cleanup failure must not replace the write/refusal that caused
                # this path. The unpredictable dot-file is not published evidence.
                pass
            raise

        try:
            try:
                # Both names are resolved relative to the directory inode the caller
                # locked (or this function opened), never by rewalking its spelling.
                os.link(
                    temporary,
                    target.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                try:
                    existing = _read_existing_at(directory_descriptor, target.name, len(data))
                except OSError as read_error:
                    raise IncompatibleReuse(
                        f"gold artifact {target} appeared while it was being written and is "
                        "not a readable regular non-symlink file; the existing entry was "
                        "not replaced"
                    ) from read_error
                if existing != data:
                    raise IncompatibleReuse(
                        f"gold artifact {target} already holds different bytes; gold records "
                        "are immutable, so one name may not describe two records, and the "
                        "existing file was not touched"
                    ) from error
            except OSError as error:
                if error.errno in _NO_HARD_LINKS:
                    raise SchemaRefusal(
                        f"the gold output root at {target.parent} is on a filesystem that "
                        f"refuses hard links ({error.strerror}); gold records are published by "
                        "atomic link so a partly written file can never take its final name, "
                        "and the output root has to be on a filesystem that supports it"
                    ) from error
                raise
            os.fsync(directory_descriptor)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory_descriptor)
            except OSError:
                # Preserve the security refusal or publication failure. A cleanup
                # error must not turn it into an unrelated generic exception.
                pass
            raise
        else:
            os.unlink(temporary, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
    except BaseException:
        if owns_directory:
            try:
                os.close(directory_descriptor)
            except OSError:
                # As above, a close failure cannot mask a security refusal.
                pass
        raise
    else:
        if owns_directory:
            os.close(directory_descriptor)
    return target
