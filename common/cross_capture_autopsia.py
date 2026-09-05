"""The one atomic, all-capture presentation used for a logical Perlectio.

This module intentionally has no reader and no text.  It is the data boundary
that makes a reader receive every capture in one request; a caller cannot turn
its views into capture-local readings and reconcile those strings afterwards.
"""

from __future__ import annotations

import copy
import unicodedata
from typing import Any, Callable, Final

from common.contracts.canonical import digest_bytes, digest_of
from common.contracts.errors import SchemaRefusal
from common.physical_act_partition import source_ledger_from_run

SCHEMA: Final = "cross-capture-autopsia.v1"
OVER_CAPACITY: Final = "cluster-presentation-over-capacity"
DISSENT_SCHEMA: Final = "cross-capture-dissent.v1"
_FORBIDDEN: Final = (
    "primary",
    "canonical",
    "select",
    "winner",
    "best",
    "better",
    "prefer",
    "rank",
    "trust",
    "weight",
    "score",
    "order",
    "reliab",
    "chosen",
    "priority",
    "picker",
)


def _sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise SchemaRefusal(f"cross-capture autopsia: {what} is not a lowercase SHA-256")
    return value


def _is_printable_nfc(value: str) -> bool:
    """A structural key one spelling of which cannot become two.

    NFC and NFD spellings of one accented key are different bytes to
    `canonical_bytes`, so an unnormalized key names a second view, physical
    page, or logical act that nothing else in the run can see.
    """
    return value.isprintable() and unicodedata.normalize("NFC", value) == value


def _ref(value: Any, what: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"cross-capture autopsia: {what} is not a digest-bound reference")
    path = value["relative_path"]
    if not isinstance(path, str) or not path:
        raise SchemaRefusal(f"cross-capture autopsia: {what} has no path")
    # Same containment idiom as `common/contracts/envelope.py::validate_input_refs`
    # and `common/runtree/store.py::RunTree.resolve`: a reference is relative to
    # the run root, and this schema is the boundary that seals it -- a caller
    # downstream trusting this shape as already-checked must not be the first
    # place a traversal path is actually refused.
    if path.startswith("/") or ".." in path.split("/"):
        raise SchemaRefusal(f"cross-capture autopsia: {what} path {path!r} escapes the run tree")
    _sha(value["sha256"], f"{what} sha256")
    return dict(value)


def _ref_list(value: Any, what: str) -> list[dict[str, str]]:
    """Keep duplicate references because distinct regions may encode to identical bytes."""
    if not isinstance(value, list) or not value:
        raise SchemaRefusal(f"cross-capture autopsia: {what} must retain every image reference")
    refs = [_ref(item, what) for item in value]
    return sorted(refs, key=lambda item: (item["relative_path"], item["sha256"]))


def _reject_preference(value: Any) -> None:
    """Refuse a nested capture-preference claim anywhere in an untrusted payload.

    Iterative for the same reason `corpus_register.refuse_capture_preference`
    is, and this was the one preference screen still recursing. Both guards run
    ahead of any shape check -- `build_autopsia` screens `views` before `_view`
    closes it, and `assemble_reader_input` screens a reader dossier whose only
    prior check is that it is a dict -- so the value each walks is arbitrary
    caller input. A recursive walk over it exhausted the interpreter stack at a
    few thousand levels and raised `RecursionError`, which is a crash and not a
    named refusal; the payload that reached the guard then left no record of
    what was wrong with it. Depth is the walk's own list here, so a deep payload
    is screened to the bottom exactly like a shallow one.

    Depth was only half of it. The screen runs *before* `_view` proves any
    shape, so the `views` it walks is arbitrary caller input, and
    `build_autopsia` is called with in-memory lists rather than a document this
    module parsed -- so a view that is its own ancestor reaches this walk. The
    recursion this replaced ended such a payload by exhausting itself; a
    worklist has none to exhaust, so it appended forever and hung the caller
    instead of returning a named refusal. The enter/exit bookkeeping below is
    the dossier sweep's, and it refuses through this screen's own
    `SchemaRefusal`.

    Only the containers open on the current path are tracked. A view object
    genuinely shared between two entries is not a cycle and stays permitted --
    it is refused later, by name, as a duplicate capture rather than here as a
    loop.
    """
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
                    "cross-capture autopsia: a presentation contains itself, so no sweep "
                    "of it can terminate and a preference field below the loop could "
                    "never be found; the presentation is refused"
                )
            open_path.add(marker)
            pending.append(("exit", marker))
        if isinstance(current, dict):
            for key, item in current.items():
                lowered = str(key).lower()
                if any(fragment in lowered for fragment in _FORBIDDEN):
                    raise SchemaRefusal(
                        f"cross-capture autopsia: forbidden preference field {key!r}"
                    )
                pending.append(("value", item))
        elif isinstance(current, list):
            pending.extend(("value", item) for item in current)


