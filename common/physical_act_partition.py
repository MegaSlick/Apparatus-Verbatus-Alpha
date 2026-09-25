"""Closed correspondence preparation and logical-act partition contracts.

This is deliberately independent of a reader.  Discovery can prove geometry and
append an immutable register declaration; production then consumes a fresh
snapshot and produces the denominator that later stages must honour.

The source ledger comes from the sealed source manifest rather than local
proposals, so an unproposed active member remains required. Production narrows
that ledger only by independently verifying Exemplar page lineage.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from typing import Any, Final

from common.contracts.canonical import is_sha256, self_hash, self_hash_refusal, verify_self_hash
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import (
    act_id as local_act_id,
)
from common.contracts.identities import (
    is_well_formed,
    physical_act_component_designation,
    physical_act_id,
)
from common.corpus_register import (
    append_records,
    members_of,
    membership_heads,
    physical_act_page,
    refuse_capture_preference,
    resolve_proposal,
)
from common.corpus_register import register_digest as read_register_digest

PARTITION_SCHEMA: Final = "physical-act-partition.v1"
PROPOSAL_SCHEMA: Final = "correspondence-proposal.v1"
# The Perlector's hold on an act whose capture the register clusters, read by the Recensor.
CROSS_CAPTURE_READ_NOT_BUILT: Final = "cross-capture-read-not-built"
_TEXTUAL_FIELDS: Final = frozenset(
    {"text", "ocr", "testimonium", "lectio", "perlectio", "edit_distance"}
)


def source_ledger_from_run(run: dict[str, Any]) -> set[str]:
    """Use the source manifest so a capture missed by proposals remains required."""
    rows = run.get("source_manifest") if isinstance(run, dict) else None
    if not isinstance(rows, list) or not rows:
        raise SchemaRefusal("physical-act partition: run has no sealed source manifest")
    ledger: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SchemaRefusal("physical-act partition: source manifest row is not an object")
        ledger.add(_sha(row.get("sha256"), "source manifest sha256"))
    return ledger


def _refuse_preference(value: Any) -> None:
    refuse_capture_preference(value, what="physical-act partition")


def _refuse_textual(value: Any) -> None:
    """Refuse textual evidence anywhere in an untrusted proposal payload.

    Iterative, so a deep payload is a named refusal rather than a
    `RecursionError`, and a self-containing one is refused rather than looped
    on. Only containers open on the current path are tracked, so a component
    shared between siblings is still walked wherever it appears.
    """
    pending: list[tuple[str, Any]] = [("value", value)]
    open_path: set[int] = set()
    while pending:
        kind, current = pending.pop()
        if kind == "exit":
            open_path.discard(current)
            continue
        if isinstance(current, (dict, list, tuple)):
            marker = id(current)
            if marker in open_path:
                raise SchemaRefusal(
                    "correspondence proposal: a proposal contains itself, so no sweep of "
                    "it can terminate and textual evidence below the loop could never be "
                    "found. Rebuild the proposal from values that are not their own "
                    "ancestors."
                )
            open_path.add(marker)
            pending.append(("exit", marker))
        if isinstance(current, dict):
            if set(current) & _TEXTUAL_FIELDS:
                raise SchemaRefusal(
                    "correspondence proposal: textual evidence cannot match physical acts"
                )
            pending.extend(("value", item) for item in current.values())
        elif isinstance(current, (list, tuple)):
            # A tuple serializes exactly like a list through `canonical_bytes`.
            pending.extend(("value", item) for item in current)


def _findings(code: str, acts: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"code": code, "act_id": row["act_id"]} for row in acts]


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _refuse_unverified_self_hash(payload: dict[str, Any], subject: str, noun: str) -> None:
    if verify_self_hash(payload):
        return
    unhashable = self_hash_refusal(payload)
    if unhashable is not None:
        raise SchemaRefusal(f"{subject}: self hash cannot be verified: {unhashable}")
    raise SchemaRefusal(f"{subject}: self hash does not match the sealed {noun}")


def _finding_pairs(findings: list[Any], subject: str) -> list[tuple[str, str]]:
    """Closed findings as sorted, unique `(act_id, code)` pairs, or refuse."""
    pairs: list[tuple[str, str]] = []
    for row in findings:
        if (
            not isinstance(row, dict)
            or set(row) != {"code", "act_id"}
            or not isinstance(row["code"], str)
            or not row["code"]
            or not _is_derived_id(row["act_id"], "act_")
        ):
            raise SchemaRefusal(f"{subject}: finding is not closed")
        pairs.append((row["act_id"], row["code"]))
    if pairs != sorted(set(pairs)):
        raise SchemaRefusal(f"{subject}: findings are not sorted unique facts")
    return pairs


def _dedupe_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    """One finding per (code, act_id): a repeated cause is not a repeated fact."""
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for finding in findings:
        seen[(finding["code"], finding["act_id"])] = finding
    return list(seen.values())


def _finding_sort_key(row: dict[str, str]) -> tuple[str, str]:
    return row["act_id"], row["code"]


def _sha(value: Any, what: str) -> str:
    if not is_sha256(value):
        raise SchemaRefusal(f"physical-act partition: {what} must be a lowercase SHA-256")
    return value


def _is_derived_id(value: Any, prefix: str) -> bool:
    """A derived identity of one kind: `is_well_formed` accepts every prefix."""
    return is_well_formed(value) and value.startswith(prefix)


def _path(value: Any, what: str) -> str:
    # A sealed reference is relative to the run root, as in
    # `common/contracts/envelope.py::validate_input_refs`.
    if not isinstance(value, str) or not value:
        raise SchemaRefusal(f"physical-act partition: {what} path is not a non-empty string")
    if value.startswith("/") or ".." in value.split("/"):
        raise SchemaRefusal(f"physical-act partition: {what} path {value!r} escapes the run tree")
    return value


def _act(row: Any, *, require_bindings: bool = False) -> dict[str, Any]:
    base = {"act_id", "act_key", "page_id", "page_ordinal", "source_sha256", "proposal_refs"}
    bindings = {"act_class", "act_bounds"}
    if not isinstance(row, dict) or not (base <= set(row) <= base | bindings):
        raise SchemaRefusal("physical-act partition: local act must use its closed lineage shape")
    if require_bindings and not bindings <= set(row):
        # The register re-derives an appended act_id from its class and bounds;
        # a Designator-sealed partition row has no bindings.
        raise SchemaRefusal(
            "physical-act partition: a correspondence-bound local act must carry "
            "its act_class and minted act_bounds"
        )
    if not all(
        isinstance(row[name], str) and row[name] for name in ("act_id", "act_key", "page_id")
    ):
        raise SchemaRefusal("physical-act partition: local act lacks immutable identity lineage")
    if not _is_derived_id(row["act_id"], "act_") or not _is_derived_id(row["page_id"], "pg_"):
        raise SchemaRefusal("physical-act partition: local act identities are malformed")
    if (
        not row["act_key"].isprintable()
        or unicodedata.normalize("NFC", row["act_key"]) != row["act_key"]
    ):
        raise SchemaRefusal(
            "physical-act partition: local act key is not printable NFC; the partition is "
            "refused because normalization variants cannot be separate denominator keys"
        )
    if bindings <= set(row):
        try:
            expected_act = local_act_id(row["page_id"], row["act_class"], row["act_bounds"])
        except ContractError as error:
            raise SchemaRefusal(
                f"physical-act partition: local act bindings are malformed: {error}"
            ) from error
        if row["act_id"] != expected_act:
            raise SchemaRefusal(
                "physical-act partition: local act_id does not derive from its own "
                "page, class, and minted bounds"
            )
    if not _integer(row["page_ordinal"]) or row["page_ordinal"] < 0:
        raise SchemaRefusal(
            "physical-act partition: local act page ordinal is negative, boolean, or not an "
            "integer; the partition is refused because source-page attribution must be a "
            "non-negative manifest index"
        )
    _sha(row["source_sha256"], "local act source_sha256")
    if (
        not isinstance(row["proposal_refs"], list)
        or not row["proposal_refs"]
        or not all(isinstance(x, str) and x for x in row["proposal_refs"])
    ):
        raise SchemaRefusal("physical-act partition: local act must retain proposal references")
    # A set: the producer's traversal order is not a durable byte.
    row["proposal_refs"] = sorted(set(row["proposal_refs"]))
    return row


def _alignment(row: Any) -> dict[str, Any]:
    required = {"page_id", "source_sha256", "physical_page_id", "alignment_ref"}
    if not isinstance(row, dict) or set(row) != required:
        raise SchemaRefusal("physical-act partition: capture alignment must use its closed shape")
    if not all(isinstance(row[name], str) and row[name] for name in required):
        raise SchemaRefusal("physical-act partition: capture alignment is incomplete")
    if not _is_derived_id(row["page_id"], "pg_") or not _is_derived_id(
        row["physical_page_id"], "ppg_"
    ):
        raise SchemaRefusal("physical-act partition: capture alignment names malformed identities")
    _sha(row["source_sha256"], "capture alignment source_sha256")
    return row


def _presentation_index(
    alignments: list[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Every ``(physical page, capture)`` an alignment table declares.

    One capture may reach one physical page through several rendered pages (a
    whole opening and its split half), so the page ids are a union. Two rows
    aligning one capture to one page by different references are refused.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in alignments:
        key = (row["physical_page_id"], row["source_sha256"])
        entry = index.get(key)
        if entry is None:
            index[key] = {"page_ids": {row["page_id"]}, "alignment_ref": row["alignment_ref"]}
            continue
        if entry["alignment_ref"] != row["alignment_ref"]:
            raise SchemaRefusal(
                "physical-act partition: one capture is aligned to one physical page by two "
                "different alignment references; which one holds is not a choice this makes"
            )
        entry["page_ids"].add(row["page_id"])
    return index


def _presentation(
    presentations: dict[tuple[str, str], dict[str, Any]],
    index: dict[tuple[str, str], dict[str, Any]],
    page: str,
    source: str,
) -> dict[str, Any]:
    """The one presentation row for one capture of one physical page, made once from the index."""
    key = (page, source)
    row = presentations.get(key)
    if row is None:
        entry = index[key]
        row = presentations[key] = {
            "physical_page_id": page,
            "source_sha256": source,
            "page_ids": sorted(entry["page_ids"]),
            "local_act_ids": [],
            "alignment_ref": entry["alignment_ref"],
            "projected_view_refs": [],
        }
    return row


def _accepted_record_sort_key(row: dict[str, Any]) -> tuple[int, str, str, str]:
    return (
        0 if row["kind"] == "physical-act" else 1,
        row["physical_page_id"],
        row["physical_act_id"],
        row.get("act_id", ""),
    )


def _logical_scope(
    act: dict[str, Any],
    *,
    register: bytes,
    source_ledger: set[str],
    by_page: dict[str, dict[str, Any]],
    clustered_sources: set[str],
) -> str | tuple[str, tuple[str, str | None, str | None]]:
    """One local act's logical act and identity scope, or the finding code that holds it."""
    if act["source_sha256"] not in source_ledger:
        return "local-source-absent"
    alignment = by_page.get(act["page_id"])
    if alignment is None:
        # A clustered capture missing from the alignment table is unaligned, not a
        # singleton: a singleton would duplicate the logical act it belongs to.
        if act["source_sha256"] in clustered_sources:
            return "capture-page-alignment-unresolved"
        return act["act_id"], ("image-local-singleton", None, None)
    if alignment["source_sha256"] != act["source_sha256"]:
        raise SchemaRefusal(
            "physical-act partition: local act source_sha256 does not match its "
            "page's capture alignment"
        )
    page = alignment["physical_page_id"]
    members = members_of(register, page)
    # No declared member, or a capture outside the declared cluster.
    if not members or alignment["source_sha256"] not in members:
        return "capture-page-alignment-unresolved"
    if any(member not in source_ledger for member in members):
        return "cluster-member-absent"
    resolved = resolve_proposal(register, act["act_id"])
    if resolved["outcome"] != "resolved":
        return resolved["code"]
    if resolved["page_id"] != act["page_id"]:
        return "correspondence-page-mismatch"
    # The register minted the act on another physical page than this capture is
    # aligned to; neither side is taken.
    if resolved["physical_page_id"] != page:
        return "capture-page-alignment-unresolved"
    logical = resolved["physical_act_id"]
    return logical, ("physical-act", logical, page)


