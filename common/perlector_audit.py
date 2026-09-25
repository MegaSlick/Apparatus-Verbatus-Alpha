"""Perlector Pass-C audit records, shared by their two stage-side halves.

`pipeline/4_perlector/run.py` produces the audit draft and finding records;
`pipeline/5_recensor/run.py`'s `audit_state` consumes them. Both must validate
the same closed schema, and a stage may not import another stage's module, so
the shared validation lives here (`pipeline/4_perlector/audit.py` re-exports it).

The audit request lives here too: `reproof_plan` defines the plan,
`audit_request` wraps it into the object the reader is given, and
`validate_chain` re-derives both from the frozen draft, so the seal, the
delivery and the check are one computation (principle 8).
`payload.audit.request_digest` binds a response to its request; it is `None`
exactly when no request was delivered.
"""

from __future__ import annotations

import base64
import inspect
import json
import math
from typing import Any, Final

from common.contracts import uncertainty
from common.contracts.canonical import digest_bytes, digest_of, is_sha256
from common.contracts.envelope import validate_input_refs
from common.contracts.errors import SchemaRefusal
from common.contracts.serving import (
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_CALL_RECORD_SCHEMAS,
    WIRE_DECIMAL_FIELDS,
    WIRE_DECIMAL_SCHEMA,
)
from common.contracts.stages import PERLECTOR
from common.corpus_register import refuse_capture_preference

SCHEMA: Final = "perlector-audit.v3"
LEGACY_SCHEMA: Final = "perlector-audit.v2"
# Refused by name: a v1 record cannot say whether a delivered re-proof
# completed, and inferring it from silence would be the claim it could not make.
# Its act is re-read under the current schema; the old bytes stay (principle 4).
RETIRED_SCHEMAS: Final = frozenset({"perlector-audit.v1"})
# Versioned apart from `SCHEMA`: this names the shape handed to the reader,
# `SCHEMA` the sealed policy.
REQUEST_SCHEMA: Final = "perlector-audit-request.v2"
LEGACY_REQUEST_SCHEMA: Final = "perlector-audit-request.v1"
# A re-proof returns closed edits anchored to the frozen draft, not a
# replacement act. Kept apart from REQUEST_SCHEMA so older requests stay readable.
RESPONSE_SCHEMA: Final = "perlector-audit-response.v1"
AUDIT_PROMPT_SCHEMA: Final = "perlector-audit-prompt.v1"
_AUDIT_PROMPT_FIELDS: Final = frozenset(
    {
        "schema",
        "base_prompt",
        "request_digest",
        "rendered_text",
        "rendered_sha256",
        "renderer_sha256",
        "request_sha256",
    }
)
# The producer keeps its own literal: `test_reader.py` pins `PASS_KINDS` to the
# `pass_kind="..."` literals it reads out of `run.py`.
REPROOF_PASS_KIND: Final = "audit-reproof"
AUDIT_CAP_EXHAUSTED: Final = "audit-round-cap-exhausted"
# What became of the re-examination the frozen flags required:
#   not-due          no flag was raised
#   cap-exhausted    flags were raised and the sealed cap left no round
#   complete         a delivered re-proof completed, any change inside a flag
#   incomplete       a delivered re-proof was not classified complete
#   reproof-rejected a delivered re-proof completed but changed text outside
#                    every flag; the rewrite is refused and the reading stands
# A re-proof cut off after returning the frozen text confirmed nothing, so text
# equality decides only `reproof-rejected`.
EXAMINATION_NOT_DUE: Final = "not-due"
EXAMINATION_CAP_EXHAUSTED: Final = "cap-exhausted"
EXAMINATION_COMPLETE: Final = "complete"
EXAMINATION_INCOMPLETE: Final = "incomplete"
EXAMINATION_REPROOF_REJECTED: Final = "reproof-rejected"
EXAMINATION_STATES: Final = frozenset(
    {
        EXAMINATION_NOT_DUE,
        EXAMINATION_CAP_EXHAUSTED,
        EXAMINATION_COMPLETE,
        EXAMINATION_INCOMPLETE,
        EXAMINATION_REPROOF_REJECTED,
    }
)
# Restated from `pipeline/4_perlector/truncation.py`, which a consumer stage
# may not import.
TRUNCATION_COMPLETE: Final = "complete"
TRUNCATION_TRUNCATED: Final = "truncated"
TRUNCATION_UNKNOWN: Final = "unknown"
TRUNCATION_CLASSIFICATIONS: Final = frozenset(
    {TRUNCATION_COMPLETE, TRUNCATION_TRUNCATED, TRUNCATION_UNKNOWN}
)
# The engine's own stop words this contract recognises; `None` is "no word".
DECLARED_STOP_WORDS: Final = frozenset({"stop", "length"})
_TRUNCATION_SIGNALS: Final = frozenset(
    {"stop_reason_declared", "unclosed_structure", "length_suspicious", "ends_abruptly"}
)
# Every term of the length predicate, floor included, so a reader re-derives
# the signal without the run's protocol file in hand (principle 6).
_TRUNCATION_MEASURE: Final = frozenset(
    {"region_pixels", "page_pixels", "characters", "length_floor_characters_per_page"}
)
# `characters` counts a reading that may legitimately be empty; the three areas
# and the floor are all positive or the signal could not have been judged.
_TRUNCATION_MEASURE_MAY_BE_ZERO: Final = frozenset({"characters"})
FLAG_CLASSES: Final = frozenset(
    {"date-sequence", "numbering", "order", "testimony-diff", "repetition", "within-crop"}
)
# Only a text departure from retained testimony derives a witness location;
# boundary disagreement stays page evidence for the Recensor. One declaration
# for `validate_draft` and the producer that builds the basis rows.
WITNESS_DERIVED_LOCATION_CLASSES: Final = frozenset({"testimony-diff"})
_DRAFT_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "semi_final_text",
        "page_ids",
        "round_cap",
        "policy",
        "flags",
        "flag_location_basis",
    }
)
_FINDING_FIELDS_V2: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "page_ids",
        "round_cap",
        "policy",
        "flags",
        "change_record",
        "uncertain_spans",
        "unresolved",
        "examination",
        "reproof_truncation",
        "reproof_call",
        "reproof_change_span",
    }
)
_FINDING_FIELDS_V3: Final = (_FINDING_FIELDS_V2 - {"reproof_change_span"}) | {"reproof_edits"}
_PERLECTIO_AUDIT_FIELDS: Final = frozenset(
    {
        "draft_ref",
        "finding_ref",
        "finding_digest",
        "unresolved",
        "examination",
        "reproofs",
        "request_digest",
    }
)
# `semi_final_text` is delivered because a re-proof asked to "record confirmed
# unchanged" must be shown what unchanged means.
_AUDIT_REQUEST_FIELDS: Final = frozenset(
    {"schema", "act_key", "attempt_ordinal", "draft_ref", "semi_final_text", "reproofs"}
)


