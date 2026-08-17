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
import tempfile
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

SAMPLE_SCHEMA = "gold-page-sample.v1"
DRAW_SCHEMA = "gold-sampling-draw.v1"
MANUAL_PICK_SCHEMA = "gold-manual-pick.v1"
LAYOUT_SCHEMA = "gold-page-layout.v1"
PADDING_SCHEMA = "gold-padding-rectangles.v1"
MEASUREMENT_SCHEMA = "gold-instrument-membership.v1"
TRANSCRIPTION_SCHEMA = "gold-transcription.v1"
ADJUDICATION_SCHEMA = "gold-adjudication.v1"
# The one spelling of "I cannot read this", reserved so an unreadable span is
# counted rather than guessed at, and never quietly dropped from a transcription.
ILLEGIBLE = "[ILLEGIBLE]"
# U+FEFF. `str.strip` does not remove it and it renders as nothing, so a Windows
# editor's "UTF-8 with signature" would otherwise make one transcriber's reading
# differ from an identical one by a character no reviewer can see.
BYTE_ORDER_MARK = "\ufeff"
SETS = frozenset({"calibration", "locked-acceptance"})
REGION_KINDS = frozenset({"act", "non-act-text", "occlusion", "true-blank"})

# What a filesystem that will not hard-link answers with. Named so `write_append_only`
# can say which setup fact is wrong instead of letting a bare OSError about `link`
# escape as a traceback (mirrors common/runtree/store.py's `_atomic_create`).
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS})


def _refuse(condition: bool, message: str) -> None:
    if condition:
        raise SchemaRefusal(message)


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
    a named refusal and exit 2, which is the defect F-O8 and F-S5 were opened
    for. Refused at the door instead, where the file that carries it can be
    named. `parse_constant` covers `NaN` and `Infinity`, which json accepts by
    default and which are floats by another spelling.
    """
    raise SchemaRefusal(
        f"a gold record carries integers, not the float {literal}; a float's JSON form "
        "is not stable enough to hash against"
    )


def read_json(path: str | Path) -> Any:
    """Read one JSON file, refusing unreadable or malformed input by name.

    Public so every caller — this module's own frame loader and the CLI's
    argument parsing alike — raises the same named `SchemaRefusal` instead of
    a bare traceback for a malformed catalog, plan, pick, or record file.
    """
    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_float=_refuse_json_float,
            parse_constant=_refuse_json_float,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal(f"{path} is not readable JSON") from error


def read_transcription_text(path: str | Path) -> str:
    """Read one transcription from a file a person actually typed.

    Exactly one trailing newline is dropped, because a text editor writes it and
    it belongs to the file rather than to the reading. That is the only liberty
    taken with the bytes: everything else — a second blank line, CRLF, a
    non-NFC composition — is the transcriber's own and is refused by name rather
    than tidied away where nobody would see it.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"{path} is not readable UTF-8 text") from error
    return text[:-1] if text.endswith("\n") else text


def _frame_from_run(run: dict[str, Any]) -> tuple[dict[str, str], list[dict[str, Any]]]:
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
    return _frame_from_run(read_json(path))


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


