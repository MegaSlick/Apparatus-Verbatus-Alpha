"""Exemplar: the sealed source. Nothing downstream may alter it.

Reads what the door admitted and seals each admitted source as a page: the bytes
into the run tree's blob store, and a `page` artifact binding the page identity to
the immutable origin and transform. From here on, every region in the run traces back
to one of these — ARCHITECTURE's second invariant — because a region's identity is
derived from an act's, and an act's from a page's.

The Exemplar reads the door's artifacts rather than the fixture's file list. That is
the handoff being real: if the door refused a page, this stage sees a refusal and
seals nothing for it, instead of quietly going back to the source and sealing it
anyway.

**The handoff is checked, not merely read.** Before anything is published, the
door's census is reconciled against `run.json`'s submitted source manifest — every submitted ordinal has exactly one door outcome and no door outcome
names an ordinal nobody submitted — and every admitted blob is verified against the
digest its admission claims. A source cannot disappear between submission and
sealing, and a page cannot be sealed over bytes that are no longer the bytes the
door inspected.

**The corpus seal** is one `kind="seal"` artifact per run, written once every
page has been accounted for. It is self-hashed the same way `run.json` is —
`self_hash`/`verify_self_hash` from `common/contracts/canonical.py` — so an edit
after sealing is detectable rather than merely undocumented, and a rerun over a
tampered seal refuses before it writes. It is an artifact like any other,
published through the same `context.publish` every page uses, and both
downstream readers filter the Exemplar's manifest to `kind == "page"`, so a third
kind sitting beside them disturbs nothing.

    python pipeline/1_exemplar/run.py --run-root <dir> --run-id <id>
"""

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from admission import reason_code  # noqa: E402

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.contracts.approval import REAL_INGRESS, parse_ingress_record  # noqa: E402
from common.contracts.canonical import is_sha256, self_hash, verify_self_hash  # noqa: E402
from common.contracts.envelope import read_verified, validate_envelope  # noqa: E402
from common.contracts.errors import ContractError, SchemaRefusal  # noqa: E402
from common.contracts.identities import artifact_id, page_id  # noqa: E402
from common.contracts.stages import DOOR, EXEMPLAR  # noqa: E402
from common.exemplar_boundary import (  # noqa: E402
    is_triage_derivative_contract,
    verify_triage_derivative,
)
from common.imaging import (  # noqa: E402
    DRAW_ANNOTATIONS,
    DRAW_FORMS,
    MIN_RENDER_DPI,
    POINTS_PER_INCH,
    RENDER_BACKGROUND,
    RENDER_CODEC,
    RENDER_COLOR_MODE,
    raster_mode_transform,
)
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    adapter_recipe_for,
    open_stage_context,
    run_stage,
    stage_parser,
)

DESCRIPTION = "Exemplar: the sealed source. Nothing downstream may alter it."

