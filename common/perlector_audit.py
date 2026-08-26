"""Perlector Pass-C audit records, shared by their two stage-side halves.

`pipeline/4_perlector/run.py` produces the audit draft and finding records
(`pipeline/4_perlector/audit.py` holds the flag pass and the sealed policy
loader, and re-exports these names); `pipeline/5_recensor/run.py`'s
`audit_state` consumes them to decide review routing. The producer and the consumer must validate the exact same
closed schema, or drift between the two would let a malformed record pass one
side and fail the other silently. A stage may not import another stage's
uniquely named module (`pipeline/test_stage_import_boundaries.py`), so the
shared validation surface lives here; `pipeline/4_perlector/audit.py` keeps
its producer-only logic (the flag pass and sealed policy loader) and re-exports
these names so its own public API is unchanged.

**The audit request is the instrument, and it lives here too.** A re-proof plan
computed, sealed under `payload.audit.reproofs`, and then not handed to the
reader is an instrument misreporting itself (GOVERNANCE 10): the record would
say a measured, neutral, span-scoped re-examination produced the published text
while the reader was shown only the Pass-B dossier and a bare string. So one
function (`reproof_plan`) defines the plan, `audit_request` wraps it into the
closed object the reader is actually given, and `validate_chain` re-derives both
from the frozen draft — the seal, the delivery and the check are the same
computation over the same frozen flags rather than three that happen to agree.
`payload.audit.request_digest` is what binds a response to the request that
produced it; it is `None` exactly when no request was delivered, because "no
re-proof ran" and "a re-proof ran" are different recorded facts.
"""

from __future__ import annotations

from typing import Any, Final

from common.contracts.canonical import digest_of, is_sha256
from common.contracts.envelope import validate_input_refs
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import PERLECTOR
from common.corpus_register import refuse_preference

SCHEMA: Final = "perlector-audit.v1"
# The rendered instrument's own label. Separate from `SCHEMA` because the two
# are versioned by different things: `SCHEMA` names the sealed policy an audit
# record was computed under, while this names the shape of the object a reader
# is handed. A serving path that learns to render this into prompt bytes moves
# this label without touching the policy seal.
REQUEST_SCHEMA: Final = "perlector-audit-request.v1"
# The pass this instrument belongs to, named on the consuming side so
# `reader.py`'s delivery check and its closed pass vocabulary compare against
# one spelling. The producer deliberately keeps its own literal at the call
# site: `test_reader.py` reads every `pass_kind="..."` out of `run.py` and pins
# `PASS_KINDS` to exactly that set, which is a stronger guard against a
# misspelt pass than a shared constant would be, and it only works on literals.
REPROOF_PASS_KIND: Final = "audit-reproof"
AUDIT_CAP_EXHAUSTED: Final = "audit-round-cap-exhausted"
FLAG_CLASSES: Final = frozenset(
    {"date-sequence", "numbering", "order", "testimony-diff", "repetition", "within-crop"}
)
_DRAFT_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "semi_final_text",
        "page_id",
        "page_ids",
        "round_cap",
        "policy",
        "flags",
        "flag_location_basis",
    }
)
_FINDING_FIELDS: Final = frozenset(
    {
        "act_key",
        "attempt_ordinal",
        "page_id",
        "page_ids",
        "round_cap",
        "policy",
        "flags",
        "change_record",
        "uncertain_spans",
        "unresolved",
    }
)
_PERLECTIO_AUDIT_FIELDS: Final = frozenset(
    {"draft_ref", "finding_ref", "finding_digest", "unresolved", "reproofs", "request_digest"}
)
# The closed shape of what the reader receives. `draft_ref` is both the frozen
# draft's reference and its digest, so the request names the exact bytes the
# locations index into without restating them; `semi_final_text` is those bytes'
# text, delivered because a re-proof that is asked to "record confirmed
# unchanged" must be shown what unchanged means.
_AUDIT_REQUEST_FIELDS: Final = frozenset(
    {"schema", "act_key", "attempt_ordinal", "draft_ref", "semi_final_text", "reproofs"}
)


