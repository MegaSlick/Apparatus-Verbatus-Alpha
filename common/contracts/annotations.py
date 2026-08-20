"""The Archetypus's transcription annotation layer, shared by its two halves.

`pipeline/6_archetypus/run.py` seals this layer onto the established record
(and re-exports these names so its own API is unchanged);
`pipeline/7_armarium/run.py` reconciles a record's layer against its accepted
reading's, and `pipeline/7_armarium/armarium_export.py` validates the carried
copy in every packaged product on a clean machine. A stage may not import
another stage's uniquely named module
(`pipeline/test_stage_import_boundaries.py`), so the one spelling lives here —
the same move `common/perlector_audit.py` made for the Pass-C audit surface,
and for the same reason: two spellings of one layer's rules is the pair that
drifts.

`validate_annotations` also NORMALIZES: an `illegible` note may legally arrive
without `witness_evidence`, and the validated form always carries it
(defaulted to `[]`). Whoever compares two copies of this layer must compare
the validated forms, never a validated one against a raw one — a raw/
normalized equality is a refusal waiting for the first real annotation.
"""

from __future__ import annotations

from .envelope import validate_input_refs
from .errors import SchemaRefusal

ANNOTATION_KINDS = frozenset({"uncertain", "illegible"})
CERTAINTIES = frozenset({"high", "medium", "low", "unknown"})
_WITNESS_EVIDENCE_FIELDS = frozenset({"witness_ref", "variant"})
_UNCERTAIN_FIELDS = frozenset({"kind", "start", "end", "certainty", "alternatives"})
_ILLEGIBLE_FIELDS = frozenset({"kind", "start", "end", "witness_evidence"})
_MAX_PLAUSIBLE_OFFSET = 10**15