def build_physical_act_partition(
    *,
    register: bytes,
    register_digest: str,
    proposal_seal_ref: dict[str, str],
    local_acts: list[dict[str, Any]],
    capture_alignments: list[dict[str, Any]],
    source_ledger: set[str],
) -> dict[str, Any]:
    """Build the total production denominator from a fresh register snapshot.

    Every local act aligned to a registered physical page either resolves through
    its declared correspondence or emits a named finding. It is never silently
    downgraded to a singleton. ``source_ledger`` is independent of whichever
    local acts happened to be proposed.
    """
    _refuse_preference({"local_acts": local_acts, "capture_alignments": capture_alignments})
    _sha(register_digest, "register_digest")
    # The sealed digest must be of the bytes grouped, or a mid-build append would
    # attribute this grouping to another register. This also validates the register.
    if read_register_digest(register) != register_digest:
        raise IncompatibleReuse(
            "physical-act partition: register_digest is not the digest of the register bytes "
            "this partition was built from; the register moved while it was being built"
        )
    if not isinstance(proposal_seal_ref, dict) or set(proposal_seal_ref) != {
        "relative_path",
        "sha256",
    }:
        raise SchemaRefusal("physical-act partition: proposal seal reference is not digest-bound")
    _path(proposal_seal_ref["relative_path"], "proposal seal")
    _sha(proposal_seal_ref["sha256"], "proposal seal sha256")
    if not isinstance(local_acts, list) or not local_acts:
        raise SchemaRefusal("physical-act partition: no local expected acts are not a denominator")
    if not isinstance(capture_alignments, list):
        raise SchemaRefusal("physical-act partition: capture alignments must be a list")
    if not isinstance(source_ledger, set) or not all(is_sha256(source) for source in source_ledger):
        raise SchemaRefusal(
            "physical-act partition: source ledger must be a set of lowercase SHA-256 digests"
        )
    acts = [_act(dict(row)) for row in local_acts]
    if len({row["act_id"] for row in acts}) != len(acts):
        raise SchemaRefusal("physical-act partition: a local act occurs more than once")
    if len({row["act_key"] for row in acts}) != len(acts):
        raise SchemaRefusal(
            "physical-act partition: a local act key occurs more than once; the partition is "
            "refused because two proposal rows cannot share one export key"
        )
    alignments = [_alignment(dict(row)) for row in capture_alignments]
    by_page = {row["page_id"]: row for row in alignments}
    if len(by_page) != len(alignments):
        raise SchemaRefusal(
            "physical-act partition: a capture page has more than one physical alignment"
        )
    index = _presentation_index(alignments)
    clustered_sources = {
        source
        for _page, (_digest, members) in membership_heads(register).items()
        for source in members
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scopes: dict[str, tuple[str, str | None, str | None]] = {}
    findings: list[dict[str, str]] = []
    for act in acts:
        resolved = _logical_scope(
            act,
            register=register,
            source_ledger=source_ledger,
            by_page=by_page,
            clustered_sources=clustered_sources,
        )
        if isinstance(resolved, str):
            findings.append({"code": resolved, "act_id": act["act_id"]})
            continue
        logical, scope = resolved
        scopes[logical] = scope
        groups[logical].append(act)
    logical_acts: list[dict[str, Any]] = []
    for logical in sorted(groups):
        members = sorted(groups[logical], key=lambda row: row["act_id"])
        scope, physical, physical_page = scopes[logical]
        sources = [row["source_sha256"] for row in members]
        if len(sources) != len(set(sources)):
            findings.extend(_findings("ambiguous-physical-act", members))
            continue
        components: dict[str, set[str]] = defaultdict(set)
        presentations: dict[tuple[str, str], dict[str, Any]] = {}
        if physical is not None:
            required = members_of(register, physical_page)
            if any((physical_page, source) not in index for source in required):
                findings.extend(_findings("capture-page-alignment-unresolved", members))
                continue
            components[physical_page].update(required)
            for source in required:
                _presentation(presentations, index, physical_page, source)
        for row in members:
            alignment = by_page.get(row["page_id"])
            if alignment:
                components[alignment["physical_page_id"]].add(row["source_sha256"])
                _presentation(
                    presentations, index, alignment["physical_page_id"], row["source_sha256"]
                )["local_act_ids"].append(row["act_id"])
        logical_acts.append(
            {
                "logical_act_id": logical,
                "identity_scope": scope,
                "physical_act_id": physical,
                "physical_page_components": [
                    {"physical_page_id": key, "required_capture_sha256s": sorted(value)}
                    for key, value in sorted(components.items())
                ],
                # The bindings are intake facts; the sealed row is the six-field shape.
                "member_local_acts": [
                    {
                        key: value
                        for key, value in member.items()
                        if key not in ("act_class", "act_bounds")
                    }
                    for member in members
                ],
                "capture_presentations": [presentations[key] for key in sorted(presentations)],
            }
        )
    mapped = {row["act_id"] for group in logical_acts for row in group["member_local_acts"]}
    for act in acts:
        if act["act_id"] not in mapped and not any(f["act_id"] == act["act_id"] for f in findings):
            findings.append({"code": "unresolved-physical-act", "act_id": act["act_id"]})
    payload = {
        "schema": PARTITION_SCHEMA,
        "register_digest": register_digest,
        "proposal_seal_ref": proposal_seal_ref,
        "local_expected_count": len(acts),
        "logical_expected_count": len(logical_acts),
        "logical_acts": logical_acts,
        "local_to_logical": [
            {"act_id": row["act_id"], "logical_act_id": group["logical_act_id"]}
            for group in logical_acts
            for row in group["member_local_acts"]
        ],
        "findings": sorted(_dedupe_findings(findings), key=_finding_sort_key),
    }
    payload["self_hash"] = self_hash(payload)
    return validate_physical_act_partition(payload)


def _component_pairs(components: Any) -> set[tuple[str, str]]:
    """Every required `(physical page, capture)` of a closed component list."""
    if not isinstance(components, list):
        raise SchemaRefusal("physical-act partition: physical page components are invalid")
    component_pairs: set[tuple[str, str]] = set()
    component_pages: list[str] = []
    for component in components:
        if not isinstance(component, dict) or set(component) != {
            "physical_page_id",
            "required_capture_sha256s",
        }:
            raise SchemaRefusal("physical-act partition: physical page component is not closed")
        page = component["physical_page_id"]
        required_captures = component["required_capture_sha256s"]
        if (
            not _is_derived_id(page, "ppg_")
            or not isinstance(required_captures, list)
            or not required_captures
            or not all(isinstance(item, str) for item in required_captures)
            or required_captures != sorted(set(required_captures))
        ):
            raise SchemaRefusal(
                "physical-act partition: physical-page component has a malformed identity "
                "or required-capture set; the partition is refused because its capture "
                "denominator must be a non-empty canonical set"
            )
        for source in required_captures:
            _sha(source, "required capture source_sha256")
            component_pairs.add((page, source))
        component_pages.append(page)
    if component_pages != sorted(set(component_pages)):
        raise SchemaRefusal(
            "physical-act partition: physical page components are not sorted unique pages"
        )
    return component_pairs


def _presentation_facts(presentations: Any) -> tuple[set[tuple[str, str]], list[str]]:
    """The `(physical page, capture)` pairs and local act ids a closed presentation list names."""
    if not isinstance(presentations, list):
        raise SchemaRefusal("physical-act partition: capture presentations are invalid")
    presented_local_ids: list[str] = []
    presentation_keys: list[tuple[str, str]] = []
    for presentation in presentations:
        if not isinstance(presentation, dict) or set(presentation) != {
            "physical_page_id",
            "source_sha256",
            "page_ids",
            "local_act_ids",
            "alignment_ref",
            "projected_view_refs",
        }:
            raise SchemaRefusal("physical-act partition: capture presentation is not closed")
        page = presentation["physical_page_id"]
        source = presentation["source_sha256"]
        page_ids = presentation["page_ids"]
        local_ids = presentation["local_act_ids"]
        projected = presentation["projected_view_refs"]
        if not _is_derived_id(page, "ppg_"):
            raise SchemaRefusal(
                "physical-act partition: capture presentation physical_page_id is not a "
                "recognized derived identity; the partition is refused because capture "
                "evidence cannot attach to a free-form page key"
            )
        _sha(source, "capture presentation source_sha256")
        if (
            not isinstance(page_ids, list)
            or not page_ids
            or not all(isinstance(item, str) for item in page_ids)
            or page_ids != sorted(set(page_ids))
            or not all(_is_derived_id(item, "pg_") for item in page_ids)
            or not isinstance(local_ids, list)
            or not all(isinstance(item, str) for item in local_ids)
            or local_ids != sorted(set(local_ids))
            or not all(_is_derived_id(item, "act_") for item in local_ids)
            or not isinstance(presentation["alignment_ref"], str)
            or not presentation["alignment_ref"]
            or not isinstance(projected, list)
            or not all(isinstance(item, str) and item for item in projected)
            or projected != sorted(set(projected))
        ):
            raise SchemaRefusal("physical-act partition: capture presentation is malformed")
        presentation_keys.append((page, source))
        presented_local_ids.extend(local_ids)
    if presentation_keys != sorted(set(presentation_keys)):
        raise SchemaRefusal(
            "physical-act partition: capture presentations are not in canonical set order"
        )
    return set(presentation_keys), presented_local_ids


def validate_physical_act_partition(payload: dict[str, Any]) -> dict[str, Any]:
    _refuse_preference(payload)
    if not isinstance(payload, dict) or payload.get("schema") != PARTITION_SCHEMA:
        raise SchemaRefusal("physical-act partition: invalid schema")
    _refuse_unverified_self_hash(payload, "physical-act partition", "partition")
    required = {
        "schema",
        "register_digest",
        "proposal_seal_ref",
        "local_expected_count",
        "logical_expected_count",
        "logical_acts",
        "local_to_logical",
        "findings",
        "self_hash",
    }
    if set(payload) != required:
        raise SchemaRefusal("physical-act partition: record is not closed")
    _sha(payload["register_digest"], "register_digest")
    seal = payload["proposal_seal_ref"]
    if (
        not isinstance(seal, dict)
        or set(seal) != {"relative_path", "sha256"}
        or not isinstance(seal["relative_path"], str)
        or not seal["relative_path"]
    ):
        raise SchemaRefusal("physical-act partition: proposal seal reference is not closed")
    _path(seal["relative_path"], "proposal seal")
    _sha(seal["sha256"], "proposal seal sha256")
    if any(
        not _integer(payload[name]) or payload[name] < 0
        for name in ("local_expected_count", "logical_expected_count")
    ):
        raise SchemaRefusal("physical-act partition: expected counts are invalid")

    local_rows = payload["local_to_logical"]
    if not isinstance(local_rows, list) or not isinstance(payload["findings"], list):
        raise SchemaRefusal("physical-act partition: local correspondence is not one-to-one")
    if any(
        not isinstance(row, dict)
        or set(row) != {"act_id", "logical_act_id"}
        or not isinstance(row["act_id"], str)
        or not isinstance(row["logical_act_id"], str)
        for row in local_rows
    ):
        raise SchemaRefusal("physical-act partition: local correspondence row is not closed")
    mapped = {row.get("act_id") for row in local_rows if isinstance(row, dict)}
    if len(mapped) != len(local_rows):
        raise SchemaRefusal("physical-act partition: local correspondence is not one-to-one")
    groups = payload["logical_acts"]
    if not isinstance(groups, list) or payload["logical_expected_count"] != len(groups):
        raise SchemaRefusal(
            "physical-act partition: logical_expected_count does not count the logical acts"
        )
    group_fields = {
        "logical_act_id",
        "identity_scope",
        "physical_act_id",
        "physical_page_components",
        "member_local_acts",
        "capture_presentations",
    }
    reconstructed: list[dict[str, str]] = []
    published: set[str] = set()
    published_member_keys: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != group_fields:
            raise SchemaRefusal("physical-act partition: logical act row is not closed")
        logical = group["logical_act_id"]
        members = group["member_local_acts"]
        if (
            not isinstance(logical, str)
            or not logical
            or logical in published
            or not isinstance(members, list)
            or not members
        ):
            raise SchemaRefusal(
                "physical-act partition: logical act identity or members are invalid"
            )
        published.add(logical)
        parsed_members = [_act(dict(member)) for member in members]
        if members != parsed_members:
            raise SchemaRefusal(
                "physical-act partition: proposal references are not in canonical set order"
            )
        if [member["act_id"] for member in parsed_members] != sorted(
            {member["act_id"] for member in parsed_members}
        ):
            raise SchemaRefusal(
                "physical-act partition: logical act members are not sorted unique acts"
            )
        member_sources = [member["source_sha256"] for member in parsed_members]
        if len(member_sources) != len(set(member_sources)):
            raise SchemaRefusal(
                "physical-act partition: one logical act has two local acts from one capture"
            )
        member_keys = {member["act_key"] for member in parsed_members}
        if len(member_keys) != len(parsed_members) or member_keys & published_member_keys:
            raise SchemaRefusal(
                "physical-act partition: a local act key occurs in more than one proposal "
                "row; the partition is refused because member accounting must be one-to-one"
            )
        published_member_keys.update(member_keys)

        components = group["physical_page_components"]
        component_pairs = _component_pairs(components)
        presentations = group["capture_presentations"]
        presentation_pairs, presented_local_ids = _presentation_facts(presentations)

        scope = group["identity_scope"]
        physical = group["physical_act_id"]
        if scope == "image-local-singleton":
            if len(parsed_members) != 1:
                raise SchemaRefusal(
                    "physical-act partition: image-local singleton has clustered fields"
                )
            (singleton_member,) = parsed_members
            if (
                physical is not None
                or logical != singleton_member["act_id"]
                or components
                or presentations
            ):
                raise SchemaRefusal(
                    "physical-act partition: image-local singleton has clustered fields"
                )
        elif scope == "physical-act":
            if (
                physical != logical
                or not _is_derived_id(logical, "pac_")
                or not components
                or presentation_pairs != component_pairs
            ):
                raise SchemaRefusal(
                    "physical-act partition: required capture presentation set is incomplete"
                )
            member_ids = {member["act_id"] for member in parsed_members}
            if sorted(presented_local_ids) != sorted(member_ids):
                raise SchemaRefusal(
                    "physical-act partition: presentation local acts do not equal group members"
                )
            for member in parsed_members:
                matches = [
                    presentation
                    for presentation in presentations
                    if member["act_id"] in presentation["local_act_ids"]
                ]
                if len(matches) != 1:
                    raise SchemaRefusal(
                        "physical-act partition: member lineage does not match its presentation"
                    )
                (match,) = matches
                if (
                    match["source_sha256"] != member["source_sha256"]
                    or member["page_id"] not in match["page_ids"]
                ):
                    raise SchemaRefusal(
                        "physical-act partition: member lineage does not match its presentation"
                    )
        else:
            raise SchemaRefusal("physical-act partition: unknown identity_scope")
        reconstructed.extend(
            {"act_id": member["act_id"], "logical_act_id": logical} for member in parsed_members
        )
    if [group["logical_act_id"] for group in groups] != sorted(published):
        raise SchemaRefusal("physical-act partition: logical acts are not in canonical set order")
    if {row.get("logical_act_id") for row in local_rows if isinstance(row, dict)} - published:
        raise SchemaRefusal(
            "physical-act partition: a local act is mapped to a logical act the record does "
            "not publish"
        )
    if local_rows != reconstructed:
        raise SchemaRefusal(
            "physical-act partition: local_to_logical does not equal the published group members"
        )
    held = {act for act, _code in _finding_pairs(payload["findings"], "physical-act partition")}
    # Mapped and held are disjoint, or one act could cover another's disappearance
    # in a sum that still reaches the expected count.
    if mapped & held:
        raise SchemaRefusal(
            "physical-act partition: an act is both mapped to a logical act and held by a "
            "finding; it is one or the other"
        )
    if len(mapped) + len(held) != payload["local_expected_count"]:
        raise SchemaRefusal("physical-act partition: local denominator is not total")
    return payload


def build_correspondence_proposal(
    *,
    register: bytes,
    register_digest: str,
    discovery_run_id: str,
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    """Turn exact-one geometric components into an appendable, sealed proposal.

    ``components`` is geometry-only. A component with a finding contributes no
    records, so the register writer cannot turn ambiguity into a mint. The
    register is read, not merely named: minting from geometry alone would split
    one physical act in two across overlapping discovery runs, depending only
    on which ran first (consult §2.2).
    """
    _refuse_preference(components)
    _refuse_textual(components)
    _sha(register_digest, "register_digest")
    if read_register_digest(register) != register_digest:
        raise IncompatibleReuse(
            "correspondence proposal: register_digest is not the digest of the register bytes "
            "this proposal was resolved against"
        )
    if (
        not isinstance(discovery_run_id, str)
        or not discovery_run_id
        or not isinstance(components, list)
    ):
        raise SchemaRefusal("correspondence proposal: discovery identity or components are invalid")
    parsed: list[dict[str, Any]] = []
    act_component_count: dict[str, int] = defaultdict(int)
    for component in components:
        if not isinstance(component, dict) or set(component) != {
            "physical_page_id",
            "physical_act_id",
            "local_acts",
            "evidence",
            "finding",
        }:
            raise SchemaRefusal(
                "correspondence proposal: component is not a closed geometry record"
            )
        page = component["physical_page_id"]
        existing = component["physical_act_id"]
        local = component["local_acts"]
        evidence = component["evidence"]
        finding = component["finding"]
        if not _is_derived_id(page, "ppg_") or not isinstance(local, list) or not local:
            raise SchemaRefusal("correspondence proposal: component lacks page or local acts")
        if existing is not None and (not _is_derived_id(existing, "pac_")):
            raise SchemaRefusal(
                "correspondence proposal: existing physical act identity is malformed"
            )
        acts = [_act(dict(row), require_bindings=True) for row in local]
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(x, str) and x for x in evidence)
        ):
            raise SchemaRefusal("correspondence proposal: component lacks geometric evidence")
        if finding is not None and (not isinstance(finding, str) or not finding):
            raise SchemaRefusal("correspondence proposal: component finding is malformed")
        for row in acts:
            act_component_count[row["act_id"]] += 1
        parsed.append(
            {
                "page": page,
                "existing": existing,
                "acts": acts,
                "evidence": sorted(set(evidence)),
                "finding": finding,
            }
        )

    # Resolve everything before emitting, so listing order takes nothing.
    plans: list[dict[str, Any]] = []
    target_count: dict[str, int] = defaultdict(int)
    for component in parsed:
        page = component["page"]
        existing = component["existing"]
        acts = component["acts"]
        finding = component["finding"]
        if finding is not None:
            plans.append({"findings": _findings(finding, acts)})
            continue
        # A local act in two components of one run is ambiguous, never settled
        # by listing order.
        if any(act_component_count[row["act_id"]] > 1 for row in acts):
            plans.append({"findings": _findings("ambiguous-physical-act", acts)})
            continue
        ids = sorted(row["act_id"] for row in acts)
        if len(ids) != len(set(ids)) or len({row["source_sha256"] for row in acts}) != len(acts):
            plans.append({"findings": _findings("ambiguous-physical-act", acts)})
            continue
        registered_sources = set(members_of(register, page))
        if not registered_sources or any(
            row["source_sha256"] not in registered_sources for row in acts
        ):
            # Only inside the declared cluster: an appended correspondence carries
            # no source digest, so the register could not audit an outsider later.
            plans.append({"findings": _findings("capture-page-alignment-unresolved", acts)})
            continue
        # Members already in a physical act grow that act rather than mint a second;
        # reaching two acts is a merge held, not performed (§2.2); a retracted
        # correspondence is not re-declared.
        resolutions = {row["act_id"]: resolve_proposal(register, row["act_id"]) for row in acts}
        named: dict[str, str] = {}
        for row in acts:
            resolution = resolutions[row["act_id"]]
            if (
                resolution["outcome"] != "resolved"
                and resolution["code"] != "unresolved-physical-act"
            ):
                named[row["act_id"]] = resolution["code"]
            elif resolution["outcome"] == "resolved" and resolution["page_id"] != row["page_id"]:
                named[row["act_id"]] = "correspondence-page-mismatch"
        if named:
            # The component is withheld; every member left without a
            # correspondence is named (principle 2).
            plans.append(
                {
                    "findings": [
                        {
                            "code": named.get(row["act_id"], "unresolved-physical-act"),
                            "act_id": row["act_id"],
                        }
                        for row in acts
                        if row["act_id"] in named
                        or resolutions[row["act_id"]]["outcome"] != "resolved"
                    ]
                }
            )
            continue
        touched = {
            resolution["physical_act_id"]
            for resolution in resolutions.values()
            if resolution["outcome"] == "resolved"
        }
        if len(touched) > 1 or (touched and existing is not None and existing not in touched):
            plans.append({"findings": _findings("ambiguous-physical-act", acts)})
            continue
        if touched:
            # Proven single above, not chosen: the unpack raises on two targets.
            (target,) = touched
        else:
            # The caller's `physical_act_id` asserts, it never attaches: trusting it
            # would let a caller fuse any disjoint act into any existing act.
            if existing is not None:
                plans.append({"findings": _findings("ambiguous-physical-act", acts)})
                continue
            target = None
        if target is not None and physical_act_page(register, target) != page:
            # Undeclared, or minted on another physical page.
            plans.append({"findings": _findings("ambiguous-physical-act", acts)})
            continue
        if target is not None:
            target_count[target] += 1
        plans.append(
            {
                "acts": acts,
                "ids": ids,
                "page": page,
                "evidence": component["evidence"],
                "resolutions": resolutions,
                "target": target,
            }
        )

    accepted: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    for plan in plans:
        if "findings" in plan:
            findings.extend(plan["findings"])
            continue
        acts = plan["acts"]
        page = plan["page"]
        evidence = plan["evidence"]
        physical = plan["target"]
        if physical is not None and target_count[physical] > 1:
            findings.extend(_findings("ambiguous-physical-act", acts))
            continue
        if physical is None:
            designation = physical_act_component_designation(page, plan["ids"])
            physical = physical_act_id(page, designation)
            accepted.append(
                {
                    "kind": "physical-act",
                    "physical_page_id": page,
                    "mint_designation": designation,
                    "physical_act_id": physical,
                    "evidence": evidence,
                    "appending_run": discovery_run_id,
                }
            )
        accepted.extend(
            {
                "kind": "correspondence",
                "page_id": row["page_id"],
                "act_id": row["act_id"],
                "act_class": row["act_class"],
                "act_bounds": row["act_bounds"],
                "physical_page_id": page,
                "physical_act_id": physical,
                "evidence": evidence,
                "appending_run": discovery_run_id,
            }
            for row in sorted(acts, key=lambda item: item["act_id"])
            # Re-declaring an existing correspondence would refuse the whole append.
            if plan["resolutions"][row["act_id"]]["outcome"] != "resolved"
        )
    # Enumeration order must not reach the seal, or identical evidence would give
    # a different register_digest. Mints sort ahead of the correspondences naming them.
    accepted.sort(key=_accepted_record_sort_key)
    payload = {
        "schema": PROPOSAL_SCHEMA,
        "register_digest": register_digest,
        "discovery_run_id": discovery_run_id,
        "accepted_records": accepted,
        "findings": sorted(_dedupe_findings(findings), key=_finding_sort_key),
    }
    payload["self_hash"] = self_hash(payload)
    return validate_correspondence_proposal(payload)


