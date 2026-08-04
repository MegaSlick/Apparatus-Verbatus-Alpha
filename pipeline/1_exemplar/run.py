"""Exemplar: the sealed source. Nothing downstream may alter it.

Reads what the door admitted and seals each admitted source as a page: the bytes
into the run tree's blob store, and a `page` artifact binding the page identity to
the source digest and the ordinal. From here on, every region in the run traces back
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
from common.contracts.approval import (  # noqa: E402
    APPROVAL_GATED_REAL_INGRESS,
    approval_record_reference_from_record,
    parse_data_gate_ingress_record,
)
from common.contracts.canonical import digest_bytes, self_hash, verify_self_hash  # noqa: E402
from common.contracts.envelope import validate_envelope, verify_input_bytes  # noqa: E402
from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.identities import artifact_id, page_id  # noqa: E402
from common.contracts.stages import DOOR, EXEMPLAR  # noqa: E402
from common.runtree.store import RunTree  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_COMPLETE,
    StageContext,
    adapter_recipe_for,
    open_context,
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
                payload={"ordinal": ordinal, "reason": admission["payload"]["reason"]},
            )
            page_refs.append(context.input_ref(result.relative_path))
            census.append(
                {"ordinal": ordinal, "page_id": None, "outcome": "refused", "source_sha256": None}
            )
            continue

        payload = admission["payload"]
        # Identity binds the digest of the bytes that were actually admitted, plus
        # the ordinal — spec 01's scheme, and the answer to audit Q12's truncated
        # hash of a *path*. Derived from the admission rather than from the fixture
        # so a real run, which has no fixture, names its pages the same way.
        identity = page_id(payload["sha256"], ordinal)
        result = context.publish(
            kind="page",
            subject_id=identity,
            outcome="sealed",
            inputs=[admission_ref, blob_ref],
            payload=_page_payload(payload, ordinal),
        )
        page_refs.append(context.input_ref(result.relative_path))
        census.append(
            {
                "ordinal": ordinal,
                "page_id": identity,
                "outcome": "sealed",
                "source_sha256": payload["sha256"],
            }
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

    context.finish()
    return EXIT_COMPLETE


def _page_payload(payload: dict[str, Any], ordinal: int) -> dict[str, Any]:
    """What a sealed page records: the ordinal, the sealed bytes, and any transform.

    `container_page_index`/`container_sha256` travel from the admission when the
    sealed bytes are a render rather than the submitted file itself — a PDF page or
    a fanned-out TIFF directory alike, hence the container-agnostic name (renamed
    from `pdf_page_index`: a fanned-out TIFF directory records the identical
    transform, and calling it PDF-only would be wrong the moment TIFF started
    fanning out too). ARCHITECTURE's third invariant needs the transform recorded,
    not merely performed.

    Two digests, two words. `source_sha256` is the digest of the bytes this page
    *is* — what `page_id` binds, and what `common/contracts/identities.py` calls it.
    `rendered_from.container_sha256` is the digest of the multi-page file the page
    was rendered out of. Both were called `source_sha256`, three lines apart, and
    they coincide for a standalone raster, which is what made the collision easy to
    miss and wrong for every rendered page.
    """
    sealed: dict[str, Any] = {
        "ordinal": ordinal,
        "source_sha256": payload["sha256"],
        "image_path": payload["stored_at"],
    }
    if "container_page_index" in payload:
        sealed["rendered_from"] = {
            "container_sha256": payload["container_sha256"],
            "container_page_index": payload["container_page_index"],
        }
    return sealed


def _open(args, registry_factory) -> StageContext:
    """Open the run, keeping the fixture-binding guard for the runs it applies to.

    A synthetic-fixture run is bound to a fixture and a scenario, and `open_context`
    refuses to run a direct stage against an unsealed configuration — spec 01's
    guard, and it stays. A real submission has no fixture at all: its
    `config_digest` binds the submission, the policy and the approval instead, so
    that comparison has nothing to compare and the run authority is read directly.
    The ingress record in `run.json` is what decides which of the two this is, and
    it is inside the authority's self-hash, so it cannot be quietly switched.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    mode, _policy_hash, _reference = parse_data_gate_ingress_record(run.get("ingress"))
    if mode != APPROVAL_GATED_REAL_INGRESS:
        return open_context(args, EXEMPLAR, registry_factory=registry_factory)
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
        sources[ordinal] = {"ordinal": ordinal, "relative_path": path, "sha256": digest}
    return sources


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
        if admission["artifact_id"] != artifact_id(DOOR, "admission", f"source-{ordinal}"):
            raise ContractError(f"door admission for ordinal {ordinal} has a derived-id mismatch")

        if admission["outcome"] == "admitted":
            blob_ref = _verify_admitted_blob(tree, admission, source)
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
            f"the door published no admission for submitted ordinal(s) {missing}; a source "
            "may not disappear between submission and sealing"
        )
    _verify_data_gate_evidence(tree, run, [item[1] for item in checked])
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
    tree: RunTree, admission: dict[str, Any], source: dict[str, Any]
) -> dict[str, str]:
    """Prove the sealed bytes are the bytes the door said it admitted.

    The sealed digest equals the submitted digest for a standalone raster. It
    legitimately differs for a page rendered out of a container (a PDF, or a
    fanned-out multi-directory TIFF) — and *only* then, and only when the
    admission records which page of which file produced it. A sealed page whose
    bytes silently differ from its source with no recorded transform is the one
    thing ARCHITECTURE's third invariant cannot survive.
    """
    payload = admission["payload"]
    stored_at, sealed_digest = payload.get("stored_at"), payload.get("sha256")
    if not _is_sha256(sealed_digest):
        raise ContractError("an admitted source records no lowercase sha256 for its bytes")
    if stored_at != tree.blob_path(DOOR, sealed_digest):
        raise ContractError("an admission's stored_at is not the content-addressed blob path")
    # The recorded transform is checked whenever one is *claimed*, not only when the
    # digests happen to differ. For a standalone raster they are equal, so the whole
    # branch used to be skipped and a fabricated `container_page_index` was never
    # looked at — then `_page_payload` read `container_sha256` beside it and died
    # with a bare KeyError. A record claiming a transform that did not happen is
    # the same class of untruth as a duplicate reason claiming an admission that
    # did not.
    claims_transform = "container_page_index" in payload or "container_sha256" in payload
    if sealed_digest != source["sha256"] and not claims_transform:
        raise ContractError(
            "an admitted source's sealed bytes differ from the bytes that were "
            "submitted, and no transform is recorded to explain it"
        )
    if claims_transform:
        if "container_page_index" not in payload or "container_sha256" not in payload:
            raise ContractError(
                "an admitted source records half a transform; a page rendered out of a "
                "container names both which container and which page of it"
            )
        if payload["container_sha256"] != source["sha256"]:
            raise ContractError(
                "a rendered page names a container digest the run authority did not submit"
            )
        index = payload["container_page_index"]
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ContractError("a rendered page records no non-negative page index")
        if sealed_digest == source["sha256"]:
            raise ContractError(
                "an admitted source records a render transform, but its sealed bytes are "
                "the submitted bytes unchanged; a transform that changed nothing was "
                "not performed"
            )
    if len(admission["inputs"]) != 1:
        raise ContractError("an admitted source must carry exactly one admitted-blob input")
    input_ref = admission["inputs"][0]
    if input_ref.get("relative_path") != stored_at or input_ref.get("sha256") != sealed_digest:
        raise ContractError("an admission's input does not name its content-addressed blob")
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
    return {"relative_path": stored_at, "sha256": sealed_digest}