class ReproofResponseRefusal(SchemaRefusal):
    """A delivered re-proof reply cannot be assembled into an act safely."""


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Keep JSON object spelling closed by refusing repeated member names."""
    value: dict[str, Any] = {}
    for name, member in pairs:
        if name in value:
            raise ReproofResponseRefusal(f"an audit re-proof response repeats JSON member {name!r}")
        value[name] = member
    return value


# Unreachable over today's fixed wording; it fires the moment an edit softens it.
FORBIDDEN_PROMPT_FRAGMENTS: Final = (
    "wrong",
    "incorrect",
    "should read",
    "expected",
    "replace with",
    "must change",
)


def neutral_prompt(*, start: int, end: int, text_length: int, policy_schema: str = SCHEMA) -> str:
    if not 0 <= start <= end <= text_length:
        raise SchemaRefusal("an audit re-proof location lies outside the delivered text")
    prompt = (
        "Re-examine the ink at character location "
        f"[{start}, {end}) of the delivered act. Report only what the ink supports there; "
        "if it supports the existing text, record confirmed unchanged."
    )
    if policy_schema == SCHEMA:
        prompt += (
            " Reply only as JSON with schema `perlector-audit-response.v1` and one ordered edit "
            "for every requested location; each edit repeats its exact original text and gives its replacement."
        )
    elif policy_schema != LEGACY_SCHEMA:
        raise SchemaRefusal("an audit re-proof prompt names an unknown policy schema")
    lowered = prompt.lower()
    if any(fragment in lowered for fragment in FORBIDDEN_PROMPT_FRAGMENTS):
        raise SchemaRefusal("the audit re-proof prompt is not neutral")
    return prompt


def render_reproof_instruction(request: dict[str, Any]) -> str:
    """Render the complete live instruction for one frozen exact-edit request."""
    value = validate_audit_request(request)
    if value["schema"] != REQUEST_SCHEMA:
        raise SchemaRefusal("a legacy audit request has no exact-edit response instrument")
    examples = [
        {
            "class": row["class"],
            "location": dict(row["location"]),
            "original": value["semi_final_text"][row["location"]["start"] : row["location"]["end"]],
            "replacement": value["semi_final_text"][
                row["location"]["start"] : row["location"]["end"]
            ],
        }
        for row in value["reproofs"]
    ]
    return "\n".join(
        [
            "AUDIT RE-PROOF INSTRUMENT (this governs the response format).",
            "Re-examine only the requested character locations against the ink.",
            "Offsets are zero-based Python Unicode code-point offsets into the frozen text; "
            "do not normalize, count UTF-8 bytes, or change any text outside those locations.",
            "Return exactly one edit for every requested row, in the same order. Repeat its "
            "class, exact location, and exact original substring. Put the ink-supported text "
            "in replacement. If unchanged, replacement must equal original.",
            "Reply with only one JSON object. It must have exactly schema and edits; each edit "
            "must have exactly class, location, original, and replacement.",
            "Frozen semi-final text (JSON string):",
            json.dumps(value["semi_final_text"], ensure_ascii=False),
            "Required response object, shown with unchanged replacements:",
            json.dumps(
                {"schema": RESPONSE_SCHEMA, "edits": examples},
                ensure_ascii=False,
                sort_keys=True,
            ),
        ]
    )


# Binds only the renderer and its two labels, so unrelated edits do not
# invalidate retained readings. A renderer change needs an explicit legacy path.
AUDIT_PROMPT_RENDERER_SHA256: Final = digest_of(
    {
        "source": inspect.getsource(render_reproof_instruction),
        "request_schema": REQUEST_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
    }
)


def audit_prompt_evidence(
    *,
    base_prompt: dict[str, Any],
    base_text: str,
    request: dict[str, Any],
    request_sha256: str,
    rendered_text: str,
) -> dict[str, Any]:
    """Seal the exact complete prompt rendered for one live audit request."""
    expected = "\n".join((base_text, render_reproof_instruction(request)))
    if rendered_text != expected:
        raise SchemaRefusal("a live re-proof did not retain the exact audit prompt it rendered")
    value = {
        "schema": AUDIT_PROMPT_SCHEMA,
        "base_prompt": dict(base_prompt),
        "request_digest": audit_digest(request),
        "rendered_text": rendered_text,
        "rendered_sha256": digest_bytes(rendered_text.encode("utf-8")),
        "renderer_sha256": AUDIT_PROMPT_RENDERER_SHA256,
        "request_sha256": request_sha256,
    }
    return validate_audit_prompt_evidence(value, request=request)


def validate_audit_prompt_evidence(
    payload: Any, *, request: dict[str, Any] | None = None
) -> dict[str, Any]:
    value = _closed(payload, _AUDIT_PROMPT_FIELDS, "audit prompt evidence")
    if value["schema"] != AUDIT_PROMPT_SCHEMA or not isinstance(value["base_prompt"], dict):
        raise SchemaRefusal("an audit prompt evidence record has the wrong schema or base prompt")
    if any(
        not is_sha256(value[field])
        for field in ("request_digest", "rendered_sha256", "renderer_sha256", "request_sha256")
    ):
        raise SchemaRefusal("an audit prompt evidence record has a malformed digest")
    if value["renderer_sha256"] != AUDIT_PROMPT_RENDERER_SHA256:
        raise SchemaRefusal("an audit prompt evidence record names another renderer revision")
    rendered = value["rendered_text"]
    if (
        not isinstance(rendered, str)
        or digest_bytes(rendered.encode("utf-8")) != value["rendered_sha256"]
    ):
        raise SchemaRefusal("an audit prompt evidence record disagrees with its rendered text")
    if request is not None:
        instruction = render_reproof_instruction(request)
        suffix = "\n" + instruction
        if not rendered.endswith(suffix):
            raise SchemaRefusal("an audit prompt omits its exact frozen re-proof instruction")
        base_text = rendered[: -len(suffix)]
        if value["request_digest"] != audit_digest(request) or digest_bytes(
            base_text.encode("utf-8")
        ) != value["base_prompt"].get("rendered_sha256"):
            raise SchemaRefusal("an audit prompt disagrees with its base prompt or frozen request")
    return value


def reproof_plan(
    flags: list[dict[str, Any]], *, text_length: int, policy_schema: str = SCHEMA
) -> list[dict[str, Any]]:
    """One neutral, location-only re-proof per frozen flag, in the flags' own order.

    The single definition of what Pass C asks: the producer's seal, the
    delivered request and `validate_chain`'s re-derivation all use it. Locations
    are copied so a reader mutating a delivered row cannot reach the frozen flags.
    """
    return [
        {
            "class": flag["class"],
            "location": {"start": flag["location"]["start"], "end": flag["location"]["end"]},
            "prompt": neutral_prompt(
                start=flag["location"]["start"],
                end=flag["location"]["end"],
                text_length=text_length,
                policy_schema=policy_schema,
            ),
        }
        for flag in flags
    ]


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_span(start: Any, end: Any, text_length: int | None) -> bool:
    return (
        _integer(start)
        and _integer(end)
        and 0 <= start <= end
        and (text_length is None or end <= text_length)
    )


def _closed(payload: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise SchemaRefusal(f"an {label} is not its closed schema")
    return payload


def _location(value: Any, *, text_length: int | None, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise SchemaRefusal(f"an {label} has no closed character location")
    if not _valid_span(value["start"], value["end"], text_length):
        raise SchemaRefusal(f"an {label} lies outside the delivered text")
    return value


def _validate_reproof_rows(
    rows: list[Any], *, text_length: int | None, subject: str, policy_schema: str | None = None
) -> None:
    """The neutrality screen, applied identically wherever a re-proof row appears.

    `text_length=None` is the pre-read pass: the prompt is checked against the
    location's own end, and `validate_chain` re-runs this with the real length.
    A prompt must equal `neutral_prompt` for its location exactly, which leaves
    no room for a sentence telling the reader which way to argue (principle 8).
    """
    for reproof in rows:
        if not isinstance(reproof, dict) or set(reproof) != {"class", "location", "prompt"}:
            raise SchemaRefusal(f"a {subject} re-proof is not its closed schema")
        if (
            not isinstance(reproof["class"], str)
            or reproof["class"] not in FLAG_CLASSES
            or not isinstance(reproof["prompt"], str)
        ):
            raise SchemaRefusal(f"a {subject} re-proof has an unknown class or prompt")
        location = _location(
            reproof["location"], text_length=text_length, label=f"{subject} re-proof"
        )
        prompt_length = text_length if text_length is not None else location["end"]
        schemas = (policy_schema,) if policy_schema is not None else (SCHEMA, LEGACY_SCHEMA)
        allowed = {
            neutral_prompt(
                start=location["start"],
                end=location["end"],
                text_length=prompt_length,
                policy_schema=schema,
            )
            for schema in schemas
        }
        if reproof["prompt"] not in allowed:
            raise SchemaRefusal(f"a {subject} re-proof is not a neutral location-only prompt")


def reproof_delivery_due(flags: list[Any], round_cap: int) -> bool:
    """Whether this act's re-proof request exists: a plan and a round to spend."""
    return bool(flags) and round_cap > 0