def _view(row: Any) -> dict[str, Any]:
    fields = {
        "view_id",
        "physical_page_id",
        "source_sha256",
        "page_ids",
        "local_act_ids",
        "region_refs",
        "page_render_refs",
        "alignment_ref",
        "visibility_evidence_refs",
    }
    if not isinstance(row, dict) or set(row) != fields:
        raise SchemaRefusal("cross-capture autopsia: view is not its closed schema")
    if not all(
        isinstance(row[field], str) and row[field]
        for field in ("view_id", "physical_page_id", "alignment_ref")
    ):
        raise SchemaRefusal("cross-capture autopsia: view has incomplete immutable identity")
    if not _is_printable_nfc(row["view_id"]) or not _is_printable_nfc(row["physical_page_id"]):
        raise SchemaRefusal(
            "cross-capture autopsia: a view or physical-page key is not printable NFC; the "
            "presentation is refused because normalization variants cannot name different "
            "evidence"
        )
    source = _sha(row["source_sha256"], "view source_sha256")
    page_ids = row["page_ids"]
    local_ids = row["local_act_ids"]
    if (
        not isinstance(page_ids, list)
        or not page_ids
        or any(not isinstance(x, str) or not x for x in page_ids)
    ):
        raise SchemaRefusal("cross-capture autopsia: view has no complete page set")
    if not isinstance(local_ids, list) or any(not isinstance(x, str) or not x for x in local_ids):
        raise SchemaRefusal("cross-capture autopsia: view local act identities are malformed")
    return {
        "view_id": row["view_id"],
        "physical_page_id": row["physical_page_id"],
        "source_sha256": source,
        "page_ids": sorted(set(page_ids)),
        "local_act_ids": sorted(set(local_ids)),
        "region_refs": _ref_list(row["region_refs"], "region_refs"),
        "page_render_refs": _ref_list(row["page_render_refs"], "page_render_refs"),
        "alignment_ref": row["alignment_ref"],
        "visibility_evidence_refs": _ref_list(
            row["visibility_evidence_refs"], "visibility_evidence_refs"
        ),
    }


def build_autopsia(
    *,
    logical_act_id: str,
    partition_ref: dict[str, str],
    required_capture_sha256s: list[str],
    views: list[dict[str, Any]],
) -> dict[str, Any]:
    """Seal one complete, canonical capture presentation for one logical act."""
    if not isinstance(logical_act_id, str) or not logical_act_id:
        raise SchemaRefusal("cross-capture autopsia: logical_act_id is required")
    if not isinstance(required_capture_sha256s, list):
        raise SchemaRefusal(
            "cross-capture autopsia: required_capture_sha256s is not a capture list"
        )
    if not isinstance(views, list):
        raise SchemaRefusal("cross-capture autopsia: views is not a presentation list")
    if not _is_printable_nfc(logical_act_id):
        raise SchemaRefusal(
            "cross-capture autopsia: logical_act_id is not printable NFC; the presentation "
            "is refused because normalization variants cannot name different logical acts"
        )
    # `views` is walked here before `_view` ever proves its shape, and the
    # screen is iterative (its depth is the walk's own list), so a deep
    # unvalidated nest is walked to the bottom and then refused by the shape
    # checks below rather than crashing the interpreter stack.
    _reject_preference({"logical_act_id": logical_act_id, "views": views})
    required = sorted({_sha(item, "required capture sha256") for item in required_capture_sha256s})
    if not required:
        raise SchemaRefusal("cross-capture autopsia: a logical act has no required captures")
    checked_views = [_view(row) for row in views]
    delivered = [row["source_sha256"] for row in checked_views]
    if len(delivered) != len(set(delivered)):
        raise SchemaRefusal(
            "cross-capture autopsia: a capture has more than one view; no view is chosen"
        )
    if set(delivered) != set(required):
        raise SchemaRefusal("cross-capture autopsia: required and delivered capture sets differ")
    checked_views.sort(
        key=lambda row: (row["physical_page_id"], row["source_sha256"], row["view_id"])
    )
    if len({row["view_id"] for row in checked_views}) != len(checked_views):
        raise SchemaRefusal("cross-capture autopsia: duplicate immutable view identity")
    conservation = {
        "required_count": len(required),
        "delivered_count": len(delivered),
        "required_set_digest": digest_of(required),
        "delivered_set_digest": digest_of(sorted(delivered)),
    }
    body = {
        "schema": SCHEMA,
        "logical_act_id": logical_act_id,
        "partition_ref": _ref(partition_ref, "partition_ref"),
        "required_capture_sha256s": required,
        "views": checked_views,
        "member_conservation": conservation,
    }
    return body