def validate_correspondence_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the sealed discovery artifact before it can mutate the corpus."""
    _refuse_preference(payload)
    if not isinstance(payload, dict) or payload.get("schema") != PROPOSAL_SCHEMA:
        raise SchemaRefusal("correspondence proposal: invalid schema")
    _refuse_unverified_self_hash(payload, "correspondence proposal", "proposal")
    required = {
        "schema",
        "register_digest",
        "discovery_run_id",
        "accepted_records",
        "findings",
        "self_hash",
    }
    if set(payload) != required:
        raise SchemaRefusal("correspondence proposal: sealed record is not closed")
    _sha(payload["register_digest"], "correspondence proposal register_digest")
    run = payload["discovery_run_id"]
    records = payload["accepted_records"]
    findings = payload["findings"]
    if not isinstance(run, str) or not run:
        raise SchemaRefusal("correspondence proposal: discovery_run_id is invalid")
    if not isinstance(records, list) or not isinstance(findings, list):
        raise SchemaRefusal("correspondence proposal: records and findings must be lists")

    minted: set[str] = set()
    correspondence_acts: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("kind") not in {
            "physical-act",
            "correspondence",
        }:
            raise SchemaRefusal(
                "correspondence proposal: accepted record is not a mint or correspondence"
            )
        common = {
            "kind",
            "physical_page_id",
            "physical_act_id",
            "evidence",
            "appending_run",
        }
        expected = (
            common | {"mint_designation"}
            if record["kind"] == "physical-act"
            else common | {"page_id", "act_id", "act_class", "act_bounds"}
        )
        if set(record) != expected:
            raise SchemaRefusal("correspondence proposal: accepted record is not closed")
        if record["appending_run"] != run:
            raise SchemaRefusal(
                "correspondence proposal: accepted record does not name its discovery run"
            )
        evidence = record["evidence"]
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) and item for item in evidence)
            or evidence != sorted(set(evidence))
        ):
            raise SchemaRefusal(
                "correspondence proposal: evidence references must be sorted and unique"
            )
        page = record["physical_page_id"]
        physical = record["physical_act_id"]
        if not _is_derived_id(page, "ppg_"):
            raise SchemaRefusal("correspondence proposal: physical page identity is malformed")
        if not _is_derived_id(physical, "pac_"):
            raise SchemaRefusal("correspondence proposal: physical act identity is malformed")
        if record["kind"] == "physical-act":
            designation = record["mint_designation"]
            if (
                not isinstance(designation, str)
                or not designation
                or physical_act_id(page, designation) != physical
                or physical in minted
            ):
                raise SchemaRefusal("correspondence proposal: physical-act mint is malformed")
            minted.add(physical)
        else:
            if (
                not _is_derived_id(record["page_id"], "pg_")
                or not _is_derived_id(record["act_id"], "act_")
                or record["act_id"] in correspondence_acts
            ):
                raise SchemaRefusal(
                    "correspondence proposal: local act correspondence is malformed or repeated"
                )
            correspondence_acts.add(record["act_id"])
    if records != sorted(records, key=_accepted_record_sort_key):
        raise SchemaRefusal(
            "correspondence proposal: accepted records are not in canonical set order"
        )
    referenced = {
        record["physical_act_id"] for record in records if record["kind"] == "correspondence"
    }
    if minted - referenced:
        raise SchemaRefusal("correspondence proposal: a physical-act mint has no correspondence")

    finding_pairs = _finding_pairs(findings, "correspondence proposal")
    if correspondence_acts & {act for act, _code in finding_pairs}:
        raise SchemaRefusal(
            "correspondence proposal: one local act is both accepted and held by a finding"
        )
    return payload


def append_correspondence_proposal(
    *, register_path: str, proposal: dict[str, Any], discovery_register_digest: str
) -> str:
    """Atomically append a sealed discovery proposal, then force that run stale."""
    proposal = validate_correspondence_proposal(proposal)
    if proposal.get("register_digest") != discovery_register_digest:
        raise IncompatibleReuse(
            "correspondence proposal: discovery register digest is not its sealed predecessor"
        )
    records = proposal.get("accepted_records")
    if not isinstance(records, list) or not records:
        raise SchemaRefusal("correspondence proposal: contains no accepted append records")
    return append_records(register_path, records, expected_digest=discovery_register_digest)