# Module scope so tests can assert against the same closed list the runtime
# screen uses. The screen below is unreachable over today's fixed literal, and
# that is its job: it fires on every call the moment an editor softens the
# wording (or a future variant is built through this function), which is a
# stronger guard than a test that must remember to run.
FORBIDDEN_PROMPT_FRAGMENTS: Final = (
    "wrong",
    "incorrect",
    "should read",
    "expected",
    "replace with",
    "must change",
)


def neutral_prompt(*, start: int, end: int, text_length: int) -> str:
    if not 0 <= start <= end <= text_length:
        raise SchemaRefusal("an audit re-proof location lies outside the delivered text")
    prompt = (
        "Re-examine the ink at character location "
        f"[{start}, {end}) of the delivered act. Report only what the ink supports there; "
        "if it supports the existing text, record confirmed unchanged."
    )
    lowered = prompt.lower()
    if any(fragment in lowered for fragment in FORBIDDEN_PROMPT_FRAGMENTS):
        raise SchemaRefusal("the audit re-proof prompt is not neutral")
    return prompt


def reproof_plan(flags: list[dict[str, Any]], *, text_length: int) -> list[dict[str, Any]]:
    """One neutral, location-only re-proof per frozen flag, in the flags' own order.

    The single definition of what Pass C intends to ask. Three callers need it:
    two used to spell it out — the producer building the seal and
    `validate_chain` re-deriving the expected seal — and the third, the reader's
    delivered request, is new with this repair. Every extra spelling of one
    plan is another chance for the sealed plan and
    the delivered plan to differ while every local check still passes — which is
    exactly the shape of the defect this function exists to close.

    Locations are copied rather than aliased. The plan travels out to a reader,
    and a caller that mutated a delivered row would otherwise reach back into
    the frozen flag set the whole page pass was computed from.
    """
    return [
        {
            "class": flag["class"],
            "location": {"start": flag["location"]["start"], "end": flag["location"]["end"]},
            "prompt": neutral_prompt(
                start=flag["location"]["start"],
                end=flag["location"]["end"],
                text_length=text_length,
            ),
        }
        for flag in flags
    ]