def build_autopsia_from_run(
    *,
    run: dict[str, Any],
    logical_act_id: str,
    partition_ref: dict[str, str],
    required_capture_sha256s: list[str],
    views: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build only when every presentation belongs to this run's source ledger."""
    record = build_autopsia(
        logical_act_id=logical_act_id,
        partition_ref=partition_ref,
        required_capture_sha256s=required_capture_sha256s,
        views=views,
    )
    ledger = source_ledger_from_run(run)
    required = set(record["required_capture_sha256s"])
    missing = required - ledger
    if missing:
        # Name them. The operator's next act is to fetch a photograph, and a
        # logical act spanning several captures gives no clue which one unless
        # the refusal says. Sorted so the sentence is the same on every run.
        raise SchemaRefusal(
            "cluster-member-absent: required captures are absent from this run: "
            + ", ".join(sorted(missing))
        )
    return record


def validate_autopsia(value: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "logical_act_id",
            "partition_ref",
            "required_capture_sha256s",
            "views",
            "member_conservation",
        }
        or value.get("schema") != SCHEMA
    ):
        raise SchemaRefusal("cross-capture autopsia: record is not its closed schema")
    rebuilt = build_autopsia(
        logical_act_id=value["logical_act_id"],
        partition_ref=value["partition_ref"],
        required_capture_sha256s=value["required_capture_sha256s"],
        views=value["views"],
    )
    if rebuilt != value:
        raise SchemaRefusal(
            "cross-capture autopsia: record is not canonical or conservation disagrees"
        )
    return rebuilt


def _load(ref: dict[str, str], read_bytes: Callable[[str], bytes]) -> bytes:
    """Recheck bytes here because a sealed path may change before reader delivery."""
    try:
        image = read_bytes(ref["relative_path"])
    except OSError as error:
        raise SchemaRefusal(
            f"cross-capture autopsia: view image {ref['relative_path']!r} could not be read: "
            f"{error}"
        ) from error
    observed = digest_bytes(image)
    if observed != ref["sha256"]:
        raise SchemaRefusal(
            f"cross-capture autopsia: view image {ref['relative_path']!r} no longer matches its "
            f"sealed digest: expected {ref['sha256']}, observed {observed}"
        )
    return image


def _capacity_reason(record: dict[str, Any], max_images: int | None) -> str | None:
    needed = sum(
        len(view["region_refs"]) + len(view["page_render_refs"]) for view in record["views"]
    )
    if (
        max_images is None
        or not isinstance(max_images, int)
        or isinstance(max_images, bool)
        or max_images < needed
    ):
        available = repr(max_images) if max_images is not None else "no sealed image ceiling"
        return (
            f"{OVER_CAPACITY}: complete atomic presentation needs {needed} images but "
            f"max_images provides {available}; no reader call is made and the logical act "
            "remains held"
        )
    return None


def over_capacity_reason(autopsia: dict[str, Any], max_images: int | None) -> str | None:
    """Return a finding before loading; an unsealed ceiling cannot authorize a call."""
    return _capacity_reason(validate_autopsia(autopsia), max_images)