SEAL_SUBJECT = "corpus-seal"


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(DESCRIPTION).parse_args()
    # One constructor for both ingress routes, so both ask for the Door's
    # completion seal in the same order before anything writes.
    context = open_stage_context(args, EXEMPLAR, registry_factory=registry_factory)
    tree = context.tree
    _verify_existing_corpus_seal(tree)

    sources = _submitted_sources(context.run)
    admissions = _checked_admissions(tree, context.run, sources)

    sealed_digests = context.run.get("sealed_config_digests")
    canary_ledger = (
        sealed_digests.get("canary-ledger") if isinstance(sealed_digests, dict) else None
    )
    # Identity binds the admitted bytes' immutable origin, never the manifest
    # ordinal or path, so inserting a row cannot rename a page. Every identity is
    # derived, and every refusal below decided, before the first page is published,
    # so a refused Exemplar seals nothing.
    identities: dict[int, str] = {}
    ordinal_by_page: dict[str, int] = {}
    for ordinal, admission, _admission_ref, _blob_ref in admissions:
        if admission["outcome"] == "refused":
            continue
        identity = page_id(_page_origin(admission["payload"]), {"operation": "whole"})
        if identity in ordinal_by_page:
            # The Door refuses byte-identical submissions before its seal, so only a
            # tree no Door closed can reach this; every later stage works one page
            # per submitted row.
            raise ContractError(
                f"submitted ordinals {ordinal_by_page[identity]} and {ordinal} derive one "
                "page identity; one page per submitted row is the contract, so nothing was "
                "sealed"
            )
        ordinal_by_page[identity] = ordinal
        identities[ordinal] = identity
    if not identities:
        raise ContractError("every admitted source failed to seal")
    if all(
        canary_ledger is not None and sources[ordinal].get("ledger_sha256") == canary_ledger
        for ordinal in identities
    ):
        # Canary pages are controls sealed beside the submission, never a
        # substitute for it.
        raise ContractError(
            "only canary pages were admitted; no page of the real submission can be sealed"
        )

    page_refs: list[dict[str, str]] = []
    census: list[dict[str, Any]] = []
    for ordinal, admission, admission_ref, blob_ref in admissions:
        if admission["outcome"] == "refused":
            # The refusal is carried forward as this stage's own outcome so every
            # submitted ordinal has a page outcome here too.
            result = context.publish(
                kind="page",
                subject_id=admission["subject_id"],
                outcome="refused",
                inputs=[admission_ref],
                payload=_refused_page_payload(admission["payload"], ordinal, sources[ordinal]),
            )
            page_refs.append(context.input_ref(result.relative_path))
            census.append(
                _census_row(
                    sources[ordinal],
                    ordinal=ordinal,
                    page_identity=None,
                    outcome="refused",
                    source_sha256=None,
                )
            )
            continue

        payload = admission["payload"]
        identity = identities[ordinal]
        result = context.publish(
            kind="page",
            subject_id=identity,
            outcome="sealed",
            inputs=[admission_ref, blob_ref],
            payload=_page_payload(payload, ordinal, sources[ordinal]),
        )
        page_refs.append(context.input_ref(result.relative_path))
        census.append(
            _census_row(
                sources[ordinal],
                ordinal=ordinal,
                page_identity=identity,
                outcome="sealed",
                source_sha256=payload["sha256"],
            )
        )

    seal_payload: dict[str, Any] = {
        "page_count": len(census),
        "pages": sorted(census, key=lambda item: item["ordinal"]),
    }
    seal_payload["self_hash"] = self_hash(seal_payload)
    context.publish(
        kind="seal",
        subject_id=SEAL_SUBJECT,
        outcome="sealed",
        inputs=page_refs,
        payload=seal_payload,
    )

    context.seal_boundary()
    context.finish()
    return EXIT_COMPLETE


def _page_payload(payload: dict[str, Any], ordinal: int, source: dict[str, Any]) -> dict[str, Any]:
    """What a sealed page records, including a complete container render contract."""
    sealed: dict[str, Any] = {
        "ordinal": ordinal,
        "declared_path": source["relative_path"],
        "declared_sha256": source["sha256"],
        "source_sha256": payload["sha256"],
        "image_path": payload["stored_at"],
    }
    if "bytes" in source:
        sealed["declared_bytes"] = source["bytes"]
    if "ledger_sha256" in source:
        sealed["ledger_sha256"] = source["ledger_sha256"]
    if source.get("container_page_index") is not None:
        sealed["container_page_index"] = source["container_page_index"]
    if "rendered_from" in payload:
        sealed["rendered_from"] = payload["rendered_from"]
        resolution = _render_resolution_record(payload["rendered_from"])
        if resolution is not None:
            # A page whose effective DPI is below the run target must not hide
            # that reduction inside a nested renderer recipe: the sealed Exemplar
            # page says so plainly.
            sealed["render_resolution"] = resolution
    return sealed


