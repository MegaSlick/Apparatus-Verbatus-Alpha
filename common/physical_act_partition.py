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

from common.contracts.canonical import self_hash, self_hash_refusal, verify_self_hash
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

    Iterative for the same reason the preference screens are: the value is
    caller input, screened before any shape check closes it, so a deep payload
    walked recursively exhausted the interpreter stack and raised
    ``RecursionError`` -- a crash naming nothing, where this module's whole
    purpose is to refuse by name. Depth is this walk's own list now.

    A cycle is refused rather than looped on, for the same reason and with the
    same on-path bookkeeping every screen in the family now carries: the
    recursion this replaced ended a self-referential payload by exhausting
    itself, and a worklist has no stack to exhaust. The proposal components
    reaching `_refuse_textual` are the caller's own objects, not something this
    module parsed, so the shape is reachable. Only containers open on the
    current path are tracked, so a component shared between siblings is still
    walked wherever it appears.
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
        elif isinstance(current, list):
            pending.extend(("value", item) for item in current)


def _dedupe_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    """One finding per (code, act_id): a repeated cause is not a repeated fact."""
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for finding in findings:
        seen[(finding["code"], finding["act_id"])] = finding
    return list(seen.values())


def _finding_sort_key(row: dict[str, str]) -> tuple[str, str]:
    return row["act_id"], row["code"]