def _verify_refusal(admission: dict[str, Any]) -> None:
    if admission["inputs"]:
        raise ContractError("a refused source must not claim an admitted-blob input")
    # Refuses anything outside `admission.RefusalReason`. The free-text reasons the
    # skeleton wrote are exactly what spec 03 replaced, and a consumer that accepted
    # one because it happened to be a string would have replaced nothing.
    reason_code(admission["payload"].get("reason"))


def _verify_data_gate_evidence(
    tree: RunTree, run: dict[str, Any], admissions: list[dict[str, Any]]
) -> None:
    """Keep real-input approval evidence live across the Exemplar boundary.

    The run authority is the authority: it says whether this run was synthetic or
    approval-gated, and it is inside its own self-hash. Every door admission must
    agree with it — all of them or none of them — so a run cannot carry a mixture
    in which some pages were gated and others simply were not.
    """
    mode, ingress_hash, ingress_reference = parse_data_gate_ingress_record(run.get("ingress"))
    references = [admission["payload"].get("data_gate_approval_ref") for admission in admissions]
    if mode != APPROVAL_GATED_REAL_INGRESS:
        if any(reference is not None for reference in references):
            raise ContractError(
                "a synthetic-fixture run's door admissions carry data-gate approval evidence"
            )
        return
    if ingress_reference is None or ingress_hash is None:
        raise ContractError("a real run's ingress carries no data-gate approval evidence")
    if any(reference is None for reference in references):
        raise ContractError("a real run's door admissions do not all carry approval evidence")

    parsed = [approval_record_reference_from_record(reference) for reference in references]
    if any(item.to_record() != ingress_reference.to_record() for item in parsed):
        raise ContractError(
            "a door admission's approval evidence disagrees with the self-hashed run ingress"
        )
    record = tree.read_approval_record(ingress_reference)
    if record["action"] != "data-gate":
        raise ContractError("a real run's approval evidence is not a data-gate approval")
    if record["target_version_hash"] != ingress_hash:
        raise ContractError("a real run's approval names a different data-gate policy version")


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
