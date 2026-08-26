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

**Spec 03 makes the handoff checked rather than merely read.** Before anything is
published, the door's census is reconciled against `run.json`'s submitted source
manifest — every submitted ordinal has exactly one door outcome and no door outcome
names an ordinal nobody submitted — and every admitted blob is verified against the
digest its admission claims. A source cannot disappear between submission and
sealing, and a page cannot be sealed over bytes that are no longer the bytes the
door inspected.

**Spec 03 adds the corpus seal**, one `kind="seal"` artifact per run, written once
every page has been accounted for. It is self-hashed the same way `run.json` is —
`self_hash`/`verify_self_hash` from `common/contracts/canonical.py` — so an edit
after sealing is detectable rather than merely undocumented, and a rerun over a
tampered seal refuses before it writes. It needs no new file shape: it is an
artifact like any other, published through the same `context.publish` every page
uses, and both downstream readers filter the Exemplar's manifest to `kind == "page"`,
so a third kind sitting beside them disturbs nothing.

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
from common.contracts.canonical import digest_bytes, self_hash, verify_self_hash  # noqa: E402
from common.contracts.envelope import validate_envelope, verify_input_bytes  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id, page_id  # noqa: E402
from common.contracts.stages import DOOR, EXEMPLAR  # noqa: E402
from common.corpus_register import read_snapshot, verify_snapshot_is_current  # noqa: E402
from common.exemplar_boundary import _verify_triage_derivative  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    StageContext,
    adapter_recipe_for,
    open_context,
    refuse_halted_run,
    run_stage,
    stage_parser,
)

SEAL_SUBJECT = "corpus-seal"


def main(registry_factory=ChairRegistry.from_toml) -> int:
    """Run under the explicitly supplied chair/config implementation."""
    args = stage_parser(__doc__.splitlines()[0]).parse_args()
    context = _open(args, registry_factory)
    tree = context.tree
    _verify_existing_corpus_seal(tree)

    sources = _submitted_sources(context.run)
    admissions = _checked_admissions(tree, context.run, sources)

    sealed = 0
    page_refs: list[dict[str, str]] = []
    census: list[dict[str, Any]] = []
    admitted_by_page: dict[str, list[tuple[int, dict, dict[str, str], dict[str, str]]]] = {}
    for ordinal, admission, admission_ref, blob_ref in admissions:
        if admission["outcome"] == "refused":
            # The refusal is carried forward as this stage's own outcome so the
            # page is accounted for here too. A unit that simply stopped being
            # mentioned would be invariant #10's imbalance.
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
        # Identity binds the admitted bytes' immutable origin, never the manifest
        # ordinal or path: inserting a row cannot rename a page, and two rows with
        # the same origin must seal as one page citing both submissions.
        identity = page_id(_page_origin(payload), {"operation": "whole"})
        admitted_by_page.setdefault(identity, []).append(
            (ordinal, admission, admission_ref, blob_ref)
        )

    for identity, members in admitted_by_page.items():
        ordinal, admission, _admission_ref, blob_ref = members[0]
        submission_rows = [sources[member_ordinal] for member_ordinal, *_rest in members]
        inputs = [
            member_admission_ref for _ordinal, _admission, member_admission_ref, _blob in members
        ]
        inputs.append(blob_ref)
        result = context.publish(
            kind="page",
            subject_id=identity,
            outcome="sealed",
            inputs=inputs,
            payload=_page_payload(admission["payload"], ordinal, sources[ordinal], submission_rows),
        )
        page_refs.append(context.input_ref(result.relative_path))
        for member_ordinal, member_admission, _member_ref, _member_blob in members:
            census.append(
                _census_row(
                    sources[member_ordinal],
                    ordinal=member_ordinal,
                    page_identity=identity,
                    outcome="sealed",
                    source_sha256=member_admission["payload"]["sha256"],
                )
            )
        sealed += 1

    if sealed == 0:
        raise ContractError("every admitted source failed to seal")

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


