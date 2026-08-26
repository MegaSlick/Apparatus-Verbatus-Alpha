"""The one atomic, all-capture presentation used for a logical Perlectio.

This module intentionally has no reader and no text.  It is the data boundary
that makes a reader receive every capture in one request; a caller cannot turn
its views into capture-local readings and reconcile those strings afterwards.
"""

from __future__ import annotations

import copy
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


def _ref(value: Any, what: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"cross-capture autopsia: {what} is not a digest-bound reference")
    if not isinstance(value["relative_path"], str) or not value["relative_path"]:
        raise SchemaRefusal(f"cross-capture autopsia: {what} has no path")
    _sha(value["sha256"], f"{what} sha256")
    return dict(value)


def _ref_list(value: Any, what: str) -> list[dict[str, str]]:
    """Every image reference a view names, kept at its delivered cardinality.

    Two distinct regions can legitimately cut byte-identical crops -- an
    ink-free page's several recovery attempts over blank ground are the case
    this exists for -- and content-addressed storage then gives them the same
    ``relative_path``/``sha256``. That is not a caller repeating one
    reference; it is two references that happen to match, and each still
    names a real basis the reader must receive its own image slot for
    (``dossier.regions`` and the delivered pixel list stay one-to-one with
    the region set upstream of this schema, consult §3.1). Collapsing or
    refusing the pair would either under-deliver a region or turn an honest
    coincidence into a refusal, so every reference here is retained exactly
    as given; only its shape is checked.
    """
    if not isinstance(value, list) or not value:
        raise SchemaRefusal(f"cross-capture autopsia: {what} must retain every image reference")
    refs = [_ref(item, what) for item in value]
    return sorted(refs, key=lambda item: (item["relative_path"], item["sha256"]))


def _reject_preference(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _FORBIDDEN):
                raise SchemaRefusal(f"cross-capture autopsia: forbidden preference field {key!r}")
            _reject_preference(item)
    elif isinstance(value, list):
        for item in value:
            _reject_preference(item)


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
        raise SchemaRefusal("cluster-member-absent: required capture is absent from this run")
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
    """One view image, proved to be the bytes its sealed reference names.

    The reference carries the digest the Designator/Exemplar boundary sealed
    upstream, so checking it here is what makes "every view image is a direct
    digest-bound input" (consult §3.1) a check rather than a description.  The
    per-pass reader dossier this transport replaced verified every delivered
    crop the same way (``dossier.py::_delivered_images``); a transport that
    handed a reader whatever bytes now sit at the path would have quietly
    dropped that guard on the way through.
    """
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


def over_capacity_reason(autopsia: dict[str, Any], max_images: int | None) -> str | None:
    """The named capacity finding for this presentation, or ``None``.

    Separated from the loader so a caller can reach the answer *before* it has
    committed to a reading: consult §3.1 makes an over-capacity cluster a named
    finding and a ``not-run`` Perlectio for that logical act, which a producer
    can only publish if it can ask the question without the asking itself being
    the refusal.  ``None`` is intentionally not treated as infinity: an
    unmeasured serving recipe has no authority to receive a cluster.
    """
    record = validate_autopsia(autopsia)
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


def atomic_delivered_pixels(
    autopsia: dict[str, Any], *, read_bytes: Callable[[str], bytes], max_images: int | None
) -> dict[str, list[bytes]]:
    """Materialize all view pixels for one reader request, or refuse before it.

    The refusal is unconditional here even though ``over_capacity_reason`` lets
    a producer route the same fact to a ``not-run`` Perlectio: a caller that
    reached this function with a presentation that does not fit is asking for
    pixels it cannot deliver in one request, and the only alternative to
    refusing is chunking them.
    """
    record = validate_autopsia(autopsia)
    reason = over_capacity_reason(record, max_images)
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
    """Bind one logical dossier to its complete atomic pixel presentation.

    This is deliberately the *only* cross-capture transport constructor.  It
    validates and loads every view before returning anything a reader can be
    called with.  Consequently a caller cannot receive one capture's pixels,
    call a reader, and later ask for another capture as a fallback.
    """
    record = validate_autopsia(autopsia)
    if not isinstance(dossier, dict):
        raise SchemaRefusal("cross-capture autopsia: reader dossier is not an object")
    if dossier.get("logical_act_id") not in (None, record["logical_act_id"]):
        raise SchemaRefusal("cross-capture autopsia: dossier names another logical act")
    pixels = atomic_delivered_pixels(record, read_bytes=read_bytes, max_images=max_images)
    delivered = copy.deepcopy(dossier)
    delivered["logical_act_id"] = record["logical_act_id"]
    delivered["cross_capture_autopsia"] = record
    # The transport is the last boundary before the reader. Production dossiers
    # were swept when built, but a late field added by any caller must refuse
    # here, before it can condition even a failed or unpublished invocation.
    _reject_preference(delivered)
    # A real dossier arrives with a digest sealing its pre-transport fields.
    # Adding the logical identity and presentation after that seal and then
    # calling the reader would hand it an object whose own integrity claim is
    # already false. Re-seal before the invocation; publication may verify the
    # same digest again, but it must describe the bytes the reader received.
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
    """Make exactly one reader call from an all-capture presentation.

    The return keeps the delivered dossier and pixels beside the result so a
    publisher can bind the invocation to the exact evidence.  There is no
    per-view callback surface and no list of results to reconcile.
    """
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
    """19B's post-reading dissent handoff, derived after the Perlectio.

    The shell accepts only references to the already-published invocation and
    its non-establishing observation section. It cannot be a reader input.
    """
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