def _page_origin(payload: dict[str, Any]) -> dict[str, Any]:
    """Rendered bytes are derivatives; their sealed container is the origin."""
    rendered = payload.get("rendered_from")
    if rendered is None:
        return {"kind": "source", "sha256": payload["sha256"]}
    return {
        "kind": "container-page",
        "container_sha256": rendered["container_sha256"],
        "container_page_index": rendered["container_page_index"],
        "render_contract": rendered["render_contract"],
    }


def _render_resolution_record(rendered_from: Any) -> dict[str, Any] | None:
    """Project a PDF render's target/effective DPI into its sealed page record."""
    if not isinstance(rendered_from, dict) or rendered_from.get("container_format") != "pdf":
        return None
    contract = rendered_from.get("render_contract")
    if not isinstance(contract, dict):
        return None
    target = contract.get("dpi")
    effective = contract.get("effective_dpi")
    configured = contract.get("configured_target_dpi")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in (configured, target, effective)
    ):
        # The existing render-contract verifier names malformed evidence.  This
        # helper deliberately adds no alternate acceptance path for it.
        return None
    below_target = effective < target
    return {
        "configured_target_dpi": configured,
        "resolved_target_dpi": target,
        "effective_dpi": effective,
        "below_resolved_target": below_target,
        "shortfall_dpi": target - effective if below_target else 0,
    }


def _refused_page_payload(
    admission_payload: dict[str, Any], ordinal: int, source: dict[str, Any]
) -> dict[str, Any]:
    """Carry the submitted filename-ledger facts even when no page sealed."""
    refused: dict[str, Any] = {
        "ordinal": ordinal,
        "declared_path": source["relative_path"],
        "declared_sha256": source["sha256"],
        "reason": admission_payload["reason"],
    }
    if "bytes" in source:
        refused["declared_bytes"] = source["bytes"]
    if "ledger_sha256" in source:
        refused["ledger_sha256"] = source["ledger_sha256"]
    if source.get("container_page_index") is not None:
        refused["container_page_index"] = source["container_page_index"]
    return refused


def _census_row(
    source: dict[str, Any],
    *,
    ordinal: int,
    page_identity: str | None,
    outcome: str,
    source_sha256: str | None,
) -> dict[str, Any]:
    """One corpus-seal row, retaining the original filename ledger facts."""
    row: dict[str, Any] = {
        "ordinal": ordinal,
        "declared_path": source["relative_path"],
        "declared_sha256": source["sha256"],
        "page_id": page_identity,
        "outcome": outcome,
        "source_sha256": source_sha256,
    }
    if "bytes" in source:
        row["declared_bytes"] = source["bytes"]
    if "ledger_sha256" in source:
        row["ledger_sha256"] = source["ledger_sha256"]
    if source.get("container_page_index") is not None:
        row["container_page_index"] = source["container_page_index"]
    return row