def _is_ref_shaped(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        return False
    try:
        validate_input_refs([value])
    except SchemaRefusal:
        return False
    return True


def _reference_key(reference: dict) -> tuple[str, str]:
    return (reference["relative_path"], reference["sha256"])


def validate_annotations(annotations, text: str, witnesses: dict | None, what: str) -> list[dict]:
    """The Perlectio's uncertainty layer, carried whole and refused if malformed.

    Nothing here can place a character into `text`, and that is a property of the
    schema rather than a rule someone has to remember: no field on either kind
    could carry one, and a gap is required to be zero-width.

    `witnesses` maps `(relative_path, sha256)` to the text that witness actually
    reported, or `None` where it reported none. A quoted `variant` must be a
    substring of what its cited witness reported: a variant no witness said is
    neither the ink nor testimony, and there is nothing else it could honestly be.
    The comparison is exact, because normalizing is where a record starts to
    differ from the testimony it claims to quote.

    `witnesses=None` is every read-back caller — the record's own re-validation,
    the export's reconciliation, and the clean-machine product verifiers — which
    hold a sealed layer and no reading: the roster lives in the Perlectio's
    basis, so attribution and quotation are the two things they cannot re-check.
    Everything else is checked from this one spelling, because a second copy of
    these rules for the read path is a pair that drifts.
    """
    if not isinstance(annotations, list):
        raise SchemaRefusal(f"{what} is not a list")
    # Package-sourced callers can hand a non-string here; the layer's offsets
    # only mean anything against a real text, and a TypeError out of len()
    # would be a crash where the contract owes a named refusal.
    if not isinstance(text, str):
        raise SchemaRefusal(f"{what} cannot be validated against a non-string text")
    length = len(text)
    validated: list[dict] = []
    for index, note in enumerate(annotations):
        label = f"{what}[{index}]"
        if not isinstance(note, dict):
            raise SchemaRefusal(f"{label} is not an object")
        kind = note.get("kind")
        if kind not in ANNOTATION_KINDS:
            raise SchemaRefusal(
                f"{label} has kind {kind!r}, which is not one of {sorted(ANNOTATION_KINDS)}"
            )
        start, end = note.get("start"), note.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise SchemaRefusal(f"{label} has a non-integer start or end")
        if abs(start) > _MAX_PLAUSIBLE_OFFSET or abs(end) > _MAX_PLAUSIBLE_OFFSET:
            raise SchemaRefusal(f"{label} has a start or end far outside any plausible text length")
        if start < 0 or end > length or start > end:
            raise SchemaRefusal(
                f"{label} spans [{start}, {end}), which is outside this reading's own text "
                f"bounds [0, {length}]"
            )
        if kind == "illegible":
            allowed_fields = _ILLEGIBLE_FIELDS
            validated_note = {
                "kind": kind,
                "start": start,
                "end": end,
                "witness_evidence": _validate_witness_evidence(note, start, end, witnesses, label),
            }
        else:  # "uncertain"
            allowed_fields = _UNCERTAIN_FIELDS
            validated_note = {
                "kind": kind,
                "start": start,
                "end": end,
                "certainty": _validate_certainty(note, label),
                "alternatives": _validate_alternatives(note, text, start, end, label),
            }
        unknown = set(note) - allowed_fields
        if unknown:
            raise SchemaRefusal(
                f"{label} carries field(s) {sorted(unknown)} outside its closed schema"
            )
        validated.append(validated_note)
    return validated


def _validate_certainty(note: dict, label: str) -> str:
    certainty = note.get("certainty")
    if certainty not in CERTAINTIES:
        raise SchemaRefusal(
            f"{label} has certainty {certainty!r}, which is not one of {sorted(CERTAINTIES)}"
        )
    return certainty


def _validate_alternatives(note: dict, text: str, start: int, end: int, label: str) -> list[str]:
    """The reader's own candidate readings for characters that ARE in `text`.

    The Perlector's alternatives and not a witness's: it reads the ink
    (ARCHITECTURE), so its uncertainty about a span it did read is its own.
    Witness material attaches to a *gap*, which is the only place spec 10 asks
    for it.

    The span must cover a readable character rather than merely a width, because
    a span over blank text is where the silences collapse: `derive_text_status`
    finds no gap there and returns `no_readable_text`, so one record would claim
    the act held no ink while carrying an annotation asserting characters were
    read at that position and offering alternatives for them.
    """
    if not text[start:end].strip():
        raise SchemaRefusal(
            f"{label} is an uncertain span covering no readable character; uncertainty "
            "flags characters that ARE present in `text`, so it must cover at least one, "
            "and where nothing was read the honest shape is an illegible gap"
        )
    alternatives = note.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        raise SchemaRefusal(f"{label} is uncertain but names no alternatives")
    for alternative in alternatives:
        if not isinstance(alternative, str) or not alternative:
            raise SchemaRefusal(f"{label} names an empty or non-string alternative reading")
    if len(set(alternatives)) != len(alternatives):
        raise SchemaRefusal(f"{label} repeats an alternative reading")
    return list(alternatives)


def _validate_witness_evidence(
    note: dict, start: int, end: int, witnesses: dict | None, label: str
) -> list[dict]:
    """What a witness claimed at a gap: retained as evidence, never as characters.

    An absent or empty list is ordinary — every witness may have found the same
    damage the reader did. What is refused is a claim attributed to a witness that
    this act was not read against, or words that witness never reported. Both of
    those need the roster; `witnesses=None` is the read-back caller that has no
    reading to get one from, and checks the shape alone.
    """
    if start != end:
        raise SchemaRefusal(
            f"{label} is an illegible gap with start {start} != end {end}; a gap is a "
            "zero-width anchor and structurally cannot carry characters into `text`, "
            "whatever evidence hangs off it"
        )
    evidence = note.get("witness_evidence", [])
    if not isinstance(evidence, list):
        raise SchemaRefusal(f"{label}.witness_evidence is not a list")
    checked: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(evidence):
        item_label = f"{label}.witness_evidence[{index}]"
        if not isinstance(item, dict):
            raise SchemaRefusal(f"{item_label} is not an object")
        if set(item) != _WITNESS_EVIDENCE_FIELDS:
            raise SchemaRefusal(
                f"{item_label} is not exactly {sorted(_WITNESS_EVIDENCE_FIELDS)}; a candidate-text "
                "field beside a gap would be a second source of established characters"
            )
        witness_ref = item.get("witness_ref")
        if not _is_ref_shaped(witness_ref) or (
            witnesses is not None and _reference_key(witness_ref) not in witnesses
        ):
            raise SchemaRefusal(
                f"{item_label} names a witness reference that is not one of this act's own "
                "witnesses; annotation evidence may only cite a testimonium already in "
                "this reading's basis"
            )
        variant = item.get("variant")
        if not isinstance(variant, str) or not variant:
            raise SchemaRefusal(f"{item_label} names no variant reading")
        if witnesses is not None:
            reported = witnesses[_reference_key(witness_ref)]
            if not isinstance(reported, str) or variant not in reported:
                raise SchemaRefusal(
                    f"{item_label} quotes a variant its cited witness never reported; a variant "
                    "that is neither the ink nor something a witness actually said is a "
                    "reconstruction, and the record carries none"
                )
        identity = (witness_ref["relative_path"], witness_ref["sha256"], variant)
        if identity in seen:
            raise SchemaRefusal(f"{item_label} repeats the same witness claim")
        seen.add(identity)
        checked.append({"witness_ref": witness_ref, "variant": variant})
    return checked