def _closed(payload: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != fields:
        raise SchemaRefusal(f"an {label} is not its closed schema")
    return payload


def _location(value: Any, *, text_length: int | None, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise SchemaRefusal(f"an {label} has no closed character location")
    start, end = value["start"], value["end"]
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end
        or (text_length is not None and end > text_length)
    ):
        raise SchemaRefusal(f"an {label} lies outside the delivered text")
    return value


def _validate_reproof_rows(rows: list[Any], *, text_length: int | None, subject: str) -> None:
    """The neutrality screen, applied identically wherever a re-proof row appears.

    `text_length=None` is the pre-read pass's contract: shape and location
    structure are held, and the prompt is checked against the location's own
    end. `validate_chain` re-runs this with the frozen semi-final's real
    length, so a location that only fits a longer text than the draft carries
    is still refused where the draft is in hand.

    The sealed copy on the Perlectio and the delivered copy in the audit request
    are the same rows read by different halves of the same claim, so they get the
    same screen: closed shape, a known flag class, a location inside the frozen
    text, and a prompt that is *exactly* `neutral_prompt` for that location.
    Equality against the generated prompt is the whole discipline — it leaves no
    room for a sentence that tells the reader which way to argue (GOVERNANCE 10),
    because anything but the generated string is refused rather than screened for
    forbidden words.
    """
    for reproof in rows:
        if not isinstance(reproof, dict) or set(reproof) != {"class", "location", "prompt"}:
            raise SchemaRefusal(f"a {subject} re-proof is not its closed schema")
        if reproof["class"] not in FLAG_CLASSES or not isinstance(reproof["prompt"], str):
            raise SchemaRefusal(f"a {subject} re-proof has an unknown class or prompt")
        location = _location(
            reproof["location"], text_length=text_length, label=f"{subject} re-proof"
        )
        if reproof["prompt"] != neutral_prompt(
            start=location["start"],
            end=location["end"],
            text_length=text_length if text_length is not None else location["end"],
        ):
            raise SchemaRefusal(f"a {subject} re-proof is not a neutral location-only prompt")


def reproof_delivery_due(flags: list[Any], round_cap: int) -> bool:
    """One spelling of "this act's re-proof request exists": a plan and a round.

    The producer decides delivery with it, and `validate_chain` re-derives the
    same fact from the frozen draft with it. Two spellings of that condition
    would be the exact drift this module repairs elsewhere: an act one side
    thinks delivered and the other thinks had nothing to deliver.
    """
    return bool(flags) and round_cap > 0


def audit_request(
    *,
    act_key: str,
    attempt_ordinal: int,
    draft_ref: dict[str, str],
    semi_final_text: str,
    flags: list[dict[str, Any]],
) -> dict[str, Any]:
    """The closed instrument Pass C hands the reader, built from the frozen draft.

    Everything here is derivable from the published audit draft plus its own
    reference, which is what makes the delivery checkable: `validate_chain`
    rebuilds this object from the draft it reads back and compares digests, so
    the request the record names is the request the frozen flags imply. Nothing
    in it is a Pass-C-only fact the producer could have chosen freely.

    It carries no witness material, no ranking, and no wanted reading — only the
    frozen text, the locations, and the generated neutral prompts. The field set
    is closed rather than swept for preference-bearing names the way a dossier
    is (`dossier.assert_no_order_bearing_field`): a closed set is the stronger
    guard, because a new field cannot appear at all without an editor coming
    through this validator first.
    """
    request = {
        "schema": REQUEST_SCHEMA,
        "act_key": act_key,
        "attempt_ordinal": attempt_ordinal,
        "draft_ref": dict(draft_ref),
        "semi_final_text": semi_final_text,
        "reproofs": reproof_plan(flags, text_length=len(semi_final_text)),
    }
    return validate_audit_request(request)


def validate_audit_request(payload: Any) -> dict[str, Any]:
    """Refuse an audit request at the seam, in the producer and in the reader alike.

    A reader is entitled to refuse rather than guess: the pass label alone can
    never tell it which span to re-examine or what task was delivered, and a
    reader that read `pass_kind` and invented the rest is the failure this
    request exists to end.
    """
    value = _closed(payload, _AUDIT_REQUEST_FIELDS, "audit request")
    if value["schema"] != REQUEST_SCHEMA:
        raise SchemaRefusal("an audit request does not declare the audit-request schema")
    if not isinstance(value["act_key"], str) or not value["act_key"]:
        raise SchemaRefusal("an audit request has no act identity")
    if (
        not isinstance(value["attempt_ordinal"], int)
        or isinstance(value["attempt_ordinal"], bool)
        or value["attempt_ordinal"] < 1
    ):
        raise SchemaRefusal("an audit request has no integer attempt ordinal")
    validate_input_refs([value["draft_ref"]])
    # `validate_input_refs` reads two keys and ignores the rest; an extra field
    # here would ride to the reader, into the digest, and back out of
    # `validate_chain`'s rebuild without ever meeting the neutrality screen. The
    # closed-set claim above is only true if the nested shape is closed too.
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
        value["reproofs"], text_length=len(value["semi_final_text"]), subject="audit request"
    )
    return value