def examination_state(
    flags: list[Any],
    round_cap: int,
    reproof_truncation: dict[str, Any] | None,
    *,
    reproof_change_span: tuple[int, int] | None = None,
    flag_text_length: int | None = None,
) -> str:
    """What became of the re-examination the flags required; derived, never chosen.

    `reproof_truncation` is the truncation instrument over the re-proof's own
    response. Text equality is deliberately not an input: it once stood in for
    completion (F1). `reproof_change_span` is the envelope between the frozen
    semi-final and a completed re-proof's text, `None` when there is none;
    `flag_text_length` is required with it for `flag_contains_change`'s slack.
    """
    if not flags:
        if reproof_truncation is not None:
            raise SchemaRefusal("an audit records a re-proof for an act that raised no flag")
        return EXAMINATION_NOT_DUE
    if not reproof_delivery_due(flags, round_cap):
        if reproof_truncation is not None:
            raise SchemaRefusal(
                "an audit records a re-proof termination although its sealed cap left no "
                "round to deliver one"
            )
        return EXAMINATION_CAP_EXHAUSTED
    if reproof_truncation is None:
        raise SchemaRefusal(
            "an audit whose frozen plan and cap delivered a re-proof records no termination "
            "for it; a re-examination with no recorded ending cannot be called complete"
        )
    if reproof_truncation["classification"] == TRUNCATION_COMPLETE:
        if reproof_change_span is not None:
            if flag_text_length is None:
                raise SchemaRefusal(
                    "an audit records a re-proof's change span without the frozen semi-final's "
                    "own length to measure witness slack against"
                )
            start, end = reproof_change_span
            contained = any(
                flag_contains_change(flag, start=start, end=end, before_length=flag_text_length)
                for flag in flags
            )
            if not contained:
                return EXAMINATION_REPROOF_REJECTED
        return EXAMINATION_COMPLETE
    return EXAMINATION_INCOMPLETE


def unresolved_state(examination: str) -> bool:
    """Flags stay unresolved unless a delivered re-proof actually completed.

    An incomplete re-proof discharged nothing, and a rejected one's rewrite was
    never published. `complete` is not a per-flag claim: one call answers every
    flag at once.
    """
    if type(examination) is not str or examination not in EXAMINATION_STATES:
        raise SchemaRefusal(f"{examination!r} is not an audit examination state")
    return examination in {
        EXAMINATION_CAP_EXHAUSTED,
        EXAMINATION_INCOMPLETE,
        EXAMINATION_REPROOF_REJECTED,
    }


def length_signal(*, characters: int, region_pixels: int, page_pixels: int, floor: int) -> bool:
    """The truncation length signal, as a pure function of its four terms.

    The reading's characters, scaled from its region to the page's area, against
    the sealed floor. An empty reading is never suspicious: that outcome is
    `no-readable-text`, decided elsewhere. Producer and validator share this so
    the arithmetic cannot drift.
    """
    return characters > 0 and characters * page_pixels < floor * region_pixels


def truncation_classification(signals: dict[str, Any]) -> str:
    """The truncation instrument's verdict, as a pure function of its four signals.

    The engine's `length` is authoritative for `truncated`; three suspicious
    computed signals are `truncated`; a clean vote under a declared `stop` is
    `complete`; anything else is `unknown`, which holds. Shared with
    `pipeline/4_perlector/truncation.py::classify` so the verdict and every check
    are one rule.
    """
    declared = signals["stop_reason_declared"]
    if declared == "length":
        return TRUNCATION_TRUNCATED
    suspicious = sum(
        bool(signals[name]) for name in ("unclosed_structure", "length_suspicious", "ends_abruptly")
    )
    if suspicious == 3:
        return TRUNCATION_TRUNCATED
    if suspicious == 0 and declared == "stop":
        return TRUNCATION_COMPLETE
    return TRUNCATION_UNKNOWN


def validate_truncation_record(
    value: Any,
    *,
    label: str,
    text: str | None = None,
    length_floor_characters_per_page: int | None = None,
) -> dict[str, Any]:
    """The sealed shape of one raw truncation measurement, its verdict re-derived.

    `length_suspicious` and the classification are re-derived from the record's
    own measure and signals. `text` binds `characters`, and
    `length_floor_characters_per_page` binds the floor, for a caller that holds
    them; without them the record only agrees with itself. Only for raw records
    such as `reproof_truncation`: the Perlectio's own `truncation` is reconciled
    on purpose in `pipeline/4_perlector/run.py` and must not be passed here.
    """
    if not isinstance(value, dict) or set(value) != {"classification", "signals", "measure"}:
        raise SchemaRefusal(f"{label} is not a closed truncation record")
    # Type before membership: an unhashable value would escape as TypeError.
    if type(value["classification"]) is not str or (
        value["classification"] not in TRUNCATION_CLASSIFICATIONS
    ):
        raise SchemaRefusal(f"{label} names an unknown truncation classification")
    signals = value["signals"]
    if not isinstance(signals, dict) or set(signals) != _TRUNCATION_SIGNALS:
        raise SchemaRefusal(f"{label} does not carry the instrument's four signals")
    declared = signals["stop_reason_declared"]
    if declared is not None and (type(declared) is not str or declared not in DECLARED_STOP_WORDS):
        raise SchemaRefusal(
            f"{label} declares stop reason {declared!r}, not one of "
            f"{sorted(DECLARED_STOP_WORDS)} or null"
        )
    for name in ("unclosed_structure", "length_suspicious", "ends_abruptly"):
        if type(signals[name]) is not bool:
            raise SchemaRefusal(f"{label} has a non-boolean {name} signal")
    measure = value["measure"]
    if not isinstance(measure, dict) or set(measure) != _TRUNCATION_MEASURE:
        raise SchemaRefusal(f"{label} does not carry the length signal's closed measure")
    for name in sorted(_TRUNCATION_MEASURE):
        floor = 0 if name in _TRUNCATION_MEASURE_MAY_BE_ZERO else 1
        if type(measure[name]) is not int or measure[name] < floor:
            raise SchemaRefusal(
                f"{label} measure {name} is not a {'non-negative' if floor == 0 else 'positive'} "
                "integer"
            )
    if text is not None and measure["characters"] != len(text):
        raise SchemaRefusal(
            f"{label} measure counts {measure['characters']} characters but the text it was "
            f"measured over has {len(text)}"
        )
    # Refused before the derivation, so the refusal names the floor, not the signal.
    if (
        length_floor_characters_per_page is not None
        and measure["length_floor_characters_per_page"] != length_floor_characters_per_page
    ):
        raise SchemaRefusal(
            f"{label} was judged under length floor "
            f"{measure['length_floor_characters_per_page']} but this run sealed "
            f"{length_floor_characters_per_page}"
        )
    derived_length_signal = length_signal(
        characters=measure["characters"],
        region_pixels=measure["region_pixels"],
        page_pixels=measure["page_pixels"],
        floor=measure["length_floor_characters_per_page"],
    )
    if signals["length_suspicious"] != derived_length_signal:
        raise SchemaRefusal(
            f"{label} claims length_suspicious {signals['length_suspicious']!r} but the geometry "
            f"and floor on its own measure make it {derived_length_signal!r}"
        )
    derived = truncation_classification(signals)
    if value["classification"] != derived:
        raise SchemaRefusal(
            f"{label} claims classification {value['classification']!r} but its own signals "
            f"make it {derived!r}"
        )
    return value


_LEGACY_REPROOF_CALL_FIELDS: Final = frozenset(
    {"call_record_ref", "raw_response_ref", "response_sha256", "finish_reason", "served_model_id"}
)
_REPROOF_CALL_FIELDS: Final = _LEGACY_REPROOF_CALL_FIELDS | {"request_sha256", "audit_prompt"}
# Restated from `pipeline/4_perlector/live_reader.py::_mapped_stop_reason`.
_FINISH_REASON_TO_STOP_WORD: Final = {"stop": "stop", "length": "length", None: None}


def validate_reproof_call(
    value: Any, *, label: str, policy_schema: str = SCHEMA
) -> dict[str, Any] | None:
    """The retained response the sealed re-proof termination was measured over.

    `None` where no engine stands behind the reader (the fixture chamber) or no
    re-proof was delivered.
    """
    if value is None:
        return None
    expected_fields = (
        _REPROOF_CALL_FIELDS if policy_schema == SCHEMA else _LEGACY_REPROOF_CALL_FIELDS
    )
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise SchemaRefusal(f"{label} is not the closed retained-call record")
    validate_input_refs([value["call_record_ref"]])
    validate_input_refs([value["raw_response_ref"]])
    if not is_sha256(value["response_sha256"]):
        raise SchemaRefusal(f"{label} has no response digest")
    # The live client sets `response_sha256` to the raw response's own digest.
    if value["response_sha256"] != value["raw_response_ref"]["sha256"]:
        raise SchemaRefusal(
            f"{label} names response digest {value['response_sha256']} but its retained raw "
            f"response is {value['raw_response_ref']['sha256']}"
        )
    if value["finish_reason"] is not None and type(value["finish_reason"]) is not str:
        raise SchemaRefusal(f"{label} has a malformed finish reason")
    if value["finish_reason"] not in _FINISH_REASON_TO_STOP_WORD:
        raise SchemaRefusal(
            f"{label} names finish reason {value['finish_reason']!r}, which no reader maps to "
            "a stop word"
        )
    if not isinstance(value["served_model_id"], str) or not value["served_model_id"]:
        raise SchemaRefusal(f"{label} names no served model")
    if policy_schema == SCHEMA:
        if not is_sha256(value["request_sha256"]):
            raise SchemaRefusal(f"{label} names no request digest")
        validate_audit_prompt_evidence(value["audit_prompt"])
        if value["audit_prompt"]["request_sha256"] != value["request_sha256"]:
            raise SchemaRefusal(f"{label}'s audit prompt names another chair request")
    return value