def _page_payload(
    payload: dict[str, Any],
    ordinal: int,
    source: dict[str, Any],
    submission_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
            # Ruling 14's unresolved interpretation leaves the memory-bounded
            # renderer provisional.  Until Tyrel settles it, a page whose effective
            # DPI is below the run target must not hide that reduction inside a
            # nested renderer recipe: the sealed Exemplar page says so plainly.
            sealed["render_resolution"] = resolution
    members = submission_rows or [{**source, "ordinal": ordinal}]
    sealed["submission_rows"] = [
        _submission_row(item) for item in sorted(members, key=lambda item: item["ordinal"])
    ]
    return sealed


def _submission_row(source: dict[str, Any]) -> dict[str, Any]:
    """One submitted-row citation; ordinal is row accounting, not page identity."""
    fields = (
        "ordinal",
        "relative_path",
        "sha256",
        "bytes",
        "ledger_sha256",
        "container_page_index",
    )
    return {field: source[field] for field in fields if source.get(field) is not None}


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


def _open(args, registry_factory) -> StageContext:
    """Open the run, keeping the fixture-binding guard for the runs it applies to.

    A synthetic-fixture run is bound to a fixture and a scenario, and `open_context`
    refuses to run a direct stage against an unsealed configuration — spec 01's
    guard, and it stays. A real submission has no fixture at all: its
    `config_digest` binds the submission and door execution recipe instead, so
    that comparison has nothing to compare and the run authority is read directly.
    The ingress record in `run.json` is what decides which of the two this is, and
    it is inside the authority's self-hash, so it cannot be quietly switched.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    verify_snapshot_is_current(run, args.corpus_register)
    read_snapshot(tree, run)
    mode = parse_ingress_record(run.get("ingress"))
    if mode != REAL_INGRESS:
        return open_context(args, EXEMPLAR, registry_factory=registry_factory)
    refuse_halted_run(tree, EXEMPLAR, args.hard_failure_config)
    return StageContext(
        tree=tree,
        run=run,
        fixture={},
        scenario=args.scenario,
        stage=EXEMPLAR,
        adapter_revision=adapter_recipe_for(run, EXEMPLAR),
        args=args,
        registry=None,
    )


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
        if not _is_sha256(digest):
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

    ledger_hashes: set[str] = set()
    files_by_path: dict[str, dict[str, Any]] = {}
    for ordinal, source in sources.items():
        ledger_hash = source.get("ledger_sha256")
        size = source.get("bytes")
        if not _is_sha256(ledger_hash):
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
        existing = files_by_path.setdefault(source_file["relative_path"], source_file)
        if existing != source_file:
            raise ContractError(
                "run.json repeats one filename with incompatible digest or byte-count entries; "
                "its filename ledger cannot be reconstructed"
            )
    if len(ledger_hashes) != 1:
        raise ContractError("run.json source rows name more than one filename ledger")
    ledger = {
        "schema": "submission-manifest.v1",
        "files": sorted(files_by_path.values(), key=lambda item: item["relative_path"]),
    }
    if self_hash(ledger) != next(iter(ledger_hashes)):
        raise ContractError(
            "run.json source rows do not reproduce the self-hashed filename ledger that "
            "admitted this real submission"
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
    if not isinstance(relative_path, str) or not _is_sha256(digest):
        raise ContractError("the door's manifest holds an invalid admission reference")
    try:
        data = tree.read_bytes(relative_path)
    except OSError as error:
        raise ContractError("a door admission's bytes could not be read") from error
    if digest_bytes(data) != digest:
        raise ContractError("a door admission's bytes no longer match the door's manifest")
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
    if not _is_sha256(sealed_digest):
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
        is_derivative = _is_triage_derivative(rendered_from["render_contract"])
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
    try:
        blob = tree.read_bytes(stored_at)
    except OSError as error:
        # A *deleted* blob, where the branch below catches a *changed* one. Without
        # this the stage died with a FileNotFoundError traceback and CPython's exit
        # 1, where `common/stage.py` turns a ContractError into EXIT_FATAL (2) and
        # says exit codes carry cause — while `_read_checked_admission` three
        # functions up catches OSError for exactly this class of failure.
        raise ContractError(
            "an admitted blob could not be read; the bytes the door sealed are no "
            "longer in the run tree, and a page cannot be sealed over bytes nobody has"
        ) from error
    if digest_bytes(blob) != sealed_digest:
        raise ContractError("an admitted blob's bytes no longer match their sealed digest")
    verify_input_bytes(input_ref, blob)
    if is_derivative:
        _verify_triage_derivative(rendered_from["render_contract"], parent_bytes, parent, blob)
    return {"relative_path": stored_at, "sha256": sealed_digest}


def _is_triage_derivative(contract: Any) -> bool:
    return (
        isinstance(contract, dict)
        and isinstance(contract.get("derivative_page"), dict)
        and contract["derivative_page"].get("kind") == "sealed-derivative-page-v1"
    )


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
        parent_bytes = tree.read_bytes(parent_ref["relative_path"])
    except OSError as error:
        raise ContractError(
            "a derivative page's submitted master could not be read; the page was not sealed "
            "because its lineage cannot be re-derived; restore the content-addressed master "
            "from the submitted bytes before retrying"
        ) from error
    verify_input_bytes(parent_ref, parent_bytes)
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
            or minimum != 72
            or contract["configured_target_dpi"] != configured
            or contract["dpi"] != target
            or contract["min_dpi"] != minimum
            or contract["scale"] != {"numerator": target, "denominator": 72}
            or contract["background"] != "white"
            or contract["draw_annotations"] is not True
            or contract["draw_forms"] is not True
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
        if output["codec"] != "png" or output["color_mode"] != "RGB":
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
        high_precision_tiff_modes = {
            "I": "I",
            "F": "F",
            "I;16B": "I;16B",
            "I;16L": "I;16",
        }
        preserved_png = {"1", "L", "LA", "RGB", "RGBA", "I;16"}
        if (
            not isinstance(source_mode, str)
            or not source_mode
            or not isinstance(source_bands, list)
            or not source_bands
            or any(not isinstance(band, str) or not band for band in source_bands)
        ):
            raise ContractError("a raster page's render contract names no source pixel mode")
        if source_mode in high_precision_tiff_modes:
            expected_transform = "lossless-tiff-samples"
            expected_mode = high_precision_tiff_modes[source_mode]
            expected_codec = "tiff"
        elif source_mode in preserved_png:
            expected_transform = "identity"
            expected_mode = source_mode
            expected_codec = "png"
        else:
            expected_mode = "RGBA" if "A" in source_bands else "RGB"
            expected_transform = f"convert-to-{expected_mode.lower()}"
            expected_codec = "png"
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
    # Refuses anything outside `admission.RefusalReason`. The free-text reasons the
    # skeleton wrote are exactly what spec 03 replaced, and a consumer that accepted
    # one because it happened to be a string would have replaced nothing.
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


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


if __name__ == "__main__":
    raise SystemExit(run_stage(main))