def _validate_common(value: dict[str, Any], *, text_length: int) -> None:
    if (
        not isinstance(value["act_key"], str)
        or not value["act_key"]
        or not isinstance(value["page_id"], str)
        or not value["page_id"]
        or not isinstance(value["page_ids"], list)
        or not value["page_ids"]
        or any(not isinstance(page_id, str) or not page_id for page_id in value["page_ids"])
        or len(value["page_ids"]) != len(set(value["page_ids"]))
        or value["page_id"] != value["page_ids"][0]
    ):
        raise SchemaRefusal("an audit record has no act or page identity")
    if (
        not isinstance(value["attempt_ordinal"], int)
        or isinstance(value["attempt_ordinal"], bool)
        or value["attempt_ordinal"] < 1
    ):
        raise SchemaRefusal("an audit record has no integer attempt ordinal")
    if (
        not isinstance(value["round_cap"], int)
        or isinstance(value["round_cap"], bool)
        or value["round_cap"] < 0
    ):
        raise SchemaRefusal("an audit record has no integer round cap")
    if not isinstance(value["policy"], dict) or set(value["policy"]) != {
        "schema",
        "sha256",
        "approval_ref",
    }:
        raise SchemaRefusal("an audit record has no sealed policy reference")
    if (
        value["policy"]["schema"] != SCHEMA
        or not is_sha256(value["policy"]["sha256"])
        or not isinstance(value["policy"]["approval_ref"], str)
    ):
        raise SchemaRefusal("an audit record has a malformed sealed policy reference")
    if not isinstance(value["flags"], list):
        raise SchemaRefusal("an audit record has no flag list")
    for flag in value["flags"]:
        if not isinstance(flag, dict) or set(flag) != {"class", "location"}:
            raise SchemaRefusal("an audit flag is not its closed schema")
        if flag["class"] not in FLAG_CLASSES:
            raise SchemaRefusal("an audit flag has an unknown class or malformed location")
        _location(flag["location"], text_length=text_length, label="audit flag")


def validate_draft(payload: Any) -> dict[str, Any]:
    value = _closed(payload, _DRAFT_FIELDS, "audit draft")
    refuse_preference(value, what="an audit draft")
    if not isinstance(value["semi_final_text"], str):
        raise SchemaRefusal("an audit draft has no semi-final text")
    _validate_common(value, text_length=len(value["semi_final_text"]))
    basis = value["flag_location_basis"]
    if not isinstance(basis, list) or any(
        not isinstance(row, dict)
        or set(row) != {"class", "chair", "derivation"}
        or row["class"] != "testimony-diff"
        or not isinstance(row["chair"], str)
        or not row["chair"]
        or row["derivation"] not in {"own-report", "page-slice"}
        for row in basis
    ):
        raise SchemaRefusal(
            "the audit draft has no closed witness-derived flag-location basis. "
            "A testimony-diff location cannot be traced to the testimony that located it. "
            "Rebuild the draft with class, chair, and derivation for each such flag."
        )
    identities = [(row["class"], row["chair"], row["derivation"]) for row in basis]
    if len(identities) != len(set(identities)):
        raise SchemaRefusal(
            "the audit draft repeats a witness-derived flag-location basis. "
            "One witness would be recorded twice as the source of one location. "
            "Remove the duplicate basis row and rebuild the draft."
        )
    testimony_diff_count = sum(flag["class"] == "testimony-diff" for flag in value["flags"])
    if len(basis) != testimony_diff_count:
        raise SchemaRefusal(
            "the audit draft's testimony-diff flags and witness-derived location basis disagree. "
            "At least one witness-derived flag or its source would be unaccounted. "
            "Rebuild both lists from the same frozen dossier comparison."
        )
    return value