def _refuse_call_verdict_disagreement(call: dict[str, Any], termination: dict[str, Any]) -> None:
    """Refuse a sealed stop word that disagrees with the retained call's finish reason.

    Otherwise a finding could seal `complete` beside a call whose engine said
    `length`.
    """
    expected = _FINISH_REASON_TO_STOP_WORD[call["finish_reason"]]
    declared = termination["signals"]["stop_reason_declared"]
    if expected != declared:
        raise SchemaRefusal(
            f"an audit finding's re-proof call finished {call['finish_reason']!r} but its "
            f"sealed termination declares stop word {declared!r}"
        )


def _validate_live_reproof_request(
    tree: Any,
    reading: dict[str, Any],
    call_evidence: dict[str, Any],
    request: dict[str, Any],
) -> None:
    """Rebuild the exact Perlector request body that carried the audit prompt."""
    prompt = validate_audit_prompt_evidence(call_evidence["audit_prompt"], request=request)
    if call_evidence["request_sha256"] != prompt["request_sha256"]:
        raise SchemaRefusal("an audit re-proof call and prompt name different requests")
    for name in ("call_record_ref", "raw_response_ref"):
        if call_evidence[name] not in reading.get("inputs", []):
            raise SchemaRefusal(f"an audit re-proof does not bind its {name} as a direct input")
    call_bytes = tree.read_bytes(call_evidence["call_record_ref"]["relative_path"])
    if digest_bytes(call_bytes) != call_evidence["call_record_ref"]["sha256"]:
        raise SchemaRefusal("an audit re-proof call record reference disagrees with its bytes")
    try:
        call = json.loads(call_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaRefusal("an audit re-proof call record is not JSON") from error
    raw_bytes = tree.read_bytes(call_evidence["raw_response_ref"]["relative_path"])
    if digest_bytes(raw_bytes) != call_evidence["raw_response_ref"]["sha256"]:
        raise SchemaRefusal("an audit re-proof raw response reference disagrees with its bytes")
    if (
        not isinstance(call, dict)
        or call.get("schema") not in CHAIR_CALL_RECORD_SCHEMAS
        or call.get("request_sha256") != prompt["request_sha256"]
        or call.get("raw_response_ref") != call_evidence["raw_response_ref"]
        or call.get("served_model_id") != call_evidence["served_model_id"]
        or call.get("parse_problem") is not None
        or call.get("response_model") != call_evidence["served_model_id"]
        or (call.get("schema") == CHAIR_CALL_RECORD_SCHEMA and call.get("response_status") != 200)
    ):
        raise SchemaRefusal("an audit re-proof prompt is not bound to its successful chair call")
    dossier = reading["payload"].get("dossier")
    autopsia = dossier.get("cross_capture_autopsia") if isinstance(dossier, dict) else None
    views = autopsia.get("views") if isinstance(autopsia, dict) else None
    if not isinstance(views, list) or any(
        not isinstance(view, dict)
        or not isinstance(view.get("page_render_refs"), list)
        or not isinstance(view.get("region_refs"), list)
        for view in views
    ):
        raise SchemaRefusal("an audit re-proof has no atomic presentation to rebuild its request")
    page_refs = [ref for view in views for ref in view.get("page_render_refs", [])]
    region_refs = [ref for view in views for ref in view.get("region_refs", [])]

    def image_part(reference: dict[str, str]) -> dict[str, Any]:
        data = tree.read_bytes(reference["relative_path"])
        if digest_bytes(data) != reference["sha256"]:
            raise SchemaRefusal("an audit re-proof image reference disagrees with its bytes")
        return {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode("ascii")},
        }

    content = [image_part(ref) for ref in page_refs]
    content.append({"type": "text", "text": prompt["rendered_text"]})
    content.extend(image_part(ref) for ref in region_refs)
    image_sha256s = [ref["sha256"] for ref in page_refs + region_refs]
    receipt = tree.read_run_receipt(call["receipt_ref"])
    body = _rebuild_chair_request_bytes(
        recorded_generation=call.get("generation_sent"),
        messages=[{"role": "user", "content": content}],
        model_id=call["served_model_id"],
        seed=receipt["seed"],
    )
    if call.get("image_sha256s") != image_sha256s or digest_bytes(body) != prompt["request_sha256"]:
        raise SchemaRefusal("an audit re-proof prompt does not reproduce its retained request")