def atomic_delivered_pixels(
    autopsia: dict[str, Any], *, read_bytes: Callable[[str], bytes], max_images: int | None
) -> dict[str, list[bytes]]:
    """Refuse instead of chunking when all pixels cannot fit in one reader request."""
    record = validate_autopsia(autopsia)
    reason = _capacity_reason(record, max_images)
    if reason is not None:
        raise SchemaRefusal(reason)
    regions = [_load(ref, read_bytes) for view in record["views"] for ref in view["region_refs"]]
    pages = [_load(ref, read_bytes) for view in record["views"] for ref in view["page_render_refs"]]
    return {"region_images": regions, "page_render_images": pages}


def assemble_reader_input(
    *,
    autopsia: dict[str, Any],
    dossier: dict[str, Any],
    read_bytes: Callable[[str], bytes],
    max_images: int | None,
) -> tuple[dict[str, Any], dict[str, list[bytes]]]:
    """Return nothing until every view has validated and loaded as one presentation."""
    record = validate_autopsia(autopsia)
    if not isinstance(dossier, dict):
        raise SchemaRefusal("cross-capture autopsia: reader dossier is not an object")
    if dossier.get("logical_act_id") not in (None, record["logical_act_id"]):
        raise SchemaRefusal("cross-capture autopsia: dossier names another logical act")
    pixels = atomic_delivered_pixels(record, read_bytes=read_bytes, max_images=max_images)
    delivered = copy.deepcopy(dossier)
    delivered["logical_act_id"] = record["logical_act_id"]
    delivered["cross_capture_autopsia"] = record
    # Late fields must meet the preference guard before they can condition even
    # a failed or unpublished invocation.
    _reject_preference(delivered)
    # The logical identity and presentation are added after dossier construction,
    # so the pre-transport digest cannot describe the object the reader receives.
    if "dossier_digest" in delivered:
        body = {key: value for key, value in delivered.items() if key != "dossier_digest"}
        delivered["dossier_digest"] = digest_of(body)
    return delivered, pixels


def invoke_one_logical_read(
    reader: Any,
    *,
    autopsia: dict[str, Any],
    dossier: dict[str, Any],
    read_bytes: Callable[[str], bytes],
    max_images: int | None,
    pass_kind: str,
) -> tuple[dict[str, Any], dict[str, list[bytes]], Any]:
    """Expose one result, dossier, and pixel set; no per-view result can be reconciled."""
    delivered, pixels = assemble_reader_input(
        autopsia=autopsia,
        dossier=dossier,
        read_bytes=read_bytes,
        max_images=max_images,
    )
    return delivered, pixels, reader.read(delivered, pass_kind=pass_kind, delivered_pixels=pixels)


def dissent_shell(
    *,
    perlectio_ref: dict[str, str],
    autopsia: dict[str, Any],
    reader_invocation_ref: dict[str, str],
    response_observation_digest: str,
) -> dict[str, Any]:
    """Accept only post-reading references, so dissent cannot become reader input."""
    record = validate_autopsia(autopsia)
    shell: dict[str, Any] = {
        "schema": DISSENT_SCHEMA,
        "perlectio_ref": _ref(perlectio_ref, "perlectio_ref"),
        "logical_act_id": record["logical_act_id"],
        "partition_ref": record["partition_ref"],
        "views": [
            {
                "view_id": view["view_id"],
                "source_sha256": view["source_sha256"],
                "region_refs": view["region_refs"],
            }
            for view in record["views"]
        ],
        "capture_pairs": [
            [left, right]
            for offset, left in enumerate(record["required_capture_sha256s"])
            for right in record["required_capture_sha256s"][offset + 1 :]
        ],
    }
    shell["reader_invocation_ref"] = _ref(reader_invocation_ref, "reader_invocation_ref")
    shell["response_observation_digest"] = _sha(
        response_observation_digest, "response_observation_digest"
    )
    return shell


def cross_capture_audit_scope(autopsia: dict[str, Any]) -> dict[str, list[str]]:
    """The full cross-capture audit denominator, with no representative page."""
    record = validate_autopsia(autopsia)
    return {"page_ids": sorted({page for view in record["views"] for page in view["page_ids"]})}