def _sha(value: Any, what: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise SchemaRefusal(f"physical-act partition: {what} must be a lowercase SHA-256")
    return value


def _is_derived_id(value: Any, prefix: str) -> bool:
    """A derived identity of one kind: the right shape *and* the right prefix.

    `is_well_formed` accepts every derived-identity prefix, so the prefix test
    is what pins which kind of thing a field names. Shape alone would let a page
    id stand where an act id belongs.
    """
    return is_well_formed(value) and value.startswith(prefix)


def _path(value: Any, what: str) -> str:
    # Same containment idiom as `common/contracts/envelope.py::validate_input_refs`
    # and `common/runtree/store.py::RunTree.resolve`: a reference is relative to
    # the run root, and a digest-bound reference this schema seals must be
    # refused here rather than trusted through to whichever reader dereferences it.
    # Typed before it is walked. The key-set check upstream proves the field is
    # present, not that it is a string, so a `relative_path` of None or a number
    # reached `startswith` and killed the stage with an AttributeError traceback
    # instead of the named refusal this module exists to produce.
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
        # The register re-derives every correspondence's act_id from its class
        # and minted bounds, so a discovery row that will be appended must
        # carry both; a run-partition row read from the Designator seal, whose
        # closed contract has no bindings, legitimately omits them.
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
        # Proving the identity here keeps a mismatched class or bounds a
        # partition intake refusal rather than a later append refusal.
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
    if (
        not isinstance(row["page_ordinal"], int)
        or isinstance(row["page_ordinal"], bool)
        or row["page_ordinal"] < 0
    ):
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
    # Several region references for one local act are a set.  Preserve them all,
    # but do not preserve the producer's traversal order as a durable byte.
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

    A capture may reach one physical page through more than one rendered page --
    a whole opening and the split half of it are two `page_id`s over identical
    source bytes -- so the consult's presentation row carries `page_ids[]`.
    Keeping the first row and dropping the rest would delete a page from the
    record, which is why the union is built once here rather than by a
    `setdefault` at each use.  Two rows that disagree about *how* one capture
    aligns to one physical page are a contradiction in the caller's table, not a
    choice to be made: they are refused.
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
    """The one presentation row for one capture of one physical page.

    Created from the alignment index rather than from whichever alignment row is
    to hand, so a capture reaching the page through several rendered pages keeps
    all of them and the row is the same whichever member reaches it first.
    """
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
    # The digest is sealed into the artifact as the provenance of this grouping, so
    # it has to be the digest of the bytes that produced it. A caller that reads the
    # digest before an append and the bytes after gets a partition grouped by one
    # register and attributed to another -- a cluster re-registered mid-lifecycle,
    # with nothing downstream able to see it. Reading the digest here also validates
    # the register, which the alignment-free path would otherwise never do.
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
    if not isinstance(source_ledger, set) or not all(
        isinstance(source, str)
        and len(source) == 64
        and all(character in "0123456789abcdef" for character in source)
        for source in source_ledger
    ):
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
    # A local act on a clustered capture absent from this run's alignment table is
    # missing alignment, not an image-local singleton. Publishing it as a singleton
    # would duplicate the logical act it belongs to.
    clustered_sources = {
        source
        for _page, (_digest, members) in membership_heads(register).items()
        for source in members
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scopes: dict[str, tuple[str, str | None, str | None]] = {}
    findings: list[dict[str, str]] = []
    for act in acts:
        if act["source_sha256"] not in source_ledger:
            findings.append({"code": "local-source-absent", "act_id": act["act_id"]})
            continue
        alignment = by_page.get(act["page_id"])
        if alignment is None:
            if act["source_sha256"] in clustered_sources:
                findings.append(
                    {"code": "capture-page-alignment-unresolved", "act_id": act["act_id"]}
                )
                continue
            logical = act["act_id"]
            scopes[logical] = ("image-local-singleton", None, None)
        else:
            if alignment["source_sha256"] != act["source_sha256"]:
                raise SchemaRefusal(
                    "physical-act partition: local act source_sha256 does not match its "
                    "page's capture alignment"
                )
            page = alignment["physical_page_id"]
            members = members_of(register, page)
            if not members or alignment["source_sha256"] not in members:
                # Either the physical page currently has no declared member (every
                # link retracted, or none yet), or this alignment names a capture the
                # register does not declare for that physical page -- a correspondence
                # naming a capture outside the cluster. Both are the same finding: the
                # alignment cannot be trusted to resolve this act.
                findings.append(
                    {"code": "capture-page-alignment-unresolved", "act_id": act["act_id"]}
                )
                continue
            if any(member not in source_ledger for member in members):
                findings.append({"code": "cluster-member-absent", "act_id": act["act_id"]})
                continue
            resolved = resolve_proposal(register, act["act_id"])
            if resolved["outcome"] != "resolved":
                findings.append({"code": resolved["code"], "act_id": act["act_id"]})
                continue
            if resolved["page_id"] != act["page_id"]:
                findings.append({"code": "correspondence-page-mismatch", "act_id": act["act_id"]})
                continue
            if resolved["physical_page_id"] != page:
                # The register minted this physical act on one physical page and the
                # caller's table aligns this capture to another. Taking either side
                # would make one logical act's page depend on which member happened
                # to sort first, so neither is taken.
                findings.append(
                    {"code": "capture-page-alignment-unresolved", "act_id": act["act_id"]}
                )
                continue
            logical = resolved["physical_act_id"]
            scopes[logical] = ("physical-act", logical, page)
        groups[logical].append(act)
    logical_acts: list[dict[str, Any]] = []
    for logical in sorted(groups):
        members = sorted(groups[logical], key=lambda row: row["act_id"])
        scope, physical, physical_page = scopes[logical]
        # A same-capture double member is an ambiguous component, never an arbitrary collapse.
        sources = [row["source_sha256"] for row in members]
        if len(sources) != len(set(sources)):
            findings.extend(
                {"code": "ambiguous-physical-act", "act_id": row["act_id"]} for row in members
            )
            continue
        components: dict[str, set[str]] = defaultdict(set)
        presentations: dict[tuple[str, str], dict[str, Any]] = {}
        if physical is not None:
            # `physical_page` is the page the register minted this physical act on,
            # already checked against every member's alignment above -- never read
            # off whichever member sorted first.
            required = members_of(register, physical_page)
            if any((physical_page, source) not in index for source in required):
                findings.extend(
                    {"code": "capture-page-alignment-unresolved", "act_id": row["act_id"]}
                    for row in members
                )
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
                # Bindings (act_class/act_bounds) are intake facts for identity
                # re-derivation and register appends; the sealed lineage row is
                # the closed six-field shape every downstream consumer closes on.
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
    # The builder is held to the same conservation arithmetic its consumers are.
    # A denominator that is wrong here is wrong everywhere downstream, and a
    # producer that only its readers check is one refactor away from publishing a
    # partition nobody re-reads until a stage has already used it.
    return validate_physical_act_partition(payload)


def validate_physical_act_partition(payload: dict[str, Any]) -> dict[str, Any]:
    _refuse_preference(payload)
    if not isinstance(payload, dict) or payload.get("schema") != PARTITION_SCHEMA:
        raise SchemaRefusal("physical-act partition: invalid schema")
    if not verify_self_hash(payload):
        unhashable = self_hash_refusal(payload)
        if unhashable is not None:
            raise SchemaRefusal(
                f"physical-act partition: self hash cannot be verified: {unhashable}"
            )
        raise SchemaRefusal("physical-act partition: self hash does not match the sealed partition")
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
        not isinstance(payload[name], int) or isinstance(payload[name], bool) or payload[name] < 0
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

        presentations = group["capture_presentations"]
        if not isinstance(presentations, list):
            raise SchemaRefusal("physical-act partition: capture presentations are invalid")
        presentation_pairs: set[tuple[str, str]] = set()
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
            pair = (page, source)
            presentation_pairs.add(pair)
            presentation_keys.append(pair)
            presented_local_ids.extend(local_ids)
        if presentation_keys != sorted(set(presentation_keys)):
            raise SchemaRefusal(
                "physical-act partition: capture presentations are not in canonical set order"
            )

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
    finding_pairs: list[tuple[str, str]] = []
    for row in payload["findings"]:
        if (
            not isinstance(row, dict)
            or set(row) != {"code", "act_id"}
            or not isinstance(row["code"], str)
            or not row["code"]
            or not _is_derived_id(row["act_id"], "act_")
        ):
            raise SchemaRefusal("physical-act partition: finding is not closed")
        finding_pairs.append((row["act_id"], row["code"]))
    if finding_pairs != sorted(set(finding_pairs)):
        raise SchemaRefusal("physical-act partition: findings are not sorted unique facts")
    held = {act for act, _code in finding_pairs}
    # An act is carried into a logical act or it is held by a named finding. Both at
    # once means the record answers two ways at once; a sum that merely reaches the
    # expected count lets one act cover another's disappearance, which is precisely
    # the arithmetic a partial run would produce.
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

    ``components`` is deliberately geometry-only: each component names its
    physical page, its local acts, and digest-bound registration evidence.  A
    component with any finding is retained as a finding and contributes no
    records; the register writer therefore cannot turn ambiguity into a mint.

    The register is read, not merely named. A resolver that mints from geometry
    alone cannot see that one of its local acts already belongs to a physical
    act, so two honest discovery runs over an overlapping component split one
    physical act into two -- and the split is quiet, because each surviving act
    resolves perfectly well on its own. Whether the two runs merge or split then
    depends only on which ran first, which is the order-dependence the consult's
    §2.2 transitivity requirement rules out.
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
                # Evidence names a set of geometric facts.  Its enumerator's
                # traversal order is neither evidence nor a durable corpus fact.
                "evidence": sorted(set(evidence)),
                "finding": finding,
            }
        )

    def ambiguous(acts: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [{"code": "ambiguous-physical-act", "act_id": row["act_id"]} for row in acts]

    # Resolution must finish before emission: emitting as it goes would let the
    # component listed first take a physical act and leave the second held.
    plans: list[dict[str, Any]] = []
    target_count: dict[str, int] = defaultdict(int)
    for component in parsed:
        page = component["page"]
        existing = component["existing"]
        acts = component["acts"]
        finding = component["finding"]
        if finding is not None:
            plans.append({"findings": [{"code": finding, "act_id": row["act_id"]} for row in acts]})
            continue
        # A local act named by more than one component in this same discovery
        # run is exactly "a capture in two proposed correspondences": the
        # resolver did not produce an exact-one admissible component, so the
        # whole component is ambiguous and none of it is appended -- never
        # resolved by which component happened to be listed first.
        if any(act_component_count[row["act_id"]] > 1 for row in acts):
            plans.append({"findings": ambiguous(acts)})
            continue
        ids = sorted(row["act_id"] for row in acts)
        if len(ids) != len(set(ids)) or len({row["source_sha256"] for row in acts}) != len(acts):
            plans.append({"findings": ambiguous(acts)})
            continue
        registered_sources = set(members_of(register, page))
        if not registered_sources or any(
            row["source_sha256"] not in registered_sources for row in acts
        ):
            # Geometry may relate acts only inside the capture cluster the
            # immutable register declares. Letting an outsider through here
            # would append a durable correspondence the register cannot later
            # audit, because that record carries the rendered page and act but
            # not a second copy of the source digest.
            plans.append(
                {
                    "findings": [
                        {
                            "code": "capture-page-alignment-unresolved",
                            "act_id": row["act_id"],
                        }
                        for row in acts
                    ]
                }
            )
            continue
        # What the register already says about these local acts. A component whose
        # members already belong to a physical act is *that* act growing, never a
        # second mint over an overlapping set; one that reaches two physical acts
        # is the transitive merge §2.2 holds rather than performs; and one whose
        # correspondence a person retracted is not re-declared behind them.
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
            # The whole component is withheld. Each member is named for what the
            # register says of it, and a member this run leaves without any
            # correspondence is named too -- an unnamed member is a lost one
            # (GOVERNANCE 2). A member that already resolves keeps the
            # correspondence it has and needs no finding.
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
            plans.append({"findings": ambiguous(acts)})
            continue
        if touched:
            # Exactly one, proven above rather than chosen: the unpack raises if
            # this component ever reaches two physical acts.
            (target,) = touched
        else:
            # The caller's `physical_act_id` is a consistency assertion, not a
            # route for attaching an otherwise-unresolved component.  A real
            # touch is established only by replaying one of this component's
            # local acts through the register.  Trusting a same-page id here
            # would let a caller fuse any disjoint act into any existing act.
            if existing is not None:
                plans.append({"findings": ambiguous(acts)})
                continue
            target = None
        if target is not None and physical_act_page(register, target) != page:
            # The named physical act is undeclared, or was minted on another
            # physical page. Appending against it would attach this component to
            # a page the register never put it on.
            plans.append({"findings": ambiguous(acts)})
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
            # Two components of one run reaching one physical act is that run
            # merging them, and neither is preferred for being listed first.
            findings.extend(ambiguous(acts))
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
            # A member the register already corresponds to this act needs no second
            # declaration; re-declaring it would refuse the whole append.
            if plan["resolutions"][row["act_id"]]["outcome"] != "resolved"
        )
    # Enumeration order must not reach the seal. Two discovery runs that found the
    # same components in a different order would otherwise seal different bytes and
    # append the same facts in a different sequence, giving the corpus a different
    # register_digest for identical evidence (consult §2.2, closing paragraph).
    # Mints sort ahead of the correspondences that name them because the register
    # reads a record only after something declares it; within each, the sort is a
    # serialization of the whole set, never a choice among it.
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
    if not verify_self_hash(payload):
        unhashable = self_hash_refusal(payload)
        if unhashable is not None:
            raise SchemaRefusal(
                f"correspondence proposal: self hash cannot be verified: {unhashable}"
            )
        raise SchemaRefusal("correspondence proposal: self hash does not match the sealed proposal")
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

    finding_pairs: list[tuple[str, str]] = []
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"code", "act_id"}
            or not isinstance(finding["code"], str)
            or not finding["code"]
            or not _is_derived_id(finding["act_id"], "act_")
        ):
            raise SchemaRefusal("correspondence proposal: finding is not closed")
        finding_pairs.append((finding["act_id"], finding["code"]))
    if finding_pairs != sorted(set(finding_pairs)):
        raise SchemaRefusal("correspondence proposal: findings are not sorted unique facts")
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
