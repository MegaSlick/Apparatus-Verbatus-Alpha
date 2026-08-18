"""Fixture-only witness feeding contracts for R3.

These adapters describe exactly what a real witness call must receive and retain,
but deliberately make no model call.  They are small enough to exercise against
fixtures while preserving the operational boundary: a response is already
complete when it reaches this module.  In particular, repetition is inspected
*after* capture; it cannot affect generation or alter the captured bytes.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from contextlib import contextmanager
from itertools import groupby
from threading import RLock
from typing import Any, Callable, Iterator

from common.chandra_custody import read_retained_chandra_response
from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.contracts.stages import ATTESTATORES

CHURRO_OUTPUT_TOKENS = 24_000
DAI_MAX_WIDTH_PX = 1_500
DAI_MAX_HEIGHT_PX = 4_096
DAI_MAX_TOTAL_PIXELS = 2_359_296
# Two of these three ceilings are read off something; one is chosen. Sealing them
# side by side as bare integers made all three read as measured model bounds, and
# a number nobody can trace is exactly what GOVERNANCE 10 refuses -- so each one
# carries its own provenance into the record it seals, and a chosen ceiling says
# out loud that it was chosen.
DAI_LIMIT_SOURCES = {
    "max_width_px": ("design v2.1 section 2: DAI is fed act crops at most 1500 px wide"),
    "max_height_px": (
        "R3 policy, no model source: nothing in the design, the roster or DAI's own "
        "serving flags names a pixel height (the serving script's 4096 is "
        "--max-model-len, a token count). Chosen so the sealed pixel budget still "
        "affords 576 px of width at the ceiling (4096 x 576 = 2359296 exactly); "
        "below it the total-pixel ceiling governs, so it binds only strips past "
        "about 7:1 that carry too little width to read"
    ),
    "max_total_pixels": (
        "DAI's own serving profile max_pixels (the old pipeline's serve_dai.sh), "
        "already recorded in this repository at operations/serving/preflight.py"
    ),
}
SCHEDULING_POLICY = "chair-outer-act-inner.stage-major-parish.v1"
# Where an act with no page ordinal sorts: before every placed act, and named
# rather than spelled -1 at the two places that have to agree on it.
_UNPLACED_ORDINAL = -1
# The (adapter, parser) pairs `retain_model_view` can actually carry to a state.
_RUNNABLE_PARSERS = frozenset({("churro.v1", "xml")})
_UNCERTAINTY_TOKENS = ("[UNCERTAIN]", "[CROSSED_OUT]")
_REPETITION_WINDOW = 24
_REPETITION_MIN_REPEATS = 3


def churro_prompt() -> dict[str, str]:
    """The trained two-message XML framing, retained verbatim.

    These strings are carried bytes from the quarantined prior adapter,
    ``/window/remote/pilot_churro.py`` (its transcription of the Churro model
    release's ``prompts/ocr.py``), named as carried in the commit that brought
    them per the quarantine rule. The Churro release is non-commercially
    licensed; this repository's recorded posture is the research track
    (workbench/standing/MASTER_PLAN.md, witness-licenses row): research use is
    compliant, weights are never vendored, and any merging decision needs its
    own licensing decision first. They are split by role because joining or
    summarizing them changes the actual chat-template bytes the model receives.
    """
    system = (
        "You are an expert in diplomatic transcription of historical documents from various "
        "languages. Your task is to extract the full text from a given page. Only output the "
        "transcribed text between <output> and </output> tags."
    )
    user = (
        "Follow these instructions:\n\n"
        "1. You will be provided with a scanned document page.\n\n"
        "2. Perform transcription on the entirety of the page, converting all visible text into "
        "the following format. Include handwritten and print text, if any. Include tables, "
        "captions, headers, main text and all other visible text.\n\n"
        "3. If you encounter any non-text elements, simply skip them without attempting to "
        "describe them.\n\n"
        "4. Do not modernize or standardize the text. For example, if the transcription is using "
        '"ſ" instead of "s" or "а" instead of "a", keep it that way.\n\n'
        "5. When you come across text in languages other than English, transcribe it as "
        "accurately as possible without translation.\n\n"
        "6. Output the OCR result in the following format:\n\n"
        "<output>\nextracted text here\n</output>\n\n"
        "Remember, your goal is to accurately transcribe the text from the scanned page as much "
        "as possible. Process the entire page, even if it contains a large amount of text, and "
        "provide clear, well-formatted output. Pay attention to the appropriate reading order "
        "and layout of the text."
    )
    return {"system": system, "user": user}


def churro_generation() -> dict[str, int]:
    """The predeclared operational bound, not a content or repetition control."""
    return {"max_new_tokens": CHURRO_OUTPUT_TOKENS}


def validate_churro_xml(raw: bytes) -> str:
    """Validate the native Churro XML while retaining raw bytes on every failure."""
    # Refused as a type before any content question: a str here would slip past
    # the byte scan below and still parse, making the DOCTYPE refusal skippable
    # by the caller's choice of type.
    if not isinstance(raw, (bytes, bytearray)):
        raise SchemaRefusal("Churro response is not raw bytes")
    # A DOCTYPE is refused before the parser sees it: a legitimate Churro
    # response is one plain <output> element, and a DTD is the door to entity
    # tricks this validator has no reason to keep open. A whole-payload scan on
    # purpose — a DOCTYPE may follow an XML declaration, and escaped text
    # (&lt;!DOCTYPE) never contains these bytes, so honest transcriptions pass.
    if b"<!DOCTYPE" in bytes(raw).upper():
        raise SchemaRefusal("Churro response carries a DOCTYPE; a plain <output> element cannot")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeDecodeError) as error:
        raise SchemaRefusal(f"Churro response is not parseable XML: {error}") from error
    if root.tag != "output" or set(root.attrib) or list(root):
        raise SchemaRefusal("Churro response must be a plain <output> XML element")
    return root.text or ""


def detect_repetition(raw: bytes) -> dict[str, Any] | None:
    """Report a repeated tail after capture; this function has no generation input."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Not silence: None below means "inspected, nothing to report", and an
        # undecodable capture was never inspected at all. The record carries
        # that fact as its own finding kind so nothing is lost silently, and
        # `retain_model_view` knows not to call it a repetition.
        return {"kind": "post-hoc-repetition-uninspected", "reason": "response is not UTF-8 text"}
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < _REPETITION_WINDOW * _REPETITION_MIN_REPEATS:
        return None
    for width in range(
        _REPETITION_WINDOW, min(256, len(normalized) // _REPETITION_MIN_REPEATS) + 1
    ):
        unit = normalized[-width:]
        repeats = 1
        # One window back at a time, rather than `normalized.endswith(unit *
        # (repeats + 1))`. That form rebuilds and re-compares the whole matched
        # tail on every step, so counting a tail of n repetitions costs O(n^2)
        # characters: measured on a wholly repeated response, 0.58 s at 0.9 MB
        # and 16.5 s at 4.6 MB, against 0.04 s and 0.23 s here. Nothing bounds
        # the size of a captured response, and the cost lands on exactly the
        # degenerate runaway output this detector exists to find. The two forms
        # accept the same tails and return the same count: the tail already ends
        # with `unit * repeats`, so one further repetition is one further window
        # equal to `unit` -- pinned in the R3 suite against the built-string
        # reading it replaces.
        while (repeats + 1) * width <= len(normalized) and (
            normalized[-(repeats + 1) * width : -repeats * width] == unit
        ):
            repeats += 1
        if repeats >= _REPETITION_MIN_REPEATS:
            return {"kind": "post-hoc-repetition", "unit_characters": width, "repeats": repeats}
    return None


def dai_model_view(
    *,
    source_image_ref: dict[str, str],
    model_image_ref: dict[str, str],
    width_px: int,
    height_px: int,
    system_prompt_ref: dict[str, str],
    query_prompt_ref: dict[str, str],
    generation_config_ref: dict[str, str],
) -> dict[str, Any]:
    """Build DAI's crop view, referencing carried prompt/config bytes by manifest."""
    for name, reference in (
        ("source image", source_image_ref),
        ("model image", model_image_ref),
        ("system prompt", system_prompt_ref),
        ("query prompt", query_prompt_ref),
        ("generation config", generation_config_ref),
    ):
        _reference(reference, name)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (width_px, height_px)
    ):
        raise SchemaRefusal("DAI input dimensions must be positive integers")
    resized_width, resized_height = _dai_dimensions(width_px, height_px)
    resized = (resized_width, resized_height) != (width_px, height_px)
    if resized and source_image_ref["sha256"] == model_image_ref["sha256"]:
        raise SchemaRefusal("DAI resized model image is not distinct from its source bytes")
    if not resized and source_image_ref != model_image_ref:
        raise SchemaRefusal("DAI identity transform does not retain the source image bytes exactly")
    limits = {
        "schema": "dai-image-limits.v2",
        "max_width_px": DAI_MAX_WIDTH_PX,
        "max_height_px": DAI_MAX_HEIGHT_PX,
        "max_total_pixels": DAI_MAX_TOTAL_PIXELS,
        "sources": dict(DAI_LIMIT_SOURCES),
    }
    return {
        "adapter": "dai-atr.v1",
        "source_image_ref": source_image_ref,
        "model_image_ref": model_image_ref,
        "transform": {
            "kind": "resize-preserve-aspect" if resized else "identity",
            "resampler": "pillow-lanczos" if resized else None,
            "dimension_rounding": "floor" if resized else None,
            "source_width_px": width_px,
            "source_height_px": height_px,
            "target_width_px": resized_width,
            "target_height_px": resized_height,
        },
        "image_limits": limits,
        "image_limits_sha256": digest_of(limits),
        "prompts": {"system": system_prompt_ref, "query": query_prompt_ref},
        "generation_config_ref": generation_config_ref,
        "uncertainty_tokens_preserved": list(_UNCERTAINTY_TOKENS),
    }


def _dai_dimensions(width_px: int, height_px: int) -> tuple[int, int]:
    """Largest integer aspect-preserving view within every DAI ceiling."""
    upper_width = min(
        width_px,
        DAI_MAX_WIDTH_PX,
        width_px * DAI_MAX_HEIGHT_PX // height_px,
    )
    if upper_width < 1:
        raise SchemaRefusal("DAI image aspect cannot fit the sealed height ceiling")
    low, high = 1, upper_width
    while low < high:
        candidate = (low + high + 1) // 2
        candidate_height = max(1, height_px * candidate // width_px)
        if candidate * candidate_height <= DAI_MAX_TOTAL_PIXELS:
            low = candidate
        else:
            high = candidate - 1
    target_width = low
    target_height = max(1, height_px * target_width // width_px)
    if target_height > DAI_MAX_HEIGHT_PX:
        raise SchemaRefusal("DAI image aspect cannot fit the sealed height ceiling")
    return target_width, target_height


def chandra_capture_intake(
    tree: Any,
    *,
    page_id: str,
    page_ordinal: int,
    response_ref: object,
    receipt_ref: object,
    custody_ref: object,
) -> dict[str, Any]:
    """Consume R2's one-receipt raw response without re-serving Chandra.

    The shared custody rule lives in common/chandra_custody.py (a stage may not
    import another stage's module).  ``custody_ref`` is the content-addressed
    binding record R2's write recorded; without it, two individually-valid
    references would not be proof they came from the same Chandra call.  The
    result intentionally carries the original references alongside the exact
    raw bytes' digest.
    """
    raw = read_retained_chandra_response(
        tree,
        response_ref,
        receipt_ref,
        custody_ref,
        page_id=page_id,
        page_ordinal=page_ordinal,
    )
    return {
        "schema": "attestatores-chandra-capture.v1",
        "adapter": "chandra-capture.v1",
        "page_id": page_id,
        "page_ordinal": page_ordinal,
        "response_ref": response_ref,
        "receipt_ref": receipt_ref,
        "custody_ref": custody_ref,
        "raw_response_sha256": digest_bytes(raw),
    }


def retain_model_view(
    tree: Any,
    *,
    adapter: str,
    view: dict[str, Any],
    raw_response: bytes,
    transport_stop_reason: str,
    parser: str | None = None,
) -> dict[str, Any]:
    """Retain a reproducible view and raw response, including parser failure bytes."""
    if not isinstance(adapter, str) or not adapter:
        raise SchemaRefusal("model-view adapter is blank")
    if not isinstance(raw_response, bytes):
        raise SchemaRefusal("model-view raw response is not bytes")
    if not isinstance(transport_stop_reason, str) or not transport_stop_reason:
        raise SchemaRefusal("model-view transport stop reason is blank")
    # A parser this boundary cannot run would leave `parse.state` at "pending"
    # forever: a finished attempt wearing the look of one still in progress, which
    # is the shape GOVERNANCE 2 refuses. Ask for a parse that runs, or ask for none.
    if parser is not None and (adapter, parser) not in _RUNNABLE_PARSERS:
        raise SchemaRefusal(f"model-view parser {parser!r} does not run for adapter {adapter!r}")
    raw_digest, published = tree.put_blob(ATTESTATORES, raw_response)
    record: dict[str, Any] = {
        "schema": "attestatores-model-view.v1",
        "adapter": adapter,
        "view": view,
        "raw_response_ref": {"relative_path": published.relative_path, "sha256": raw_digest},
        "transport_stop_reason": transport_stop_reason,
        # What the transport said, until this boundary finds a reason to say
        # something more honest. Stated once here rather than reassigned
        # identically down each branch that finds nothing.
        "stop_reason": transport_stop_reason,
        "findings": [],
        "parse": {"state": "not-requested" if parser is None else "pending", "parser": parser},
    }
    if adapter == "churro.v1":
        if finding := detect_repetition(raw_response):
            record["findings"].append(finding)
            # Only an actual repetition rewrites the stop reason; an
            # uninspected capture is a recorded fact, not a detected failure.
            if finding["kind"] == "post-hoc-repetition":
                record["stop_reason"] = "partial-post-hoc-repetition-detected"
        if parser == "xml":
            try:
                record["parse"] = {
                    "state": "parsed",
                    "parser": "xml",
                    "text": validate_churro_xml(raw_response),
                }
            except SchemaRefusal as error:
                record["parse"] = {"state": "failed", "parser": "xml", "reason": str(error)}
                record["stop_reason"] = "partial-parse-failed"
    return record


def stage_major_schedule(
    parish_id: str, acts: Iterable[dict[str, Any]], chairs: Iterable[str]
) -> list[dict[str, str]]:
    """One resident chair at a time; deterministic chair-outer, act-inner order."""
    if not isinstance(parish_id, str) or not parish_id:
        raise SchemaRefusal("schedule parish identity is blank")
    chair_rows = list(chairs)
    if any(not isinstance(chair, str) or not chair for chair in chair_rows):
        raise SchemaRefusal("schedule chair identity is blank")
    ordered_chairs = sorted(set(chair_rows))
    if len(ordered_chairs) != len(chair_rows):
        # Iterables in production are lists; accepting duplicates makes a repeated
        # serving action look like normal scheduling, so materialize once below.
        raise SchemaRefusal("schedule repeats a chair")
    rows = list(acts)
    seen_acts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("act_id"), str) or not row["act_id"]:
            raise SchemaRefusal("schedule act has no identity")
        # The same reason the chair check gives, in the other dimension: a
        # repeated act row is a second serving of one act wearing the look of
        # ordinary scheduling, and every chair would carry it, so one duplicate
        # in becomes one duplicate Testimonium per chair out.
        if row["act_id"] in seen_acts:
            raise SchemaRefusal("schedule repeats an act")
        seen_acts.add(row["act_id"])
        # Checked rather than left to `sorted`, which answers a non-integer
        # ordinal with an unnamed TypeError from inside a comparison.
        ordinal = row.get("page_ordinal", _UNPLACED_ORDINAL)
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise SchemaRefusal("schedule act page ordinal is not an integer")
    ordered_acts = sorted(
        rows, key=lambda row: (row.get("page_ordinal", _UNPLACED_ORDINAL), row["act_id"])
    )
    return [
        {
            "policy": SCHEDULING_POLICY,
            "parish_id": parish_id,
            "chair": chair,
            "act_id": act["act_id"],
        }
        for chair in ordered_chairs
        for act in ordered_acts
    ]


class SingleChairResidency:
    """Fail-closed ownership of the one model resource an orchestrator may load.

    The resident name is reserved before ``load`` runs and is cleared only after
    ``unload`` succeeds. A failed unload therefore blocks every later acquire;
    it can never be mistaken for proof that the resource became vacant.

    **A failed load blocks them too, and that is the point.** The reservation is
    taken before ``load``, so a load that raises leaves the chair marked resident
    with no matching ``unload`` -- a load can fail with weights already mapped,
    and this guard exists to keep a second chair off a card whose occupancy is
    unknown. Clearing the reservation would be guessing that nothing was
    allocated. Recovering from it is an operator act against observed provider
    state, exactly as GOVERNANCE 8 requires of a shutdown, never an inference
    this object may make on its own.
    """

    def __init__(
        self,
        load: Callable[[str], Any],
        unload: Callable[[str, Any], None],
    ) -> None:
        self._load = load
        self._unload = unload
        self._resident: str | None = None
        self._lock = RLock()

    @property
    def resident(self) -> str | None:
        with self._lock:
            return self._resident

    @contextmanager
    def occupy(self, chair: str) -> Iterator[Any]:
        if not isinstance(chair, str) or not chair:
            raise SchemaRefusal("residency chair identity is blank")
        with self._lock:
            if self._resident is not None:
                raise SchemaRefusal(
                    f"cannot load chair {chair!r} while chair {self._resident!r} is resident"
                )
            self._resident = chair
        resource = self._load(chair)
        try:
            yield resource
        finally:
            self._unload(chair, resource)
            with self._lock:
                if self._resident != chair:
                    raise SchemaRefusal("single-chair residency state diverged during unload")
                self._resident = None


def execute_stage_major_schedule(
    schedule: Iterable[dict[str, str]],
    *,
    residency: SingleChairResidency,
    serve: Callable[[Any, dict[str, str]], Any],
) -> list[Any]:
    """Execute only contiguous chair blocks through the shared residency guard."""
    rows = list(schedule)
    expected_fields = {"policy", "parish_id", "chair", "act_id"}
    if any(
        not isinstance(row, dict)
        or set(row) != expected_fields
        or row["policy"] != SCHEDULING_POLICY
        or any(not isinstance(row[field], str) or not row[field] for field in expected_fields)
        for row in rows
    ):
        raise SchemaRefusal("stage-major execution received a malformed schedule row")
    chair_blocks = []
    for chair, chair_rows in groupby(rows, key=lambda row: row["chair"]):
        chair_blocks.append(chair)
        served = [row["act_id"] for row in chair_rows]
        # The block is what one residency actually serves, so a repeat inside it
        # is a second serving of one act under one load -- the same defect the
        # builder refuses, arriving through a schedule this executor did not
        # build. Checked here rather than trusted from there.
        if len(set(served)) != len(served):
            raise SchemaRefusal("stage-major execution schedule serves one act twice to a chair")
    if len(chair_blocks) != len(set(chair_blocks)):
        raise SchemaRefusal("stage-major execution schedule returns to an unloaded chair")
    if len({row["parish_id"] for row in rows}) > 1:
        raise SchemaRefusal("stage-major execution schedule mixes parish identities")
    results = []
    for chair, chair_rows in groupby(rows, key=lambda row: row["chair"]):
        with residency.occupy(chair) as resource:
            results.extend(serve(resource, row) for row in chair_rows)
    return results


def _reference(value: object, name: str) -> None:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"DAI {name} reference has no closed digest shape")
    if not isinstance(value["relative_path"], str) or not value["relative_path"]:
        raise SchemaRefusal(f"DAI {name} reference path is blank")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise SchemaRefusal(f"DAI {name} reference digest is invalid")