def _submitted_sources(run: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """The run authority's submitted manifest, by ordinal, validated as it is read."""
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise ContractError("run.json has no submitted source manifest to seal")
    sources: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("run.json holds a source manifest row that is not an object")
        ordinal, path, digest = row.get("ordinal"), row.get("relative_path"), row.get("sha256")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError("run.json holds a source manifest row with no integer ordinal")
        if ordinal in sources:
            raise ContractError(f"run.json names submitted ordinal {ordinal} more than once")
        if not isinstance(path, str) or not path:
            raise ContractError(f"run.json source ordinal {ordinal} declares no path")
        if not is_sha256(digest):
            raise ContractError(f"run.json source ordinal {ordinal} has no lowercase sha256")
        sources[ordinal] = dict(row)
    _verify_source_ledger(run, sources)
    return sources


def _verify_source_ledger(run: dict[str, Any], sources: dict[int, dict[str, Any]]) -> None:
    """Rebuild the real-input filename ledger from the sealed source manifest.

    A multi-page source occupies several page ordinals, so `run.json` repeats its
    source facts once per page.  Collapsing those repetitions back to unique file
    rows must reproduce the local submit manifest's self-hash exactly.  This is the
    between-boundary check: the door cannot start from a smaller or differently
    named set while still claiming the same original filename ledger.
    """
    mode = parse_ingress_record(run.get("ingress"))
    carries_ledger = any("ledger_sha256" in row for row in sources.values())
    if mode != REAL_INGRESS:
        if carries_ledger:
            raise ContractError("a synthetic-fixture run carries a real submission filename ledger")
        return
    if not carries_ledger:
        raise ContractError("a real run has no filename ledger bound into its source manifest")

    sealed = run.get("sealed_config_digests", {})
    canary_hash = sealed.get("canary-ledger") if isinstance(sealed, dict) else None
    ledger_hashes: set[str] = set()
    files_by_ledger: dict[str, dict[str, dict[str, Any]]] = {}
    for ordinal, source in sources.items():
        ledger_hash = source.get("ledger_sha256")
        size = source.get("bytes")
        if not is_sha256(ledger_hash):
            raise ContractError(f"run.json source ordinal {ordinal} has no filename-ledger sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ContractError(
                f"run.json source ordinal {ordinal} has no non-negative filename-ledger byte count"
            )
        ledger_hashes.add(ledger_hash)
        source_file = {
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
            "bytes": size,
        }
        files_by_path = files_by_ledger.setdefault(ledger_hash, {})
        existing = files_by_path.setdefault(source_file["relative_path"], source_file)
        if existing != source_file:
            raise ContractError(
                "run.json repeats one filename with incompatible digest or byte-count entries; "
                "its filename ledger cannot be reconstructed"
            )
    if canary_hash is None and len(ledger_hashes) != 1:
        raise ContractError("run.json source rows name more than one filename ledger")
    if canary_hash is not None and (len(ledger_hashes) != 2 or canary_hash not in ledger_hashes):
        raise ContractError("run.json source rows do not name the expected real and canary ledgers")
    for ledger_hash, files_by_path in files_by_ledger.items():
        ledger = {
            "schema": "submission-manifest.v1",
            "files": sorted(files_by_path.values(), key=lambda item: item["relative_path"]),
        }
        if self_hash(ledger) != ledger_hash:
            if canary_hash is None:
                raise ContractError(
                    "run.json source rows do not reproduce the self-hashed filename ledger that "
                    "admitted this real submission"
                )
            raise ContractError("run.json source rows do not reproduce a sealed filename ledger")
    if canary_hash is not None:
        real_hash = next(hash_value for hash_value in ledger_hashes if hash_value != canary_hash)
        canaries = files_by_ledger[canary_hash].values()
        reals = files_by_ledger[real_hash].values()
        real_paths = {row["relative_path"] for row in reals}
        real_digests = {row["sha256"] for row in reals}
        if any(
            row["relative_path"] in real_paths or row["sha256"] in real_digests for row in canaries
        ):
            raise ContractError(
                "sealed canary ledger overlaps the real submission by path or digest"
            )


def _checked_admissions(
    tree: RunTree, run: dict[str, Any], sources: dict[int, dict[str, Any]]
) -> list[tuple[int, dict[str, Any], dict[str, str], dict[str, str] | None]]:
    """Every door admission, reconciled against the run authority and byte-checked."""
    entries = [
        entry for entry in tree.build_manifest(DOOR)["artifacts"] if entry["kind"] == "admission"
    ]
    if not entries:
        raise ContractError(
            "no admissions to seal: the Exemplar was run before the door, or the door's "
            "artifacts are missing. Sealing nothing quietly would leave a run that looks "
            "finished and read no page at all"
        )

    checked: list[tuple[int, dict[str, Any], dict[str, str], dict[str, str] | None]] = []
    observed: set[int] = set()
    for entry in entries:
        admission, admission_ref = _read_checked_admission(tree, entry)
        _verify_admission_context(tree, run, entry, admission)
        payload = admission["payload"]
        ordinal = payload.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise ContractError(
                f"admission {admission['subject_id']} carries no integer ordinal; "
                "an unaccountable admission is a silent loss wearing a record"
            )
        if ordinal in observed:
            raise ContractError(f"the door published ordinal {ordinal} more than once")
        if ordinal not in sources:
            raise ContractError(f"the door published ordinal {ordinal}, which nobody submitted")
        observed.add(ordinal)
        source = sources[ordinal]
        if admission["subject_id"] != f"source-{ordinal}":
            raise ContractError(f"door admission for ordinal {ordinal} has the wrong subject")
        if payload.get("declared_path") != source["relative_path"]:
            raise ContractError(
                f"door admission for ordinal {ordinal} disagrees with run.json's declared path"
            )
        if payload.get("declared_sha256") != source["sha256"]:
            raise ContractError(
                f"door admission for ordinal {ordinal} disagrees with run.json's declared digest"
            )
        if "bytes" in source and payload.get("declared_bytes") != source["bytes"]:
            raise ContractError(
                f"door admission for ordinal {ordinal} disagrees with run.json's declared byte count"
            )
        if "ledger_sha256" in source and payload.get("ledger_sha256") != source["ledger_sha256"]:
            raise ContractError(
                f"door admission for ordinal {ordinal} disagrees with run.json's filename ledger"
            )
        if admission["artifact_id"] != artifact_id(DOOR, "admission", f"source-{ordinal}"):
            raise ContractError(f"door admission for ordinal {ordinal} has a derived-id mismatch")

        if admission["outcome"] == "admitted":
            blob_ref = _verify_admitted_blob(tree, run, admission, source)
        elif admission["outcome"] == "refused":
            _verify_refusal(admission)
            blob_ref = None
        else:
            # The closed outcome algebra should already have refused this at the
            # envelope; keep this reader total rather than falling through.
            raise ContractError(f"door admission for ordinal {ordinal} has an unknown outcome")
        checked.append((ordinal, admission, admission_ref, blob_ref))

    missing = sorted(set(sources) - observed)
    if missing:
        raise ContractError(
            f"the door published no admission for submitted source ordinal(s) {missing}; a source "
            "may not disappear between submission and sealing"
        )
    return sorted(checked, key=lambda item: item[0])


def _read_checked_admission(
    tree: RunTree, entry: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read one manifest entry once, and keep the verified reference it produced."""
    relative_path, digest = entry.get("relative_path"), entry.get("sha256")
    if not isinstance(relative_path, str) or not is_sha256(digest):
        raise ContractError("the door's manifest holds an invalid admission reference")
    ref = {"relative_path": relative_path, "sha256": digest}
    data = read_verified(tree.read_bytes, ref, "a door admission", ContractError)
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ContractError("a door admission's bytes are not a JSON artifact") from error
    return validate_envelope(decoded), {"relative_path": relative_path, "sha256": digest}


def _verify_admission_context(
    tree: RunTree, run: dict[str, Any], entry: dict[str, Any], admission: dict[str, Any]
) -> None:
    """Refuse a well-formed door artifact that belongs to a different run."""
    if admission["run_id"] != tree.run_id:
        raise ContractError("a door admission belongs to a different run")
    if admission["stage"] != DOOR:
        raise ContractError("a door admission does not name the door as its producer")
    if admission["config_digest"] != run["config_digest"]:
        raise ContractError("a door admission is bound to a different run configuration")
    if admission["producer"]["adapter_revision"] != adapter_recipe_for(run, DOOR):
        raise ContractError("a door admission names a different door adapter recipe")
    if admission["artifact_id"] != entry.get("artifact_id"):
        raise ContractError("the door's manifest and its admission disagree about identity")


def _verify_admitted_blob(
    tree: RunTree, run: dict[str, Any], admission: dict[str, Any], source: dict[str, Any]
) -> dict[str, str]:
    """Prove the sealed bytes are the bytes the door said it admitted.

    The sealed digest equals the submitted digest for a standalone raster. It may
    differ only when a complete recorded container render explains it. That records
    the exact source page and renderer settings instead of making a changed digest
    look like unaccounted corruption.
    """
    payload = admission["payload"]
    stored_at, sealed_digest = payload.get("stored_at"), payload.get("sha256")
    if not is_sha256(sealed_digest):
        raise ContractError("an admitted source records no lowercase sha256 for its bytes")
    if stored_at != tree.blob_path(DOOR, sealed_digest):
        raise ContractError("an admission's stored_at is not the content-addressed blob path")
    claims_transform = "rendered_from" in payload
    is_derivative = False
    if source.get("container_page_index") is not None and not claims_transform:
        raise ContractError(
            "a fanned source page must carry the render transform that produced its sealed pixels"
        )
    if sealed_digest != source["sha256"] and not claims_transform:
        raise ContractError(
            "an admitted source's sealed bytes differ from the bytes that were "
            "submitted, and no transform is recorded to explain it"
        )
    if claims_transform:
        rendered_from = payload["rendered_from"]
        if not isinstance(rendered_from, dict):
            raise ContractError("an admitted source's render transform is not an object")
        expected = {
            "container_format",
            "container_sha256",
            "container_page_index",
            "render_contract",
        }
        if set(rendered_from) != expected:
            raise ContractError(
                "an admitted source's render transform does not carry exactly its container "
                "format, digest, page index, and render contract"
            )
        if rendered_from["container_sha256"] != source["sha256"]:
            raise ContractError(
                "a rendered page names a container digest the run authority did not submit"
            )
        if (
            not isinstance(rendered_from["container_format"], str)
            or not rendered_from["container_format"]
        ):
            raise ContractError("a rendered page records no container format")
        container_format = rendered_from["container_format"]
        index = rendered_from["container_page_index"]
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ContractError("a rendered page records no non-negative page index")
        if source.get("container_page_index") != index:
            raise ContractError(
                "a rendered page's transform page index disagrees with run.json's submitted row"
            )
        is_derivative = is_triage_derivative_contract(rendered_from["render_contract"])
        if is_derivative:
            parent_ref, parent, parent_bytes = _verify_derivative_admission(
                payload, source, rendered_from["render_contract"], tree
            )
        else:
            _verify_render_contract(
                rendered_from["render_contract"],
                index,
                payload,
                run,
                container_format=container_format,
            )
    expected_inputs = len({stored_at, parent_ref["relative_path"]}) if is_derivative else 1
    if len(admission["inputs"]) != expected_inputs:
        raise ContractError(
            "a sealed derivative page must carry its pixels and untouched master"
            if is_derivative
            else "an admitted source must carry exactly one admitted-blob input"
        )
    input_ref = next(
        (
            reference
            for reference in admission["inputs"]
            if reference.get("relative_path") == stored_at
            and reference.get("sha256") == sealed_digest
        ),
        None,
    )
    if input_ref is None:
        raise ContractError("an admission's input does not name its content-addressed blob")
    if is_derivative and {
        (reference.get("relative_path"), reference.get("sha256"))
        for reference in admission["inputs"]
    } != {
        (input_ref["relative_path"], input_ref["sha256"]),
        (parent_ref["relative_path"], parent_ref["sha256"]),
    }:
        raise ContractError(
            "a derivative page does not input exactly its pixels and untouched master"
        )
    # Read for its digest check: the blob on disk must be the bytes the Door admitted.
    read_verified(tree.read_bytes, input_ref, "an admitted blob", ContractError)
    if is_derivative:
        verify_triage_derivative(
            rendered_from["render_contract"],
            parent_bytes,
            parent_ref["sha256"],
            parent,
            sealed_digest,
        )
    return {"relative_path": stored_at, "sha256": sealed_digest}


def _verify_derivative_admission(
    payload: dict[str, Any], source: dict[str, Any], contract: dict[str, Any], tree: RunTree
) -> tuple[dict[str, str], dict[str, Any], bytes]:
    """Sealing must re-read digest-checked master bytes, not trust the Door's earlier read."""
    parent = payload.get("parent_frame")
    derivative = contract.get("derivative_page")
    if (
        not isinstance(parent, dict)
        or set(parent) != {"sha256", "stored_at", "source_frame_index"}
        or parent.get("sha256") != source.get("sha256")
        or parent.get("stored_at") != tree.blob_path(DOOR, parent.get("sha256"))
        or not isinstance(parent.get("source_frame_index"), int)
        or isinstance(parent.get("source_frame_index"), bool)
        or parent["source_frame_index"] < 0
        or not isinstance(derivative, dict)
        or derivative.get("parent_frame_sha256") != parent["sha256"]
        or derivative.get("parent_frame_page_index") != parent["source_frame_index"]
    ):
        raise ContractError("a derivative page does not carry a valid immutable parent frame")
    parent_ref = {"relative_path": parent["stored_at"], "sha256": parent["sha256"]}
    try:
        parent_bytes = read_verified(tree.read_bytes, parent_ref, "a derivative page's master")
    except SchemaRefusal as error:
        remedy = "restore the content-addressed master from the submitted bytes before retrying"
        raise SchemaRefusal(f"{error}; {remedy}") from error
    return parent_ref, parent, parent_bytes


def _verify_render_contract(
    contract: Any,
    page_index: int,
    payload: dict[str, Any],
    run: dict[str, Any],
    *,
    container_format: str,
) -> None:
    """Refuse a partial pixel-affecting render explanation before sealing it."""
    if not isinstance(contract, dict):
        raise ContractError("a rendered page carries no render contract object")
    required = {
        "renderer",
        "renderer_version",
        "container_page_index",
        "output",
        "width",
        "height",
    }
    if not required.issubset(contract):
        raise ContractError("a rendered page's render contract omits required pixel facts")
    if contract["container_page_index"] != page_index:
        raise ContractError("a rendered page's render contract names a different page index")
    if not isinstance(contract["renderer"], str) or not contract["renderer"]:
        raise ContractError("a rendered page's render contract names no renderer")
    if not isinstance(contract["renderer_version"], str) or not contract["renderer_version"]:
        raise ContractError("a rendered page's render contract names no renderer version")
    output = contract["output"]
    if (
        not isinstance(output, dict)
        or set(output) != {"codec", "color_mode"}
        or output["codec"] not in {"png", "tiff"}
        or not isinstance(output["color_mode"], str)
    ):
        raise ContractError(
            "a rendered page's render contract does not name lossless PNG or TIFF output"
        )
    if contract["renderer"] == "pypdfium2":
        if container_format != "pdf":
            raise ContractError("only a PDF container may claim the PDFium pixel renderer")
        required_pdf = required | {
            "pdfium_version",
            "configured_target_dpi",
            "dpi",
            "min_dpi",
            "effective_dpi",
            "scale",
            "background",
            "draw_annotations",
            "draw_forms",
        }
        if set(contract) != required_pdf:
            raise ContractError("a PDF page's render contract omits or adds pixel-affecting facts")
        settings = run.get("render_settings")
        if not isinstance(settings, dict) or set(settings) != {"pdf"}:
            raise ContractError("the run authority carries no unique PDF render setting")
        pdf_settings = settings["pdf"]
        if not isinstance(pdf_settings, dict) or set(pdf_settings) != {
            "configured_target_dpi",
            "target_dpi",
            "minimum_dpi",
        }:
            raise ContractError("the run authority carries an incomplete PDF render setting")
        configured = pdf_settings["configured_target_dpi"]
        target = pdf_settings["target_dpi"]
        minimum = pdf_settings["minimum_dpi"]
        if (
            not isinstance(contract["pdfium_version"], str)
            or not contract["pdfium_version"]
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in (configured, target, minimum)
            )
            or target != max(configured, minimum)
            or minimum != MIN_RENDER_DPI
            or contract["configured_target_dpi"] != configured
            or contract["dpi"] != target
            or contract["min_dpi"] != minimum
            or contract["scale"] != {"numerator": target, "denominator": POINTS_PER_INCH}
            or contract["background"] != RENDER_BACKGROUND
            or contract["draw_annotations"] is not DRAW_ANNOTATIONS
            or contract["draw_forms"] is not DRAW_FORMS
        ):
            raise ContractError("a PDF page's render contract changes the sealed pixel recipe")
        effective = contract["effective_dpi"]
        # A page too large for the target renders at a lower whole DPI, and the
        # contract has to say which. Outside the floor-to-target band it is not a
        # capped render at all, and pixels nobody can reproduce are not sealable.
        if (
            not isinstance(effective, int)
            or isinstance(effective, bool)
            or not contract["min_dpi"] <= effective <= contract["dpi"]
        ):
            raise ContractError(
                "a PDF page's render contract does not name the whole DPI it was "
                "actually rendered at, inside the recipe's own floor and target"
            )
        if output["codec"] != RENDER_CODEC or output["color_mode"] != RENDER_COLOR_MODE:
            raise ContractError("a PDF page's render contract changes its RGB pixel recipe")
    elif contract["renderer"] == "Pillow":
        if container_format == "pdf":
            raise ContractError("a PDF container must use the PDFium whole-page renderer")
        required_raster = required | {
            "pillow_heif_version",
            "libheif_version",
            "source_mode",
            "source_bands",
            "mode_transform",
        }
        if set(contract) != required_raster:
            raise ContractError(
                "a raster page's render contract omits or adds pixel-affecting facts"
            )
        if any(
            not isinstance(contract[field], str) or not contract[field]
            for field in ("pillow_heif_version", "libheif_version")
        ):
            raise ContractError("a raster page's render contract names no HEIF decoder version")
        source_mode = contract["source_mode"]
        source_bands = contract["source_bands"]
        transform = contract["mode_transform"]
        if (
            not isinstance(source_mode, str)
            or not source_mode
            or not isinstance(source_bands, list)
            or not source_bands
            or any(not isinstance(band, str) or not band for band in source_bands)
        ):
            raise ContractError("a raster page's render contract names no source pixel mode")
        expected_transform, expected_mode, expected_codec = raster_mode_transform(
            source_mode, source_bands
        )
        if (
            transform != expected_transform
            or output["codec"] != expected_codec
            or output["color_mode"] != expected_mode
        ):
            raise ContractError("a raster page's render contract changes its mode conversion")
    else:
        raise ContractError("a rendered page's contract names an unrecognized renderer")
    geometry = payload.get("geometry")
    if not isinstance(geometry, dict) or (contract["width"], contract["height"]) != (
        geometry.get("width"),
        geometry.get("height"),
    ):
        raise ContractError(
            "a rendered page's render contract disagrees with its admitted geometry"
        )


def _verify_refusal(admission: dict[str, Any]) -> None:
    if admission["inputs"]:
        raise ContractError("a refused source must not claim an admitted-blob input")
    # Refuses anything outside admission.RefusalReason: an arbitrary string here
    # would let a refusal reason mean nothing in particular.
    reason_code(admission["payload"].get("reason"))


def _verify_existing_corpus_seal(tree: RunTree) -> None:
    """Refuse a tampered seal before a rerun can call the run reusable."""
    seals = [
        entry for entry in tree.build_manifest(EXEMPLAR)["artifacts"] if entry["kind"] == "seal"
    ]
    if not seals:
        return
    expected = artifact_id(EXEMPLAR, "seal", SEAL_SUBJECT)
    if len(seals) != 1 or seals[0]["artifact_id"] != expected:
        raise ContractError(
            "an Exemplar carries exactly one corpus seal, under the derived corpus-seal "
            "identity; this run carries something else"
        )
    seal = tree.read_artifact(EXEMPLAR, "seal", expected)
    if not verify_self_hash(seal["payload"]):
        raise ContractError(
            "the existing Exemplar corpus seal fails its own self-hash: it was edited "
            "after it was sealed, and a rerun will not build on it"
        )


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