def _method_facts(method: Any, claimed_set: Any, sampling: Any) -> None:
    """Refuse a sample whose method and its provenance fields disagree.

    Each method produces exactly one shape, so a record cannot claim one origin
    while carrying another's evidence. A seeded draw has no human claim to record
    and must name the catalog and plan it was drawn from; a manual pick has a
    stated set and no catalog or plan behind it. Without this, a page chosen by
    hand could be minted as `stratified-seed`, and "the gold was drawn by the
    seed" would be an unfalsifiable label rather than a replayable fact
    (GOVERNANCE 10; U18's *provably*).
    """
    _refuse(method not in {"stratified-seed", "manual"}, "sample method is not recognized")
    _refuse(
        claimed_set is not None and claimed_set not in SETS,
        "claimed_set is not a recognized gold set",
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
    (U14/U18) no matter which method produced the sample. `claimed_set` is the
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
        "page": {"ordinal": page["ordinal"], "sha256": page_sha, "stratum": page["stratum"]},
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
    without saying anything — the exact silent shortfall U18 ("the draw provably
    covers the adverse strata") and GOVERNANCE 2 forbid. So the plan must name
    every stratum in both sets, and a quota of 0 is how a stratum is deliberately
    left unsampled: still a declaration, still visible in the plan file.
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
    """Pure selection from already-normalized, retained draw inputs."""
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
            eligible.sort(key=lambda page: _rank(frame, page["sha256"], stratum))
            _refuse(
                len(eligible) < quota,
                f"{gold_set}/{stratum} has {len(eligible)} structurally eligible pages for quota {quota}",
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
    """Recompute selected membership; no restated member count is trusted."""
    _refuse(
        not isinstance(record, dict)
        or set(record) != {"schema", "frame", "catalog", "plan", "members", "self_hash"},
        "sampling draw has the wrong closed schema",
    )
    _refuse(record["schema"] != DRAW_SCHEMA, "sampling draw schema is not recognized")
    frame = record["frame"]
    _refuse(
        not isinstance(frame, dict) or set(frame) != {"frame_digest", "page_digest", "seed"},
        "sampling draw frame has the wrong closed schema",
    )
    for field in frame:
        _sha(frame[field], f"sampling draw frame {field}")
    raw_catalog = record["catalog"]
    _refuse(not isinstance(raw_catalog, list), "sampling draw catalog is not a list")
    source = []
    for row in raw_catalog:
        _refuse(not isinstance(row, dict), "sampling draw catalog row is not an object")
        source.append({"ordinal": row.get("ordinal"), "sha256": row.get("sha256")})
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
    seeded draw from the same three inputs and compares the whole selection, so a
    hand-picked page wearing `method: stratified-seed`, a dropped page, and a
    catalog re-described after the fact are all refused by name.
    """
    expected = {
        record["sample_digest"]: record
        for record in sample_stratified(run_path, catalog_rows, plan)
    }
    _refuse(not isinstance(records, list), "sample records are not a list")
    present = {}
    for record in records:
        validate_sample(record, run_path)
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
    persisted sample's `set` is always the page-derived partition (disjointness
    stays enforced by construction, per U14/U18 and the unconditional "calibration
    data disjoint from locked acceptance gold" in three_stage_reading_design.md
    §6); his original stated set is kept alongside as `claimed_set` so a pick
    that turns out to land in the other set is an honest, visible, recorded
    disagreement — never a silent reclassification and never a refusal that
    would force him to redo real annotation hours.
    """
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
    authority to invent or check against (see TERRA_BUILD_REPORT.md). A
    syntactically well-formed but never-derived act id will pass. Pass
    `run_path` to additionally re-check the bound sample's frame and page
    against the R0 run authority; it does not and cannot reach act existence.
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


def _gold_text(value: Any, label: str) -> str:
    """One human reading of the ink, stored so two of them can be compared exactly.

    `[ILLEGIBLE]` is reserved: it is the one spelling of "I cannot read this", so a
    later measure can count unreadable spans instead of guessing which of
    `illegible`, `[illeg.]` or `(ILLEGIBLE?)` meant it. Reserving it also keeps a
    transcriber from silently dropping what they could not read — an empty
    transcription is refused, and an unreadable act is transcribed as the token.

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
    _refuse(
        "illegible" in value.replace(ILLEGIBLE, "").casefold(),
        f"{label} spells an illegibility some way other than the reserved {ILLEGIBLE}",
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
    pair, _outcome, _adjudicator, _text = _adjudication_facts(
        record["transcriptions"], record["outcome"], record["adjudicator"], record["text"]
    )
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


def _validate_page_bound_record(
    record: Any, schema: str, extra: set[str], run_path: str | Path | None = None
) -> dict[str, Any]:
    _refuse(
        not isinstance(record, dict) or set(record) != {"schema", "sample", *extra, "self_hash"},
        "gold record has the wrong closed schema",
    )
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
        "sample set conflicts with the page-derived partition",
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
        _refuse(region["kind"] not in REGION_KINDS, "layout region kind is not recognized")
        _rectangle(region["rect"], "layout region rect")
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


def validate_record(record: Any, run_path: str | Path | None = None) -> dict[str, Any]:
    """Validate any gold record by the schema it declares."""
    schema = record.get("schema") if isinstance(record, dict) else None
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
    per-record check while making the stratification unmeasurable.

    Draw membership is checked here too, and here is the only place it can be.
    `verify-sampling` reads the sample records in a directory; a layout or padding
    record carries its own copy of a sample *inside* it, so a page the sampler
    never chose could enter gold as an annotation and be replayed by nothing —
    the exact hole "only replaying the whole draw shows that the sampler chose
    those pages" claims to close. Every seeded sample reached from any record,
    embedded or standing alone, must be one the retained draw produced.
    """
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
        "speak for the records beside it",
    )
    frames = {sample["frame"]["frame_digest"]: sample["frame"] for sample in samples}
    for draw in draws.values():
        frames.setdefault(draw["frame"]["frame_digest"], draw["frame"])
    _refuse(
        len(frames) > 1,
        f"these gold records were built under {len(frames)} different corpus frames "
        f"({', '.join(sorted(frames))}); each frame has its own ranked sampling universe, "
        "so their records cannot be combined as one predeclared draw",
    )
    for draw in draws.values():
        members = set(draw["members"])
        for sample in samples:
            _refuse(
                sample["method"] == "stratified-seed" and sample["sample_digest"] not in members,
                f"sample {sample['sample_digest']} claims the seeded draw chose it, but the "
                "retained sampling draw did not produce it; a page the sampler never chose "
                "may not enter gold inside an annotation record",
            )
    pages: dict[str, dict[str, Any]] = {}
    for sample in samples:
        page = sample["page"]
        first = pages.setdefault(page["sha256"], page)
        _refuse(
            first["stratum"] != page["stratum"],
            f"page {page['sha256']} is stratified as {first['stratum']!r} in one record "
            f"and {page['stratum']!r} in another; the catalog was re-described between them",
        )
        _refuse(
            first["ordinal"] != page["ordinal"],
            f"page {page['sha256']} is page {first['ordinal']} in one record and "
            f"{page['ordinal']} in another",
        )

    samples_by_digest: dict[str, dict[str, Any]] = {}
    for sample in samples:
        prior = samples_by_digest.setdefault(sample["sample_digest"], sample)
        _refuse(
            prior != sample,
            f"sample digest {sample['sample_digest']} describes two different samples",
        )

    transcriptions_by_digest: dict[str, dict[str, Any]] = {}
    transcription_keys: dict[tuple[str, str, str], str] = {}
    transcriptions_by_act: dict[tuple[str, str], set[str]] = {}
    for record in validated:
        if record["schema"] != TRANSCRIPTION_SCHEMA:
            continue
        sample_digest = record["sample_digest"]
        _refuse(
            sample_digest not in samples_by_digest,
            f"transcription {record['self_hash']} names sample {sample_digest}, but that "
            "sample is absent from the gold corpus",
        )
        key = (sample_digest, record["act_identity"], record["transcriber"])
        prior_digest = transcription_keys.setdefault(key, record["self_hash"])
        _refuse(
            prior_digest != record["self_hash"],
            f"transcriber {record['transcriber']!r} supplied two different readings for "
            f"act {record['act_identity']} in sample {sample_digest}",
        )
        transcriptions_by_digest[record["self_hash"]] = record
        transcriptions_by_act.setdefault((sample_digest, record["act_identity"]), set()).add(
            record["self_hash"]
        )

    adjudications: dict[tuple[str, str], str] = {}
    for record in validated:
        schema = record["schema"]
        if schema == MEASUREMENT_SCHEMA:
            _refuse(
                record["sample_digest"] not in samples_by_digest,
                f"instrument membership {record['self_hash']} names sample "
                f"{record['sample_digest']}, but that sample is absent from the gold corpus",
            )
            continue
        if schema != ADJUDICATION_SCHEMA:
            continue
        key = (record["sample_digest"], record["act_identity"])
        _refuse(
            record["sample_digest"] not in samples_by_digest,
            f"adjudication {record['self_hash']} names sample {record['sample_digest']}, but "
            "that sample is absent from the gold corpus",
        )
        embedded = {item["self_hash"] for item in record["transcriptions"]}
        absent = sorted(embedded - set(transcriptions_by_digest))
        _refuse(
            bool(absent),
            f"adjudication {record['self_hash']} embeds {len(absent)} transcription(s) that "
            f"are absent as independent gold records (first absent {absent[:1]})",
        )
        _refuse(
            transcriptions_by_act.get(key, set()) != embedded,
            f"act {record['act_identity']} in sample {record['sample_digest']} does not have "
            "exactly the two independent transcriptions embedded by its adjudication",
        )
        prior_digest = adjudications.setdefault(key, record["self_hash"])
        _refuse(
            prior_digest != record["self_hash"],
            f"act {record['act_identity']} in sample {record['sample_digest']} has two "
            "conflicting adjudications; gold has one established reading per act",
        )
    return validated


def write_append_only(path: str | Path, record: dict[str, Any]) -> Path:
    """Atomically create a record. Identical bytes are reuse; different bytes refuse.

    Refusing an existing pathname outright made a draw unrepeatable rather than
    immutable: `sample` writes one file per selected page, so an interruption
    partway through left a directory the same command could never finish — the
    first already-written record aborted the rerun and the unwritten ones stayed
    unwritten. Byte-for-byte republication changes no evidence, and this is the
    rule `common/runtree/store.py::_publish_bytes` already applies to every
    artifact in the pipeline: identical content is reuse, different content under
    one name is `IncompatibleReuse`, and the existing file is never touched.
    """
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
        try:
            existing = target.read_bytes()
        except OSError as read_error:
            raise IncompatibleReuse(
                f"gold artifact {target} appeared while it was being written and could "
                "not be read back; the existing file was not replaced"
            ) from read_error
        if existing != data:
            raise IncompatibleReuse(
                f"gold artifact {target} already holds different bytes; gold records are "
                "immutable, so one name may not describe two records, and the existing "
                "file was not touched"
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
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