def validate_finding(payload: Any, *, text: str, flag_text: str | None = None) -> dict[str, Any]:
    value = _closed(payload, _FINDING_FIELDS, "audit finding")
    refuse_preference(value, what="an audit finding")
    if not isinstance(text, str):
        raise SchemaRefusal("an audit finding was validated without its final text")
    if flag_text is not None and not isinstance(flag_text, str):
        raise SchemaRefusal("an audit finding was validated without its frozen flag text")
    _validate_common(value, text_length=len(flag_text if flag_text is not None else text))
    if not isinstance(value["change_record"], list) or not isinstance(
        value["uncertain_spans"], list
    ):
        raise SchemaRefusal("an audit finding has malformed change or uncertainty records")
    if not isinstance(value["unresolved"], bool):
        raise SchemaRefusal("an audit finding does not say whether flags remain unresolved")
    for change in value["change_record"]:
        if not isinstance(change, dict) or set(change) != {"start", "end", "triggering_flag_class"}:
            raise SchemaRefusal("an audit change record is not its closed schema")
        if change["triggering_flag_class"] not in FLAG_CLASSES:
            raise SchemaRefusal("an audit change record names an unknown triggering flag class")
        _location(
            {"start": change["start"], "end": change["end"]},
            text_length=len(flag_text if flag_text is not None else text),
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
    if value["unresolved"] != (bool(value["flags"]) and value["round_cap"] == 0):
        raise SchemaRefusal("an audit finding's unresolved state contradicts its flags and cap")
    if bool(value["uncertain_spans"]) and not value["unresolved"]:
        raise SchemaRefusal("an audit finding carries uncertainty without an unresolved state")
    return value


def validate_perlectio_audit(record: Any, *, text_length: int | None) -> dict[str, Any]:
    value = _closed(record, _PERLECTIO_AUDIT_FIELDS, "Perlectio audit record")
    # Validate each typed reference here. Whether the two paths accidentally
    # alias is settled by `validate_chain`'s kind-specific reads, which then
    # names the actual draft-vs-finding contract violation rather than the
    # less useful generic duplicate-path refusal.
    validate_input_refs([value["draft_ref"]])
    validate_input_refs([value["finding_ref"]])
    if not is_sha256(value["finding_digest"]):
        raise SchemaRefusal("a Perlectio audit record has no finding payload digest")
    if not isinstance(value["unresolved"], bool) or not isinstance(value["reproofs"], list):
        raise SchemaRefusal("a Perlectio audit record has malformed resolution facts")
    # `None` is a fact, not an absence: it says no audit request was delivered
    # to the reader for this act, which is what a flagless act and an
    # exhausted-cap act both record. Anything else must be a real digest, so a
    # record cannot claim a delivery with a placeholder.
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
    """Attribute the re-proof's change to the flag that actually located it.

    The triggering class is the whole point of this record: spec 03's Pass C
    keeps it so that "witness-diff-triggered changes that moved toward the
    witness" — the soft-picker signature — is computable from the tree. So the
    change must be attributed to the *narrowest* flag that contains it, not to
    whichever flag the caller happened to list first. Flags reach here sorted by
    `(location.start, class)`, and the cross-act classes (`date-sequence`,
    `numbering`, `order`) all span the whole text from offset 0, so first-listed
    meant "the widest flag on the act wins": a correction squarely inside a
    narrow `testimony-diff` span was recorded as `date-sequence`, and the one
    measurement this record exists to support silently lost it (GOVERNANCE 10).

    Width ties break on `(start, class)` so the attribution is a function of the
    frozen flag set alone. Consumers re-derive this record exactly
    (`validate_chain`), so the rule may use nothing but the recorded facts.

    One re-proof still yields at most one changed span: the span is the
    prefix/suffix-trimmed envelope of an exact comparison, and a re-proof whose
    envelope escapes every single flag is refused rather than attributed by
    guesswork. Decomposing an envelope that covers two disjoint flags would need
    a real alignment, and this record is recomputed byte-for-byte by every later
    consumer — a diff heuristic here would make a sealed record's validity depend
    on the differ. That decomposition belongs with the real reader in R6, which
    is the first thing that can answer two locations in one pass.
    """
    if before == after:
        return []
    start, end = text_change_span(before, after)
    containing = [
        flag
        for flag in flags
        if flag["location"]["start"] <= start and end <= flag["location"]["end"]
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


def validate_chain(tree, reading: dict[str, Any], act_id: str) -> dict[str, Any]:
    """Validate the exact draft/finding/Perlectio relationship once for every reader."""
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
    )
    shared_fields = (
        "act_key",
        "attempt_ordinal",
        "page_id",
        "page_ids",
        "round_cap",
        "policy",
        "flags",
    )
    if any(draft_payload[field] != finding_payload[field] for field in shared_fields):
        raise SchemaRefusal(f"audit draft and finding for {act_id} restate different frozen facts")
    basis = payload.get("basis")
    if not isinstance(basis, dict):
        raise SchemaRefusal(f"reading of {act_id} has no object basis for its completed reading")
    regions = basis.get("regions")
    if not isinstance(regions, list) or not regions:
        raise SchemaRefusal(
            f"reading of {act_id} has no non-empty region basis for its completed reading"
        )
    pages_by_ordinal: dict[int, str] = {}
    for region in regions:
        ordinal = region.get("source_page_ordinal") if isinstance(region, dict) else None
        page_id = region.get("source_page_id") if isinstance(region, dict) else None
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(page_id, str)
            or not page_id
            or (ordinal in pages_by_ordinal and pages_by_ordinal[ordinal] != page_id)
        ):
            raise SchemaRefusal(
                f"reading of {act_id} has an unusable source page in its region basis"
            )
        pages_by_ordinal[ordinal] = page_id
    basis_page_ids = [pages_by_ordinal[ordinal] for ordinal in sorted(pages_by_ordinal)]
    if draft_payload["page_ids"] != basis_page_ids:
        raise SchemaRefusal(
            f"audit page set for {act_id} disagrees with the reading's sealed region basis"
        )
    if draft_payload["act_key"] != payload.get("act_key") or draft_payload[
        "attempt_ordinal"
    ] != payload.get("attempt_ordinal"):
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit identity")
    expected_changes = change_record(
        draft_payload["semi_final_text"], payload["text"], draft_payload["flags"]
    )
    if finding_payload["change_record"] != expected_changes:
        raise SchemaRefusal(f"reading of {act_id} disagrees with its exact audit change record")
    if record["finding_digest"] != audit_digest(finding_payload):
        raise SchemaRefusal(f"reading of {act_id} names an audit finding with a mismatched digest")
    if record["unresolved"] != finding_payload["unresolved"]:
        raise SchemaRefusal(f"reading of {act_id} contradicts its audit finding's unresolved state")
    if finding["inputs"] != [record["draft_ref"]]:
        raise SchemaRefusal(f"audit finding for {act_id} does not bind exactly its audit draft")
    if record["draft_ref"] not in reading.get("inputs", []) or record[
        "finding_ref"
    ] not in reading.get("inputs", []):
        raise SchemaRefusal(f"reading of {act_id} does not bind both audit artifacts as inputs")
    expected_reproofs = reproof_plan(
        draft_payload["flags"], text_length=len(draft_payload["semi_final_text"])
    )
    if record["reproofs"] != expected_reproofs:
        raise SchemaRefusal(f"reading of {act_id} does not retain exactly its frozen re-proof plan")
    # The delivery half of the same claim. A sealed plan says what Pass C
    # *intended* to ask; `request_digest` says which closed request the reader
    # was actually handed, and rebuilding that request here from the frozen
    # draft is what stops the two drifting. Delivery is not the producer's word
    # for it either: a request exists exactly when there was a plan to deliver
    # and a round left to spend, so an act with no flags, or one whose cap was
    # already exhausted, must name no request at all.
    if reproof_delivery_due(draft_payload["flags"], draft_payload["round_cap"]):
        expected_request = audit_request(
            act_key=draft_payload["act_key"],
            attempt_ordinal=draft_payload["attempt_ordinal"],
            draft_ref=record["draft_ref"],
            semi_final_text=draft_payload["semi_final_text"],
            flags=draft_payload["flags"],
        )
        if record["request_digest"] != audit_digest(expected_request):
            raise SchemaRefusal(
                f"reading of {act_id} does not name the exact audit request its frozen "
                "re-proof plan renders"
            )
    elif record["request_digest"] is not None:
        raise SchemaRefusal(
            f"reading of {act_id} names a delivered audit request although its frozen plan "
            "and round cap left nothing to deliver"
        )
    expected_uncertainty = [
        {
            "start": span["start"],
            "end": span["end"],
            "alternatives": [],
            "confidence": "low",
        }
        for span in finding_payload["uncertain_spans"]
    ]
    if payload.get("uncertain_spans") != expected_uncertainty:
        raise SchemaRefusal(f"reading of {act_id} disagrees with its audit uncertainty projection")
    return {"record": record, "draft": draft, "finding": finding}


def audit_digest(payload: dict[str, Any]) -> str:
    """A helper for a consumer to bind the exact record it accepted."""
    return digest_of(payload)