def _decode_recorded_generation(value: Any) -> Any:
    """Restore the JSON-native generation values retained by ChairClient.

    Call records replace native floats with their exact shortest wire decimal
    because canonical artifacts reject floats.  Request validation must undo
    that transcription before rebuilding the HTTP bytes; serializing the tag
    itself proves a different request and rejects every legitimate float.
    """
    if isinstance(value, dict):
        if set(value) == WIRE_DECIMAL_FIELDS and value.get("schema") == WIRE_DECIMAL_SCHEMA:
            decimal = value.get("decimal")
            if not isinstance(decimal, str):
                raise SchemaRefusal("an audit re-proof call has a malformed wire decimal")
            try:
                decoded = float(decimal)
            except ValueError as error:
                raise SchemaRefusal(
                    "an audit re-proof call has a malformed wire decimal"
                ) from error
            if not math.isfinite(decoded) or json.dumps(decoded) != decimal:
                raise SchemaRefusal("an audit re-proof call has a non-canonical wire decimal")
            return decoded
        return {key: _decode_recorded_generation(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_recorded_generation(item) for item in value]
    return value


def _rebuild_chair_request_bytes(
    *, recorded_generation: Any, messages: list[dict[str, Any]], model_id: str, seed: int
) -> bytes:
    """Rebuild the exact compact, sorted JSON bytes ChairClient sent."""
    if not isinstance(recorded_generation, dict):
        raise SchemaRefusal("an audit re-proof call has no recorded generation object")
    generation = _decode_recorded_generation(recorded_generation)
    retained_temperature = generation.pop("temperature", 0)
    retained_seed = generation.pop("seed", seed)
    if type(retained_temperature) is not int or retained_temperature != 0:
        raise SchemaRefusal("an audit re-proof call retained another temperature")
    if type(retained_seed) is not int or retained_seed != seed:
        raise SchemaRefusal("an audit re-proof call retained another seed")
    if set(generation) & {"model", "stream", "n"}:
        raise SchemaRefusal("an audit re-proof call puts a manager-owned field in generation_sent")
    body = {
        **generation,
        "messages": messages,
        "model": model_id,
        "stream": False,
        "temperature": 0,
        "seed": seed,
    }
    try:
        return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise SchemaRefusal("an audit re-proof call cannot reproduce its wire request") from error


def audit_request(
    *,
    act_key: str,
    attempt_ordinal: int,
    draft_ref: dict[str, str],
    semi_final_text: str,
    flags: list[dict[str, Any]],
    policy_schema: str = SCHEMA,
) -> dict[str, Any]:
    """The closed instrument Pass C hands the reader, built from the frozen draft.

    Everything in it derives from the draft and its reference, so
    `validate_chain` can rebuild it and compare digests. It carries no witness
    material, ranking or wanted reading, and its field set is closed.
    """
    request = {
        "schema": REQUEST_SCHEMA if policy_schema == SCHEMA else LEGACY_REQUEST_SCHEMA,
        "act_key": act_key,
        "attempt_ordinal": attempt_ordinal,
        "draft_ref": dict(draft_ref),
        "semi_final_text": semi_final_text,
        "reproofs": reproof_plan(
            flags, text_length=len(semi_final_text), policy_schema=policy_schema
        ),
    }
    return validate_audit_request(request)


def validate_audit_request(payload: Any) -> dict[str, Any]:
    """Refuse an audit request at the seam, in the producer and in the reader alike."""
    value = _closed(payload, _AUDIT_REQUEST_FIELDS, "audit request")
    if value["schema"] == REQUEST_SCHEMA:
        policy_schema = SCHEMA
    elif value["schema"] == LEGACY_REQUEST_SCHEMA:
        policy_schema = LEGACY_SCHEMA
    else:
        raise SchemaRefusal("an audit request does not declare the audit-request schema")
    if not isinstance(value["act_key"], str) or not value["act_key"]:
        raise SchemaRefusal("an audit request has no act identity")
    if not _integer(value["attempt_ordinal"]) or value["attempt_ordinal"] < 1:
        raise SchemaRefusal("an audit request has no integer attempt ordinal")
    validate_input_refs([value["draft_ref"]])
    # `validate_input_refs` ignores extra keys, which would ride into the digest
    # unscreened.
    if set(value["draft_ref"]) != {"relative_path", "sha256"}:
        raise SchemaRefusal("an audit request's draft reference is not its closed shape")
    if not isinstance(value["semi_final_text"], str):
        raise SchemaRefusal(
            "an audit request carries no frozen semi-final text; a re-proof cannot confirm "
            "what it was not shown"
        )
    if not isinstance(value["reproofs"], list) or not value["reproofs"]:
        raise SchemaRefusal(
            "an audit request delivers no re-proof location; a request asking the reader to "
            "re-examine nothing would seal a measurement nobody could have made"
        )
    _validate_reproof_rows(
        value["reproofs"],
        text_length=len(value["semi_final_text"]),
        subject="audit request",
        policy_schema=policy_schema,
    )
    return value


def reproof_response_from_text(request: dict[str, Any], proposed_text: str) -> str:
    """Render a fixture re-proof as the same exact-edit reply a live chair owes.

    Fixture declarations predate the edit protocol and name a proposed full
    reading.  This adapter does not make that legacy convenience permissive:
    it can encode it only when its one change envelope fits one flagged span.
    An escaping fixture proposal is deliberately rendered with its escaping
    span so :func:`assemble_reproof_response` rejects it just as a live reply
    would.  Python string offsets are Unicode code-point offsets throughout;
    no byte slicing or normalization is introduced between the frozen draft
    and the assembled text.
    """
    request = validate_audit_request(request)
    before = request["semi_final_text"]
    rows = [
        {
            "class": row["class"],
            "location": dict(row["location"]),
            "original": before[row["location"]["start"] : row["location"]["end"]],
            "replacement": before[row["location"]["start"] : row["location"]["end"]],
        }
        for row in request["reproofs"]
    ]
    if proposed_text != before:
        start, end = text_change_span(before, proposed_text)
        matching = next(
            (
                row
                for row in rows
                if row["location"]["start"] <= start and end <= row["location"]["end"]
            ),
            None,
        )
        if matching is None:
            # Keep the escaping span verbatim for the refusal record.
            matching = rows[0]
            matching["location"] = {"start": start, "end": end}
            matching["original"] = before[start:end]
        else:
            location = matching["location"]
            # `end` indexes the frozen text; translate it into the proposed text.
            proposed_end = len(proposed_text) - (len(before) - end)
            matching["replacement"] = (
                before[location["start"] : start]
                + proposed_text[start:proposed_end]
                + before[end : location["end"]]
            )
    return json.dumps(
        {"schema": RESPONSE_SCHEMA, "edits": rows}, ensure_ascii=False, sort_keys=True
    )


def assemble_reproof_response(
    raw_response: str, request: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Validate an exact-edit reply and splice it into the frozen draft.

    Every requested location must be answered in its delivered order, with the
    exact source substring repeated.  Changed locations may not overlap: an
    overlapping pair has no deterministic composition order and is held rather
    than guessed.  The function returns both the assembled act and the parsed
    proposal so callers can retain the proposed/original evidence beside the
    frozen audit draft.
    """
    request = validate_audit_request(request)
    if request["schema"] != REQUEST_SCHEMA:
        raise ReproofResponseRefusal(
            "an audit re-proof edit response cannot be applied to a legacy request; resume under "
            "one sealed audit version or hold the act rather than mixing response contracts"
        )
    if not isinstance(raw_response, str):
        raise ReproofResponseRefusal("an audit re-proof response is not text")
    try:
        response = json.loads(raw_response, object_pairs_hook=_closed_json_object)
    except json.JSONDecodeError as error:
        raise ReproofResponseRefusal("an audit re-proof response is not valid JSON") from error
    if not isinstance(response, dict) or set(response) != {"schema", "edits"}:
        raise ReproofResponseRefusal("an audit re-proof response is not its closed schema")
    if response["schema"] != RESPONSE_SCHEMA or not isinstance(response["edits"], list):
        raise ReproofResponseRefusal("an audit re-proof response names an unknown schema or edits")
    expected = request["reproofs"]
    if len(response["edits"]) != len(expected):
        raise ReproofResponseRefusal(
            "an audit re-proof response does not answer every flagged span"
        )
    assembled = assemble_reproof_edits(
        response["edits"], before=request["semi_final_text"], planned=expected
    )
    return assembled, response


def assemble_reproof_edits(
    response_edits: Any, *, before: str, planned: list[dict[str, Any]]
) -> str:
    """Validate and deterministically splice already-parsed exact edits."""
    if not isinstance(response_edits, list) or len(response_edits) != len(planned):
        raise ReproofResponseRefusal(
            "an audit re-proof response does not answer every flagged span"
        )
    edits_by_location: dict[tuple[int, int], dict[str, Any]] = {}
    answers_by_location: dict[tuple[int, int], str] = {}
    for proposed, expected in zip(response_edits, planned, strict=True):
        if not isinstance(proposed, dict) or set(proposed) != {
            "class",
            "location",
            "original",
            "replacement",
        }:
            raise ReproofResponseRefusal("an audit re-proof edit is not its closed schema")
        if proposed["class"] != expected["class"]:
            raise ReproofResponseRefusal("an audit re-proof edit names the wrong flagged class")
        location = proposed["location"]
        if (
            not isinstance(location, dict)
            or set(location) != {"start", "end"}
            or not _valid_span(location["start"], location["end"], len(before))
        ):
            raise ReproofResponseRefusal("an audit re-proof edit has invalid character bounds")
        if location != expected["location"]:
            raise ReproofResponseRefusal(
                "an audit re-proof edit does not repeat its exact requested location"
            )
        start, end = location["start"], location["end"]
        if not isinstance(proposed["original"], str) or not isinstance(
            proposed["replacement"], str
        ):
            raise ReproofResponseRefusal(
                "an audit re-proof edit has non-text original or replacement"
            )
        if proposed["original"] != before[start:end]:
            raise ReproofResponseRefusal(
                "an audit re-proof edit does not repeat the exact frozen text"
            )
        key = (start, end)
        prior_answer = answers_by_location.setdefault(key, proposed["replacement"])
        if prior_answer != proposed["replacement"]:
            raise ReproofResponseRefusal(
                "an audit re-proof response gives contradictory answers for one exact span"
            )
        if proposed["replacement"] != proposed["original"]:
            edits_by_location.setdefault(
                key,
                {
                    "start": start,
                    "end": end,
                    "replacement": proposed["replacement"],
                },
            )
    edits = list(edits_by_location.values())
    edits.sort(key=lambda row: (row["start"], row["end"]))
    for previous, current in zip(edits, edits[1:], strict=False):
        if current["start"] < previous["end"]:
            raise ReproofResponseRefusal(
                "an audit re-proof response proposes overlapping changed spans"
            )
    pieces: list[str] = []
    cursor = 0
    for edit in edits:
        pieces.extend((before[cursor : edit["start"]], edit["replacement"]))
        cursor = edit["end"]
    pieces.append(before[cursor:])
    return "".join(pieces)


def change_records_from_edits(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One audit change record for each changed anchored response edit."""
    return [
        {
            "start": edit["location"]["start"],
            "end": edit["location"]["end"],
            "triggering_flag_class": edit["class"],
        }
        for edit in edits
        if edit["replacement"] != edit["original"]
    ]


def _validate_common(value: dict[str, Any], *, text_length: int) -> None:
    if (
        not isinstance(value["act_key"], str)
        or not value["act_key"]
        or not isinstance(value["page_ids"], list)
        or not value["page_ids"]
        or any(not isinstance(page_id, str) or not page_id for page_id in value["page_ids"])
        or len(value["page_ids"]) != len(set(value["page_ids"]))
        or value["page_ids"] != sorted(value["page_ids"])
    ):
        raise SchemaRefusal("an audit record has no act identity or canonical page set")
    if not _integer(value["attempt_ordinal"]) or value["attempt_ordinal"] < 1:
        raise SchemaRefusal("an audit record has no integer attempt ordinal")
    if not _integer(value["round_cap"]) or value["round_cap"] < 0:
        raise SchemaRefusal("an audit record has no integer round cap")
    if not isinstance(value["policy"], dict) or set(value["policy"]) != {
        "schema",
        "sha256",
        "approval_ref",
    }:
        raise SchemaRefusal("an audit record has no sealed policy reference")
    if type(value["policy"]["schema"]) is str and value["policy"]["schema"] in RETIRED_SCHEMAS:
        raise SchemaRefusal(
            f"an audit record was sealed under {value['policy']['schema']}, which could not "
            "record whether a delivered re-proof completed; it is refused rather than read "
            f"forward, and its act is re-read from the sealed evidence under {SCHEMA} in a "
            "new run. The old bytes are evidence and stay as written"
        )
    if (
        type(value["policy"]["schema"]) is not str
        or value["policy"]["schema"] not in {SCHEMA, LEGACY_SCHEMA}
        or not is_sha256(value["policy"]["sha256"])
        or not isinstance(value["policy"]["approval_ref"], str)
    ):
        raise SchemaRefusal("an audit record has a malformed sealed policy reference")
    if not isinstance(value["flags"], list):
        raise SchemaRefusal("an audit record has no flag list")
    for flag in value["flags"]:
        if not isinstance(flag, dict) or set(flag) != {"class", "location"}:
            raise SchemaRefusal("an audit flag is not its closed schema")
        if not isinstance(flag["class"], str) or flag["class"] not in FLAG_CLASSES:
            raise SchemaRefusal("an audit flag has an unknown class or malformed location")
        _location(flag["location"], text_length=text_length, label="audit flag")
    if value["policy"]["schema"] == SCHEMA:
        identities = [
            (flag["class"], flag["location"]["start"], flag["location"]["end"])
            for flag in value["flags"]
        ]
        if len(identities) != len(set(identities)):
            raise SchemaRefusal("an audit record repeats an identical flag")


def validate_draft(payload: Any) -> dict[str, Any]:
    value = _closed(payload, _DRAFT_FIELDS, "audit draft")
    refuse_capture_preference(value, what="an audit draft")
    if not isinstance(value["semi_final_text"], str):
        raise SchemaRefusal("an audit draft has no semi-final text")
    _validate_common(value, text_length=len(value["semi_final_text"]))
    basis = value["flag_location_basis"]
    if not isinstance(basis, list) or any(
        not isinstance(row, dict)
        or set(row) != {"class", "chair", "derivation", "location"}
        or not isinstance(row["class"], str)
        or row["class"] not in WITNESS_DERIVED_LOCATION_CLASSES
        or not isinstance(row["chair"], str)
        or not row["chair"]
        or not isinstance(row["derivation"], str)
        or row["derivation"] not in {"own-report", "page-slice"}
        for row in basis
    ):
        raise SchemaRefusal(
            "the audit draft has no closed witness-derived flag-location basis. "
            "A testimony-diff location cannot be traced to the testimony that located it. "
            "Rebuild the draft with class, chair, derivation, and location for each such flag."
        )
    for row in basis:
        _location(row["location"], text_length=len(value["semi_final_text"]), label="basis")
    # Bound by location, not list position: one chair may locate two flags, and
    # two chairs one.
    identities = [
        (row["chair"], row["derivation"], row["location"]["start"], row["location"]["end"])
        for row in basis
    ]
    if len(identities) != len(set(identities)):
        raise SchemaRefusal(
            "the audit draft repeats a witness-derived flag-location basis. "
            "One witness would be recorded twice as the source of one location. "
            "Remove the duplicate basis row and rebuild the draft."
        )
    flagged = {
        (flag["location"]["start"], flag["location"]["end"])
        for flag in value["flags"]
        if flag["class"] in WITNESS_DERIVED_LOCATION_CLASSES
    }
    located = {(row["location"]["start"], row["location"]["end"]) for row in basis}
    if located != flagged:
        raise SchemaRefusal(
            "the audit draft's testimony-diff flags and witness-derived location basis disagree. "
            "At least one witness-derived flag or its source would be unaccounted. "
            "Rebuild both lists from the same frozen dossier comparison."
        )
    return value


def validate_finding(
    payload: Any,
    *,
    text: str,
    flag_text: str | None = None,
    length_floor_characters_per_page: int | None = None,
) -> dict[str, Any]:
    policy_schema = (
        payload["policy"].get("schema")
        if isinstance(payload, dict) and isinstance(payload.get("policy"), dict)
        else None
    )
    value = _closed(
        payload,
        _FINDING_FIELDS_V3 if policy_schema == SCHEMA else _FINDING_FIELDS_V2,
        "audit finding",
    )
    refuse_capture_preference(value, what="an audit finding")
    if not isinstance(text, str):
        raise SchemaRefusal("an audit finding was validated without its final text")
    if flag_text is not None and not isinstance(flag_text, str):
        raise SchemaRefusal("an audit finding was validated without its frozen flag text")
    flag_text_length = len(flag_text) if flag_text is not None else len(text)
    _validate_common(value, text_length=flag_text_length)
    if not isinstance(value["change_record"], list) or not isinstance(
        value["uncertain_spans"], list
    ):
        raise SchemaRefusal("an audit finding has malformed change or uncertainty records")
    if not isinstance(value["unresolved"], bool):
        raise SchemaRefusal("an audit finding does not say whether flags remain unresolved")
    for change in value["change_record"]:
        if not isinstance(change, dict) or set(change) != {"start", "end", "triggering_flag_class"}:
            raise SchemaRefusal("an audit change record is not its closed schema")
        if (
            not isinstance(change["triggering_flag_class"], str)
            or change["triggering_flag_class"] not in FLAG_CLASSES
        ):
            raise SchemaRefusal("an audit change record names an unknown triggering flag class")
        _location(
            {"start": change["start"], "end": change["end"]},
            text_length=flag_text_length,
            label="audit change record",
        )
    for span in value["uncertain_spans"]:
        if not isinstance(span, dict) or set(span) != {"start", "end", "reason"}:
            raise SchemaRefusal("an audit uncertainty span is not its closed schema")
        _location(
            {"start": span["start"], "end": span["end"]},
            text_length=len(text),
            label="audit uncertainty span",
        )
        if span["start"] == span["end"] or span["reason"] != AUDIT_CAP_EXHAUSTED:
            raise SchemaRefusal("an audit uncertainty span has no exhausted-cap reason or width")
    if policy_schema == SCHEMA:
        reproof_change_span = None
        edits = value["reproof_edits"]
        delivered = value["reproof_truncation"] is not None
        if delivered != (edits is not None):
            raise SchemaRefusal(
                "an audit finding carries exact re-proof edits exactly when a re-proof was delivered"
            )
        if edits is not None:
            if flag_text is None:
                raise SchemaRefusal(
                    "an audit finding's exact re-proof edits require the frozen semi-final text"
                )
            planned = reproof_plan(value["flags"], text_length=len(flag_text), policy_schema=SCHEMA)
            try:
                assembled = assemble_reproof_edits(edits, before=flag_text, planned=planned)
            except ReproofResponseRefusal as error:
                raise SchemaRefusal(
                    f"an audit finding has invalid exact re-proof edits: {error}"
                ) from error
            projected = "" if assembled.strip() == "" else assembled
            if projected != text:
                raise SchemaRefusal(
                    "an audit finding's exact re-proof edits do not assemble to its published text"
                )
            if value["change_record"] != change_records_from_edits(edits):
                raise SchemaRefusal(
                    "an audit finding's change records do not account for each changed exact edit"
                )
        else:
            if value["change_record"]:
                raise SchemaRefusal("an audit finding without a re-proof carries a change record")
            if flag_text is not None and text != flag_text:
                raise SchemaRefusal(
                    "an audit finding without a re-proof does not preserve the frozen semi-final text"
                )
        examination = examination_state(
            value["flags"], value["round_cap"], value["reproof_truncation"]
        )
    else:
        reproof_change_span = None
        span = value["reproof_change_span"]
        if span is not None:
            _location(
                span, text_length=flag_text_length, label="an audit finding's reproof change span"
            )
            reproof_change_span = (span["start"], span["end"])
        examination = examination_state(
            value["flags"],
            value["round_cap"],
            value["reproof_truncation"],
            reproof_change_span=reproof_change_span,
            flag_text_length=flag_text_length,
        )
    if value["reproof_truncation"] is not None:
        # A refused re-proof published the frozen text, not the response the
        # termination measured, so its character count cannot be bound to `text`.
        refused = (
            policy_schema == LEGACY_SCHEMA
            and reproof_change_span is not None
            and flag_text is not None
            and text == flag_text
        )
        validate_truncation_record(
            value["reproof_truncation"],
            label="an audit finding's re-proof termination",
            text=None if refused else text,
            length_floor_characters_per_page=length_floor_characters_per_page,
        )
    validate_reproof_call(
        value["reproof_call"],
        label="an audit finding's re-proof call",
        policy_schema=policy_schema,
    )
    if value["reproof_call"] is not None:
        if value["reproof_truncation"] is None:
            raise SchemaRefusal(
                "an audit finding names a re-proof call although no re-proof was delivered"
            )
        _refuse_call_verdict_disagreement(value["reproof_call"], value["reproof_truncation"])
    if value["examination"] != examination:
        raise SchemaRefusal(
            f"an audit finding claims examination {value['examination']!r} but its flags, cap, "
            f"re-proof termination and change span make it {examination!r}"
        )
    if value["unresolved"] != unresolved_state(examination):
        raise SchemaRefusal(
            "an audit finding's unresolved state contradicts its examination; a re-proof that "
            "did not complete resolves nothing, however its text compares"
        )
    if bool(value["uncertain_spans"]) and examination != EXAMINATION_CAP_EXHAUSTED:
        raise SchemaRefusal(
            "an audit finding carries exhausted-cap uncertainty although its cap was not "
            "exhausted; an incomplete re-proof is recorded as an incomplete examination, "
            "never as a span"
        )
    # One direction only: an unchanged re-proof seals no span, but a cut-off one
    # may depart and still carry one.
    if (
        policy_schema == LEGACY_SCHEMA
        and reproof_change_span is not None
        and value["reproof_truncation"] is None
    ):
        raise SchemaRefusal(
            "an audit finding's reproof change span exists only when a re-proof was delivered"
        )
    if policy_schema == LEGACY_SCHEMA and examination == EXAMINATION_REPROOF_REJECTED:
        if reproof_change_span is None:
            raise SchemaRefusal(
                "an audit finding claims a rejected re-proof without the change span that "
                "would show what escaped every flag"
            )
        if value["change_record"] != []:
            raise SchemaRefusal(
                "an audit finding's re-proof was rejected, so it published no change; a "
                "rejected re-proof's change record is empty by construction"
            )
        if flag_text is not None and text != flag_text:
            raise SchemaRefusal(
                "an audit finding's re-proof was rejected, so the published text is the "
                "frozen semi-final, not the rejected rewrite"
            )
    return value


def validate_perlectio_audit(record: Any, *, text_length: int | None) -> dict[str, Any]:
    if isinstance(record, dict) and set(record) == _PERLECTIO_AUDIT_FIELDS - {"examination"}:
        raise SchemaRefusal(
            "a Perlectio audit record carries the perlector-audit.v1 field set (no "
            "`examination`), so it cannot say whether a delivered re-proof completed; it is "
            f"refused rather than read forward, and its act is re-read under {SCHEMA} in a new "
            "run. The old bytes are evidence and stay as written"
        )
    value = _closed(record, _PERLECTIO_AUDIT_FIELDS, "Perlectio audit record")
    # Aliasing between the two is named by `validate_chain`'s kind-specific reads.
    validate_input_refs([value["draft_ref"]])
    validate_input_refs([value["finding_ref"]])
    if not is_sha256(value["finding_digest"]):
        raise SchemaRefusal("a Perlectio audit record has no finding payload digest")
    if not isinstance(value["unresolved"], bool) or not isinstance(value["reproofs"], list):
        raise SchemaRefusal("a Perlectio audit record has malformed resolution facts")
    if type(value["examination"]) is not str or value["examination"] not in EXAMINATION_STATES:
        raise SchemaRefusal("a Perlectio audit record names an unknown examination state")
    if value["unresolved"] != unresolved_state(value["examination"]):
        raise SchemaRefusal(
            "a Perlectio audit record's unresolved state contradicts its examination state"
        )
    if (value["examination"] == EXAMINATION_NOT_DUE) != (value["reproofs"] == []):
        raise SchemaRefusal(
            "a Perlectio audit record's examination contradicts its re-proof plan: `not-due` "
            "means no flag and therefore no plan, and a plan means a flag"
        )
    if (value["examination"] in {EXAMINATION_NOT_DUE, EXAMINATION_CAP_EXHAUSTED}) != (
        value["request_digest"] is None
    ):
        raise SchemaRefusal(
            "a Perlectio audit record's examination contradicts its delivery: a request digest "
            "exists exactly when a re-proof was delivered"
        )
    if value["request_digest"] is not None and not is_sha256(value["request_digest"]):
        raise SchemaRefusal("a Perlectio audit record has no delivered audit-request digest")
    _validate_reproof_rows(value["reproofs"], text_length=text_length, subject="Perlectio audit")
    return value


def text_change_span(before: str, after: str) -> tuple[int, int]:
    """Smallest semi-final span affected by an exact textual comparison."""
    start = 0
    limit = min(len(before), len(after))
    while start < limit and before[start] == after[start]:
        start += 1
    end = len(before)
    after_end = len(after)
    while end > start and after_end > start and before[end - 1] == after[after_end - 1]:
        end -= 1
        after_end -= 1
    return start, end


def change_record(before: str, after: str, flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attribute the re-proof's change to the narrowest flag that contains it.

    The triggering class makes witness-diff-triggered changes computable from
    the tree. Flags arrive sorted by start, and the cross-act classes span the
    whole text, so first-listed would always credit the widest flag. Width ties
    break on `(start, class)`, a function of the frozen flags alone, because
    consumers re-derive this record exactly. An envelope escaping every single
    flag is refused rather than decomposed by a diff heuristic.

    A witness-derived flag's end is itself suffix-trimmed against its testimony,
    so it can land one character short of the text's true end when the two
    strings share a last character. `flag_contains_change` allows exactly that
    one character, and only for a change that starts inside the flag.
    """
    if before == after:
        return []
    start, end = text_change_span(before, after)
    containing = [
        flag
        for flag in flags
        if flag_contains_change(flag, start=start, end=end, before_length=len(before))
    ]
    if not containing:
        raise SchemaRefusal("an audit re-proof changed text outside every flagged location")
    triggering = min(
        containing,
        key=lambda flag: (
            flag["location"]["end"] - flag["location"]["start"],
            flag["location"]["start"],
            flag["class"],
        ),
    )
    return [{"start": start, "end": end, "triggering_flag_class": triggering["class"]}]


def flag_contains_change(flag: dict[str, Any], *, start: int, end: int, before_length: int) -> bool:
    """Whether one flag covers a `[start, end)` change envelope.

    Shared by `change_record`, `examination_state` and `validate_finding`.
    """
    location = flag["location"]
    if location["start"] > start:
        return False
    if end <= location["end"]:
        return True
    # The one-character slack `change_record` documents.
    return (
        flag["class"] in WITNESS_DERIVED_LOCATION_CLASSES
        and start < location["end"]
        and end == before_length
        and end - location["end"] == 1
    )


def validate_chain(
    tree,
    reading: dict[str, Any],
    act_id: str,
    *,
    length_floor_characters_per_page: int | None = None,
) -> dict[str, Any]:
    """Validate the exact draft/finding/Perlectio relationship once for every reader.

    `length_floor_characters_per_page` is the sealed `[truncation]` floor, from a
    caller that holds the protocol bytes; `None` is a caller that does not (the
    Recensor, the fixture chamber), a declared absence.

    Known limit: under an `assessed` uncertainty state this proves only that the
    exhausted-cap projection leads `uncertain_spans`. The tail is the reader's
    own report, which no artifact holds separately, so its provenance is not
    proven here.
    """
    payload = reading.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise SchemaRefusal(f"reading of {act_id} has no final text for its Pass-C audit")
    record = validate_perlectio_audit(payload.get("audit"), text_length=None)
    draft = tree.read_artifact_reference(
        record["draft_ref"], stage=PERLECTOR, kind="audit-draft", subject_id=act_id
    )
    finding = tree.read_artifact_reference(
        record["finding_ref"], stage=PERLECTOR, kind="audit-finding", subject_id=act_id
    )
    draft_payload = validate_draft(draft.get("payload"))
    validate_perlectio_audit(record, text_length=len(draft_payload["semi_final_text"]))
    finding_payload = validate_finding(
        finding.get("payload"),
        text=payload["text"],
        flag_text=draft_payload["semi_final_text"],
        length_floor_characters_per_page=length_floor_characters_per_page,
    )
    shared_fields = (
        "act_key",
        "attempt_ordinal",
        "page_ids",
        "round_cap",
        "policy",
        "flags",
    )
    if any(draft_payload[field] != finding_payload[field] for field in shared_fields):
        raise SchemaRefusal(f"audit draft and finding for {act_id} restate different frozen facts")
    basis_page_ids = _basis_page_ids(payload, act_id)
    if draft_payload["page_ids"] != basis_page_ids:
        raise SchemaRefusal(
            f"audit page set for {act_id} disagrees with the reading's sealed region basis; "
            "the finding omits or invents page evidence; rebuild it from the sealed regions"
        )
    if draft_payload["act_key"] != payload.get("act_key") or draft_payload[
        "attempt_ordinal"
    ] != payload.get("attempt_ordinal"):
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit identity")
    if draft_payload["policy"]["schema"] == SCHEMA:
        expected_changes = (
            change_records_from_edits(finding_payload["reproof_edits"])
            if finding_payload["reproof_edits"] is not None
            else []
        )
    else:
        expected_changes = change_record(
            draft_payload["semi_final_text"], payload["text"], draft_payload["flags"]
        )
    if finding_payload["change_record"] != expected_changes:
        raise SchemaRefusal(f"reading of {act_id} disagrees with its exact audit change record")
    if record["finding_digest"] != audit_digest(finding_payload):
        raise SchemaRefusal(f"reading of {act_id} names an audit finding with a mismatched digest")
    if record["unresolved"] != finding_payload["unresolved"]:
        raise SchemaRefusal(f"reading of {act_id} contradicts its audit finding's unresolved state")
    # Defence in depth: kept because it names the exact fact.
    if record["examination"] != finding_payload["examination"]:
        raise SchemaRefusal(
            f"reading of {act_id} contradicts its audit finding's examination state"
        )
    if finding["inputs"] != [record["draft_ref"]]:
        raise SchemaRefusal(f"audit finding for {act_id} does not bind exactly its audit draft")
    if record["draft_ref"] not in reading.get("inputs", []) or record[
        "finding_ref"
    ] not in reading.get("inputs", []):
        raise SchemaRefusal(f"reading of {act_id} does not bind both audit artifacts as inputs")
    policy_schema = draft_payload["policy"]["schema"]
    expected_reproofs = reproof_plan(
        draft_payload["flags"],
        text_length=len(draft_payload["semi_final_text"]),
        policy_schema=policy_schema,
    )
    if record["reproofs"] != expected_reproofs:
        raise SchemaRefusal(f"reading of {act_id} does not retain exactly its frozen re-proof plan")
    # A request exists exactly when there was a plan and a round to spend.
    if reproof_delivery_due(draft_payload["flags"], draft_payload["round_cap"]):
        expected_request = audit_request(
            act_key=draft_payload["act_key"],
            attempt_ordinal=draft_payload["attempt_ordinal"],
            draft_ref=record["draft_ref"],
            semi_final_text=draft_payload["semi_final_text"],
            flags=draft_payload["flags"],
            policy_schema=policy_schema,
        )
        if record["request_digest"] != audit_digest(expected_request):
            raise SchemaRefusal(
                f"reading of {act_id} does not name the exact audit request its frozen "
                "re-proof plan renders"
            )
        if policy_schema == SCHEMA and finding_payload["reproof_call"] is not None:
            _validate_live_reproof_request(
                tree, reading, finding_payload["reproof_call"], expected_request
            )
            if (
                finding_payload["change_record"]
                and payload.get("prompt") != finding_payload["reproof_call"]["audit_prompt"]
            ):
                raise SchemaRefusal(
                    f"reading of {act_id} publishes re-proof text beside another call's prompt"
                )
    elif record["request_digest"] is not None:
        raise SchemaRefusal(
            f"reading of {act_id} names a delivered audit request although its frozen plan "
            "and round cap left nothing to deliver"
        )
    _validate_uncertainty_projection(payload, finding_payload, act_id)
    return {"record": record, "draft": draft, "finding": finding}


def _basis_page_ids(payload: dict[str, Any], act_id: str) -> list[str]:
    basis = payload.get("basis")
    if not isinstance(basis, dict):
        raise SchemaRefusal(
            f"reading of {act_id} has no object basis; its completed reading cannot be "
            "reconciled to evidence; restore the sealed basis before consuming it"
        )
    regions = basis.get("regions")
    if not isinstance(regions, list) or not regions:
        raise SchemaRefusal(
            f"reading of {act_id} has no non-empty region basis; its completed text names "
            "no ink; restore the contributing region records before consuming it"
        )
    pages_by_ordinal: dict[int, str] = {}
    for region in regions:
        ordinal = region.get("source_page_ordinal") if isinstance(region, dict) else None
        page_id = region.get("source_page_id") if isinstance(region, dict) else None
        if (
            not _integer(ordinal)
            or not isinstance(page_id, str)
            or not page_id
            or (ordinal in pages_by_ordinal and pages_by_ordinal[ordinal] != page_id)
        ):
            raise SchemaRefusal(
                f"reading of {act_id} has an unusable source page in its region basis; the "
                "audit page set cannot be derived; restore one page id per integer ordinal"
            )
        pages_by_ordinal[ordinal] = page_id
    return sorted(set(pages_by_ordinal.values()))


def _validate_uncertainty_projection(
    payload: dict[str, Any], finding_payload: dict[str, Any], act_id: str
) -> None:
    expected_uncertainty = [
        {
            "start": span["start"],
            "end": span["end"],
            "alternatives": [],
            "confidence": "low",
        }
        for span in finding_payload["uncertain_spans"]
    ]
    # The exhausted-cap projection leads the layer; only an `assessed` reader may
    # add spans of its own after it.
    published = payload.get("uncertain_spans")
    # Validated before its state is read, or a contradictory `assessed` record
    # could choose the relaxed prefix rule.
    assessment_record = uncertainty.validate_assessment_record(
        payload.get("uncertainty_assessment"), f"reading of {act_id}"
    )
    state = assessment_record["state"]
    if not isinstance(published, list):
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit uncertainty projection")
    if state == "assessed":
        agrees = published[: len(expected_uncertainty)] == expected_uncertainty
    else:
        agrees = published == expected_uncertainty
    if not agrees:
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit uncertainty projection")
    # A gap is unread ink (goal 2): outside `assessed`, only the whole-act gap of a
    # `no-readable-text` outcome may appear.
    gaps = payload.get("gaps")
    if not isinstance(gaps, list):
        raise SchemaRefusal(f"reading of {act_id} has no gap list beside its audit projection")
    if state != "assessed" and not all(
        isinstance(gap, dict) and gap.get("position") == "whole-act" for gap in gaps
    ):
        raise SchemaRefusal(
            f"reading of {act_id} publishes a gap of its own although its sealed assessment "
            f"is {state!r}; only a reader that was asked reports where its sight failed"
        )
    # The last check before the Recensor publishes. Self-revisions index the prior
    # draft, so they are held at the Archetypus instead.
    try:
        uncertainty.validate(
            {
                "uncertain_spans": published,
                "gaps": gaps,
                "self_revisions": [],
                "assessment": assessment_record,
            },
            payload.get("text"),
        )
    except SchemaRefusal as error:
        raise SchemaRefusal(
            f"reading of {act_id} carries an uncertainty layer its text cannot anchor: {error}"
        ) from error


def audit_digest(payload: dict[str, Any]) -> str:
    """A helper for a consumer to bind the exact record it accepted."""
    return digest_of(payload)
