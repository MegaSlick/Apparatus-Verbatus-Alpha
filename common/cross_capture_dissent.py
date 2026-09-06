"""Immutable, non-establishing evidence for one joint capture read.

This record deliberately has no text from which an Archetypus can be made.  A
joint Perlector response may describe what was observed at a view, but those
forms are evidence beside its one returned reading, never candidates to choose
or merge.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Final

from common.contracts.canonical import digest_of, is_sha256, self_hash, verify_self_hash
from common.contracts.errors import SchemaRefusal
from common.contracts.identities import is_well_formed
from common.corpus_register import _FORBIDDEN_PREFERENCE_FIELDS, refuse_capture_preference
from common.cross_capture_autopsia import _is_printable_nfc

SCHEMA: Final = "cross-capture-dissent.v1"
CAVEAT: Final = (
    "Cross-capture disagreement detects instability or material difference; it does not "
    "identify the accurate reading. Agreement does not prove accuracy."
)
_FIELDS: Final = frozenset(
    {
        "schema",
        "logical_act_id",
        "perlectio_ref",
        "partition_ref",
        "config_digest",
        "model_provenance",
        "reader_invocation_ref",
        "response_observation_digest",
        "views",
        "loci",
        "pairs",
        "caveat",
        "self_hash",
    }
)
_REF_FIELDS: Final = frozenset({"relative_path", "sha256"})
_VIEW_FIELDS: Final = frozenset({"view_id", "source_sha256", "region_refs", "visibility_state"})
_LOCUS_FIELDS: Final = frozenset(
    {"locus_id", "established_span_or_gap_ref", "comparison_state", "observations"}
)
_OBSERVATION_FIELDS: Final = frozenset(
    {"view_id", "observed_form", "image_region_refs", "reason_codes"}
)
_PAIR_FIELDS: Final = frozenset(
    {
        "pair_id",
        "view_ids",
        "capture_condition",
        "same_ink",
        "identical_run_configuration",
        "act_match_correct",
        "finding_codes",
    }
)
_CONDITION_FIELDS: Final = frozenset({"both_unoccluded", "comparably_captured"})
_SPAN_FIELDS: Final = frozenset({"start", "end"})
# Consult §6, made mechanical rather than described: "The record contains
# structural observations and exact image anchors, not a quality score,
# confidence, rank, severity, or scalar variance."  The field-by-field checks
# below cannot enforce that on their own -- `model_provenance` is accepted as
# any object at all -- so the whole candidate is swept for these keys first, at
# every depth.
#
# Matched as key fragments, not exact names, because this vocabulary is the one
# a builder spells slightly differently (`iou_score`, `mean_variance`,
# `capture_confidence`) while meaning exactly the thing §6 forbids.  §7 shape
# 1's preference vocabulary is swept the same way, from the corpus register's
# own list rather than a second copy here, so the two spellings of "forbidden"
# cannot drift; `refuse_capture_preference` then matches those names exactly and names
# the producing record in its refusal.  The sweep is over keys only:
# `observed_form` is legitimately a witness-shaped string and must stay
# readable as evidence.
_FORBIDDEN_CLAIM_FRAGMENTS: Final = (
    "variance",
    "score",
    "confidence",
    "severity",
    "rank",
    "weight",
    "quality",
)
_FAILED_CONDITION_CODES: Final = {
    "both_unoccluded": "capture-occlusion-condition-failed",
    "comparably_captured": "capture-comparability-condition-failed",
    "same_ink": "same-ink-condition-failed",
    "identical_run_configuration": "identical-run-configuration-condition-failed",
    "act_match_correct": "cross-capture-match-failure",
}


def _stable_id(value: Any, label: str) -> str:
    """Refuse invisible or normalization-variant structural identities.

    The predicate is the autopsia's own `_is_printable_nfc`, not a second
    copy: both modules guard the same fact -- one accented key cannot become
    two -- and two copies could drift until the autopsia and the dissent
    record named views by different keys for one act.
    """
    if not isinstance(value, str) or not value or not _is_printable_nfc(value):
        raise SchemaRefusal(
            f"cross-capture dissent: {label} is not a non-empty printable NFC identity; "
            "the dissent record is refused because two spellings of one structural key "
            "cannot name different evidence"
        )
    return value


def _refuse_scalar_claim_keys(value: Any) -> None:
    # Iterative like its sibling screens (corpus_register, autopsia,
    # partition): the value is untrusted caller input, and depth must be this
    # walk's own list, never the interpreter stack.
    #
    # And cycle-aware like all of them, by the same on-path bookkeeping: the
    # record reaching `build_cross_capture_dissent` is the caller's own keyword
    # structure, so a value that is its own ancestor gets here, and a worklist
    # with no stack to exhaust would append forever rather than refuse. Only
    # containers open on the current path are tracked, so a shared, non-cyclic
    # sub-record is still screened wherever it appears.
    pending: list[tuple[str, Any]] = [("value", value)]
    open_path: set[int] = set()
    while pending:
        kind, current = pending.pop()
        if kind == "exit":
            open_path.discard(current)
            continue
        if isinstance(current, (dict, list)):
            marker = id(current)
            if marker in open_path:
                raise SchemaRefusal(
                    "cross-capture dissent: the record contains itself, so no sweep of it can "
                    "terminate and a forbidden field below the loop could never be found; the "
                    "dissent record is refused"
                )
            open_path.add(marker)
            pending.append(("exit", marker))
        if isinstance(current, dict):
            for key, item in current.items():
                lowered = str(key).lower()
                if any(fragment in lowered for fragment in _FORBIDDEN_PREFERENCE_FIELDS):
                    raise SchemaRefusal(
                        f"cross-capture dissent: forbidden preference field {key!r}; the dissent "
                        "record is refused because a compound field name cannot designate a "
                        "capture or observation as the one to use"
                    )
                if any(fragment in lowered for fragment in _FORBIDDEN_CLAIM_FRAGMENTS):
                    raise SchemaRefusal(
                        f"cross-capture dissent: forbidden scalar-claim field {key!r}; this record "
                        "carries structural observations and image anchors, never a score, a rank, "
                        "or a variance number"
                    )
                pending.append(("value", item))
        elif isinstance(current, list):
            pending.extend(("value", item) for item in current)


def _span_or_gap_ref(value: Any) -> Any:
    """Where in the one established text a locus sits -- never the text itself.

    Closed to exactly the two things its name allows: an offset span into the
    established text, or a digest-bound reference to the gap artifact that
    stands where the reading declined to place either observed form.  Anything
    free-form here could carry the established string itself, and this record's
    whole purpose is to hold evidence *beside* that text and never a second
    copy of it (consult §6, GOVERNANCE 5).
    """
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == _SPAN_FIELDS:
        start, end = value["start"], value["end"]
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or isinstance(start, bool)
            or isinstance(end, bool)
            or start < 0
            or end < start
        ):
            raise SchemaRefusal("cross-capture dissent: a locus span is not an ordered offset pair")
        return {"start": start, "end": end}
    if isinstance(value, dict) and set(value) == _REF_FIELDS:
        return _ref(value, "locus gap reference")
    raise SchemaRefusal(
        "cross-capture dissent: established_span_or_gap_ref is neither an offset span nor a "
        "digest-bound gap reference; the established text may not travel in this record"
    )


def _sha(value: Any, label: str) -> str:
    if not is_sha256(value):
        raise SchemaRefusal(f"cross-capture dissent: {label} is not a lowercase SHA-256")
    return value


def _ref(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _REF_FIELDS:
        raise SchemaRefusal(f"cross-capture dissent: {label} is not a digest-bound reference")
    if not isinstance(value["relative_path"], str) or not value["relative_path"]:
        raise SchemaRefusal(f"cross-capture dissent: {label} has no path")
    return {"relative_path": value["relative_path"], "sha256": _sha(value["sha256"], label)}


def _refs(value: Any, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SchemaRefusal(f"cross-capture dissent: {label} must retain image anchors")
    return sorted(
        (_ref(item, label) for item in value),
        key=lambda item: (item["relative_path"], item["sha256"]),
    )


def _view(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _VIEW_FIELDS:
        raise SchemaRefusal("cross-capture dissent: a view is outside its closed schema")
    view_id = _stable_id(value["view_id"], "view_id")
    if value["visibility_state"] not in {"visible", "occluded", "unresolved"}:
        raise SchemaRefusal("cross-capture dissent: a view has an unknown visibility state")
    return {
        "view_id": view_id,
        "source_sha256": _sha(value["source_sha256"], "view source_sha256"),
        "region_refs": _refs(value["region_refs"], "view region_refs"),
        "visibility_state": value["visibility_state"],
    }


def _locus(value: Any, views_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _LOCUS_FIELDS:
        raise SchemaRefusal("cross-capture dissent: a locus is outside its closed schema")
    view_ids = set(views_by_id)
    locus_id = _stable_id(value["locus_id"], "locus_id")
    if value["comparison_state"] not in {
        "same-across-views",
        "different-across-views",
        "unreadable",
        "not-comparable",
    }:
        raise SchemaRefusal("cross-capture dissent: a locus has an unknown comparison state")
    observations = value["observations"]
    if not isinstance(observations, list) or not observations:
        raise SchemaRefusal("cross-capture dissent: a locus drops its observations")
    checked = []
    observed_view_ids: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict) or set(observation) != _OBSERVATION_FIELDS:
            raise SchemaRefusal(
                "cross-capture dissent: an observation is outside its closed schema"
            )
        view_id = observation["view_id"]
        if view_id not in view_ids:
            raise SchemaRefusal("cross-capture dissent: an observation names an unknown view")
        if view_id in observed_view_ids:
            raise SchemaRefusal(
                f"cross-capture dissent: locus {locus_id!r} repeats observation view "
                f"{view_id!r}; the locus is refused because one capture cannot count twice"
            )
        observed_view_ids.add(view_id)
        form = observation["observed_form"]
        if form is not None and not isinstance(form, str):
            raise SchemaRefusal("cross-capture dissent: observed_form is evidence text or null")
        codes = observation["reason_codes"]
        if not isinstance(codes, list) or not all(isinstance(code, str) and code for code in codes):
            raise SchemaRefusal("cross-capture dissent: an observation has malformed reason codes")
        anchors = _refs(observation["image_region_refs"], "observation anchors")
        known_anchors = {
            (reference["relative_path"], reference["sha256"])
            for reference in views_by_id[view_id]["region_refs"]
        }
        foreign_anchors = [
            reference
            for reference in anchors
            if (reference["relative_path"], reference["sha256"]) not in known_anchors
        ]
        if foreign_anchors:
            raise SchemaRefusal(
                f"cross-capture dissent: locus {locus_id!r} observation {view_id!r} cites "
                f"region(s) outside that view {foreign_anchors}; the locus is refused because "
                "one capture's observation cannot borrow another capture's image evidence"
            )
        checked.append(
            {
                "view_id": view_id,
                "observed_form": form,
                "image_region_refs": anchors,
                "reason_codes": sorted(set(codes)),
            }
        )
    if observed_view_ids != view_ids:
        missing = sorted(view_ids - observed_view_ids)
        raise SchemaRefusal(
            f"cross-capture dissent: locus {locus_id!r} omits view observation(s) {missing}; "
            "the locus is refused because every capture must remain explicit in the "
            "observation denominator, including an unreadable or unavailable result"
        )
    return {
        "locus_id": locus_id,
        "established_span_or_gap_ref": _span_or_gap_ref(value["established_span_or_gap_ref"]),
        "comparison_state": value["comparison_state"],
        "observations": sorted(checked, key=lambda item: item["view_id"]),
    }


def _pair(value: Any, view_ids: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _PAIR_FIELDS:
        raise SchemaRefusal("cross-capture dissent: a pair is outside its closed schema")
    ids = value["view_ids"]
    if (
        not isinstance(ids, list)
        or len(ids) != 2
        or any(not isinstance(view_id, str) for view_id in ids)
        or len(set(ids)) != 2
        or set(ids) - view_ids
    ):
        raise SchemaRefusal("cross-capture dissent: a pair must name exactly two known views")
    condition = value["capture_condition"]
    if not isinstance(condition, dict) or set(condition) != _CONDITION_FIELDS:
        raise SchemaRefusal("cross-capture dissent: pair capture condition is malformed")
    if not all(isinstance(condition[field], bool) for field in _CONDITION_FIELDS):
        raise SchemaRefusal("cross-capture dissent: pair capture condition is not boolean")
    if not all(
        isinstance(value[field], bool)
        for field in ("same_ink", "identical_run_configuration", "act_match_correct")
    ):
        raise SchemaRefusal("cross-capture dissent: pair conditions are not boolean")
    codes = value["finding_codes"]
    if not isinstance(codes, list) or not all(isinstance(code, str) and code for code in codes):
        raise SchemaRefusal("cross-capture dissent: pair finding codes are malformed")
    failed = {
        _FAILED_CONDITION_CODES[field]
        for field, passed in {
            **condition,
            "same_ink": value["same_ink"],
            "identical_run_configuration": value["identical_run_configuration"],
            "act_match_correct": value["act_match_correct"],
        }.items()
        if not passed
    }
    missing = sorted(failed - set(codes))
    if missing:
        raise SchemaRefusal(
            f"cross-capture dissent: pair {sorted(ids)!r} failed condition finding(s) "
            f"{missing}; the Unit 20 pair is refused because a failed condition may not "
            "remain in the denominator without naming what failed"
        )
    contradicted = sorted((set(_FAILED_CONDITION_CODES.values()) - failed) & set(codes))
    if contradicted:
        raise SchemaRefusal(
            f"cross-capture dissent: pair {sorted(ids)!r} names passed condition(s) as failed "
            f"with finding(s) {contradicted}; the Unit 20 pair is refused because its finding "
            "codes contradict the boolean denominator facts"
        )
    canonical_ids = sorted(ids)
    expected_pair_id = f"pair:{digest_of(canonical_ids)}"
    if value["pair_id"] != expected_pair_id:
        raise SchemaRefusal("cross-capture dissent: pair identity is not canonical")
    return {
        "pair_id": expected_pair_id,
        "view_ids": canonical_ids,
        "capture_condition": dict(condition),
        "same_ink": value["same_ink"],
        "identical_run_configuration": value["identical_run_configuration"],
        "act_match_correct": value["act_match_correct"],
        "finding_codes": sorted(set(codes)),
    }


def build_cross_capture_dissent(**record: Any) -> dict[str, Any]:
    """Validate and seal the pair-complete Unit 19 evidence record."""
    candidate = dict(record)
    candidate.pop("self_hash", None)
    # Both screens are iterative, so a deep `model_provenance` is walked to the
    # bottom here; the depth boundary is then `self_hash`'s own named refusal
    # ("no canonical serial form"), never an uncaught RecursionError.
    _refuse_scalar_claim_keys(candidate)
    # The fragment sweep above already subsumes these exact names at every
    # depth.  This is the shared screen every producer of the §7 vocabulary
    # runs, kept so that narrowing the fragment match can never silently retire
    # the preference screen with it.
    refuse_capture_preference(candidate, what="cross-capture dissent")
    supplied_caveat = candidate.pop("caveat", CAVEAT)
    if supplied_caveat != CAVEAT or set(candidate) != _FIELDS - {"self_hash", "caveat"}:
        raise SchemaRefusal("cross-capture dissent: record is outside its closed schema")
    if (
        candidate["schema"] != SCHEMA
        or not is_well_formed(candidate["logical_act_id"])
        or not candidate["logical_act_id"].startswith("pac_")
    ):
        raise SchemaRefusal(
            "cross-capture dissent: logical_act_id is not a physical-act identity; the "
            "dissent record is refused because image-local or free-form text cannot stand "
            "in for its logical subject"
        )
    if not isinstance(candidate["views"], list):
        raise SchemaRefusal(
            "cross-capture dissent: views are not a list; the dissent record is refused "
            "because its capture denominator cannot be enumerated"
        )
    if not isinstance(candidate["pairs"], list):
        raise SchemaRefusal(
            "cross-capture dissent: pairs are not a list; the dissent record is refused "
            "because its unordered-pair denominator cannot be reconstructed"
        )
    if not isinstance(candidate["loci"], list):
        raise SchemaRefusal(
            "cross-capture dissent: loci are not a list; the dissent record is refused "
            "because its structural observations cannot be enumerated"
        )
    views = [_view(item) for item in candidate["views"]]
    view_ids = {item["view_id"] for item in views}
    view_sources = {item["source_sha256"] for item in views}
    if not views or len(view_ids) != len(views) or len(view_sources) != len(views):
        raise SchemaRefusal(
            "cross-capture dissent: views are empty or repeat a view/capture identity; the "
            "dissent record is refused because each active capture contributes exactly one "
            "view (one active capture after retraction is valid)"
        )
    pairs = [_pair(item, view_ids) for item in candidate["pairs"]]
    expected_pairs = {tuple(pair) for pair in combinations(sorted(view_ids), 2)}
    observed_pairs = {tuple(pair["view_ids"]) for pair in pairs}
    if observed_pairs != expected_pairs or len(pairs) != len(observed_pairs):
        raise SchemaRefusal(
            "cross-capture dissent: every unordered view pair, including failures, is required"
        )
    if not isinstance(candidate["model_provenance"], dict) or not candidate["model_provenance"]:
        raise SchemaRefusal(
            "cross-capture dissent: model provenance is absent or empty; the dissent record "
            "is refused because its observations must retain the reader identity and revision"
        )
    views_by_id = {item["view_id"]: item for item in views}
    loci = [_locus(item, views_by_id) for item in candidate["loci"]]
    locus_ids = [item["locus_id"] for item in loci]
    if len(locus_ids) != len(set(locus_ids)):
        raise SchemaRefusal(
            "cross-capture dissent: loci repeat an identity; the dissent record is refused "
            "because a locus cannot count twice"
        )
    loci.sort(key=lambda item: item["locus_id"])
    result = {
        "schema": SCHEMA,
        "logical_act_id": candidate["logical_act_id"],
        "perlectio_ref": _ref(candidate["perlectio_ref"], "perlectio_ref"),
        "partition_ref": _ref(candidate["partition_ref"], "partition_ref"),
        "config_digest": _sha(candidate["config_digest"], "config_digest"),
        "model_provenance": candidate["model_provenance"],
        "reader_invocation_ref": _ref(candidate["reader_invocation_ref"], "reader_invocation_ref"),
        "response_observation_digest": _sha(
            candidate["response_observation_digest"], "response_observation_digest"
        ),
        "views": sorted(views, key=lambda item: item["view_id"]),
        "loci": loci,
        "pairs": sorted(pairs, key=lambda item: item["pair_id"]),
        "caveat": CAVEAT,
    }
    try:
        result["self_hash"] = self_hash(result)
    except (RecursionError, TypeError) as error:
        raise SchemaRefusal(
            "cross-capture dissent: the record has no canonical serial form; the dissent "
            "record is refused because model provenance and observation evidence must be "
            "digest-bound JSON data"
        ) from error
    return result


def validate_cross_capture_dissent(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _FIELDS or not verify_self_hash(record):
        raise SchemaRefusal(
            "cross-capture dissent: the closed shape or self_hash failed; the dissent record "
            "is refused because resealing cannot substitute fields or bytes after observation"
        )
    rebuilt = build_cross_capture_dissent(**record)
    if rebuilt != record:
        raise SchemaRefusal(
            "cross-capture dissent: the record differs from its reconstructed canonical form; "
            "the dissent record is refused because a valid self_hash cannot authorize "
            "non-canonical evidence ordering or structure"
        )
    return record


def unit20_dissent_input(record: Any) -> dict[str, Any]:
    """The deliberately one-way Unit 20 consumer seam.

    Unit 20 receives the immutable pair denominator and may add structural
    delta/review records elsewhere.  It receives neither an established-text
    field nor a numeric variance claim from Unit 19.

    The subject, the one Perlectio reference, the complete unordered-pair
    denominator, and the caveat: that is the whole seam, and it is deliberately
    narrower than the record (consult §10.13 -- Unit 19 owns the pair-complete
    evidence and the caveat; Unit 20 owns structural deltas and review flags).
    ``loci`` is not projected here.  A Unit 20 implementation that wants the
    per-locus observations reads the sealed record through
    ``validate_cross_capture_dissent``, which re-proves the whole closed shape
    including its caveat; widening this projection instead would give a
    consumer a *partial* copy of the observation set with the caveat's binding
    context stripped down to a string it could drop, and a subset of loci is
    the one thing a record built to be pair-complete must never hand out.

    The caveat cannot be dropped on the way through: ``build_...`` refuses any
    other wording and supplies this one when none is given, ``validate_...``
    requires the field and re-derives the record's canonical form, and this
    projection copies the validated value rather than a caller's.
    """
    checked = validate_cross_capture_dissent(record)
    # Copies, not references: the seam must not hand a consumer live objects
    # into the sealed record, where one written-to pair would fail
    # verify_self_hash on evidence nobody edited on purpose.
    return {
        "schema": SCHEMA,
        "logical_act_id": checked["logical_act_id"],
        "perlectio_ref": dict(checked["perlectio_ref"]),
        "pairs": [
            {
                **pair,
                "view_ids": list(pair["view_ids"]),
                "capture_condition": dict(pair["capture_condition"]),
                "finding_codes": list(pair["finding_codes"]),
            }
            for pair in checked["pairs"]
        ],
        "caveat": checked["caveat"],
    }
