"""Credential-free worker for one planned pre-Door ingest.

It accepts no output path from rendered content: the trusted parent supplies one
absolute folder in argv-independent JSON and Landlock/Seatbelt grants writes only
beneath that folder.  It has no provider environment and no network route.

Preview and commit are two independent launches of this module, so each reads the
source folder, confirmation file, triage instrument configuration and caller-selected
data-handling policy fresh from disk rather than sharing any state. A commit request
therefore carries the exact digests its preceding preview showed, and this module
refuses to write unless all four of what it reads now still hash to those — otherwise
the shown-before-written promise is only true when nothing raced it.

The pin is an equality test and nothing more.  It can only ever refuse: no branch
here lets a supplied digest cause a write that would not otherwise happen, and no
matching digest skips a check that would otherwise run.  That is what keeps the
preview a *screen* rather than an authority record — nothing about a preview is
written anywhere, a preview request may not carry a pin at all, and the commit
re-derives every value from disk and only compares.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, is_sha256
from common.contracts.errors import ContractError
from operations.submit import gate, inventory, submit
from operations.triage import instrument, producer

_REQUEST_FIELDS = {
    "operation",
    "source",
    "output_dir",
    "policy",
    "corpus_id",
    "mode",
    "confirmation_file",
    "expected_submission_manifest_sha256",
    "expected_confirmation_sha256",
    "expected_instrument_config_sha256",
    "expected_data_handling_policy_sha256",
}
_EXPECTED_DIGEST_FIELDS = (
    "expected_submission_manifest_sha256",
    "expected_confirmation_sha256",
    "expected_instrument_config_sha256",
    "expected_data_handling_policy_sha256",
)
# How many undecodable frames a refusal lists before it summarises the rest. A
# folder of holiday photographs would otherwise produce a refusal longer than the
# 2000 characters `errors.sanitize_detail` keeps, and a truncated list reads as a
# complete one.
_MAX_LISTED_REFUSALS = 10


@dataclass(frozen=True)
class PreparedIngest:
    source: Path
    output_dir: Path
    manifest: dict[str, Any]
    frames: tuple[producer.SubmittedFrame, ...]
    recipe: dict[str, Any]
    proxies: tuple[instrument.ProxySet, ...]
    evidence: tuple[dict[str, Any], ...]
    evidence_manifest: dict[str, Any]
    confirmation: dict[str, Any] | None
    produced: producer.ProducedTriage
    data_handling_policy_sha256: str


def main() -> int:
    try:
        request = _request(json.loads(sys.stdin.read()))
        prepared = _prepare(request)
    except (ContractError, OSError, TypeError, ValueError, UnicodeError) as error:
        _reply({"status": "refusal", "reason": str(error)})
        return 2
    if request["operation"] == "preview":
        _reply({"status": "preview", "summary": _summary(prepared)})
        return 0
    try:
        _commit(prepared)
    except (ContractError, OSError, TypeError, ValueError, UnicodeError) as error:
        _reply({"status": "uncertain", "reason": str(error)})
        return 3
    _reply({"status": "committed", "summary": _summary(prepared)})
    return 0


def _request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise ValueError("ingest request has an invalid shape")
    if value["operation"] not in {"preview", "commit"}:
        raise ValueError("ingest request names an unknown operation")
    if not all(
        isinstance(value[name], str) and value[name].strip()
        for name in ("source", "output_dir", "policy", "corpus_id")
    ):
        raise ValueError("ingest request has a missing path or corpus id")
    if value["mode"] not in {"manual", "semi", "auto"}:
        raise ValueError("ingest request names an undeclared triage mode")
    if value["confirmation_file"] is not None and not isinstance(value["confirmation_file"], str):
        raise ValueError("ingest confirmation file path is invalid")
    for name in _EXPECTED_DIGEST_FIELDS:
        if value[name] is not None and not is_sha256(value[name]):
            raise ValueError(f"ingest request field {name!r} must be a lowercase sha256 or null")
    if value["operation"] == "preview" and any(
        value[name] is not None for name in _EXPECTED_DIGEST_FIELDS
    ):
        # A preview is what *establishes* the digests a later commit is pinned to; a
        # preview request that already carries one is not a preview, it is a caller
        # trying to seed the very check meant to catch it changing its mind.
        raise ValueError("a preview request may not already carry an expected digest")
    return value


def _prepare(request: Mapping[str, Any]) -> PreparedIngest:
    policy_binding = gate.load_policy_binding(Path(request["policy"]))
    roots = gate.approved_storage_roots(policy_binding.policy)
    source = gate.require_approved_storage_location(
        Path(request["source"]), roots, "submitted folder"
    )
    output_dir = gate.require_approved_storage_location(
        Path(request["output_dir"]), roots, "ingest output folder"
    )
    if not output_dir.is_dir():
        raise ValueError("the ingest output location is not a directory")
    if gate.same_or_inside(source, output_dir):
        # Filesystem identity, not spelling: a case-variant path on default
        # (case-insensitive) APFS defeats a textual `is_relative_to` here.
        # The Door refuses a ledger or triage document that lives inside the folder
        # it inventories, in these words, because its next inventory would count
        # these produced records as submitted sources
        # (`pipeline/1_exemplar/door.py::real_submission`). Without the same rule
        # here the console cheerfully wrote fifteen immutable records into the
        # submitted folder, printed "What the Door will see", and handed over a
        # ready folder the Door then refused outright — the summary claiming an
        # admission that could not happen, and the submitted folder polluted with
        # produced evidence that a re-run would then have to inventory.
        raise ValueError(
            "the ingest output folder cannot live inside the submitted folder; otherwise the "
            "next inventory includes these produced records as submitted sources and the Door "
            "refuses the whole submission. Choose an empty approved folder beside it."
        )
    if any(output_dir.iterdir()):
        raise ValueError(
            "the ingest output folder is not empty; choose a new empty approved folder"
        )
    manifest = submit.build_manifest(submit.walk_folder(source))
    frames = _frames(source, manifest)
    config = instrument.load_config()
    proxies = _proxies(frames, manifest, config)
    evidence, evidence_manifest = instrument.candidate_evidence(proxies, config)
    recipe = instrument.producer_recipe(config)
    confirmation = (
        None
        if request["confirmation_file"] is None
        else producer.load_confirmation(Path(request["confirmation_file"]))
    )
    # The preview and commit are two independent worker launches, each reading
    # from disk fresh. Without a pin, an input could change in the window between
    # the printed preview and the write, and the write would silently commit
    # different bytes than the ones shown and (for a confirmation) validated on
    # screen — the write boundary this unit exists to hold. Commit is refused,
    # before anything is touched further, unless what it just read digests
    # identically to what the preview showed.
    #
    # Four mutable reads decide what is authorized and written, so four digests
    # are pinned. The submitted ledger and confirmation were the obvious two;
    # the triage instrument configuration was the third left open. Every
    # proxy, every candidate verdict, the sealed recipe, and — through the
    # selected pairs — *which* candidate-evidence files exist at all are computed
    # from it, and the commit launch re-reads it from disk exactly as it re-reads
    # the other inputs. The caller-selected data-handling policy decides whether
    # the source and destination are approved locations, and the preview explicitly
    # reports that check; allowing its bytes to move would let commit proceed under
    # an authority different from the one shown. A confirmed pass would have caught
    # an instrument change indirectly (the confirmation names its digest), but the
    # confirmation-free pass, which has no other binding, would have written a
    # different plan than the one printed. Corpus id and mode travel inside this
    # request, so they are identical across the two launches by construction.
    if request["operation"] == "commit":
        for expected, observed, changed in (
            (
                request["expected_submission_manifest_sha256"],
                digest_of(manifest),
                "the submitted folder",
            ),
            (
                request["expected_confirmation_sha256"],
                None if confirmation is None else digest_of(confirmation),
                "the confirmation file",
            ),
            (
                request["expected_instrument_config_sha256"],
                config.source_sha256,
                "the triage instrument configuration",
            ),
            (
                request["expected_data_handling_policy_sha256"],
                policy_binding.config_sha256,
                "the data-handling policy",
            ),
        ):
            if expected != observed:
                raise ValueError(
                    f"{changed} changed after the ingest preview was shown; nothing was "
                    "written. Run the ingest preview again and commit that exact result."
                )
    produced = producer.produce(
        frames,
        corpus_id=request["corpus_id"],
        mode=request["mode"],
        confirmation=confirmation,
        instrument_recipe=recipe if confirmation is not None else None,
        evidence_manifest=evidence_manifest if confirmation is not None else None,
        evidence_records=evidence if confirmation is not None else None,
    )
    # `produce` deliberately permits a confirmation-free pass and so also has
    # to represent an empty `clusters` list while it validates the file shape.
    # The committing Unit 6B seam rightly refuses that list: a supplied
    # confirmation is an authority for a corpus-register append, and an empty
    # authority is not a harmless no-op that may leave pre-confirmation files
    # behind. Refuse it here, before this worker is given write rights, with the
    # exact producer wording the later seam uses.
    if confirmation is not None and not produced.clusters:
        raise producer.ProducerRefusal(
            "confirmation names no cluster; no manifest documents were written"
        )
    return PreparedIngest(
        source=source,
        output_dir=output_dir,
        manifest=manifest,
        frames=frames,
        recipe=recipe,
        proxies=proxies,
        evidence=tuple(evidence),
        evidence_manifest=evidence_manifest,
        confirmation=confirmation,
        produced=produced,
        data_handling_policy_sha256=policy_binding.config_sha256,
    )


def _frames(source: Path, manifest: Mapping[str, Any]) -> tuple[producer.SubmittedFrame, ...]:
    """Reopen every ledgered file, bounded, through the anchored no-follow seam.

    This is the only production reader that feeds a real operator-chosen folder
    into the triage producer, and the producer's API takes whole frames: it
    digests, decodes and re-verifies `SubmittedFrame.data`, so the bytes are held
    in memory whether this function bounds them or not. What the bound changes is
    the failure. An unbounded `read()` over a folder of 600 dpi masters — an
    ordinary scanned volume, not an attack — ends as an OOM kill of the confined
    child, which returns no JSON at all and reaches the operator as "ingest did
    not return a checked ready-folder record" with an empty detail. That is a
    result lost behind an unreadable failure (GOVERNANCE 2).

    `inventory.MAX_SUBMITTED_BYTES` is the bound, not a number chosen here: it is
    this repository's declared ceiling on *retained* submitted bytes, and its own
    docstring reserves it for "any caller that asks this reader to retain
    content", which is exactly what this is. `submit.py` records why a second
    hand-kept copy of a limit is the wrong answer.
    """

    frames = []
    retained = 0
    for position, row in enumerate(manifest["files"], start=1):
        if row["bytes"] > inventory.MAX_SUBMITTED_BYTES:
            raise ValueError(_too_large(position, len(manifest["files"]), row["sha256"]))
        with inventory.open_submission_source(source, row["relative_path"]) as opened:
            # One byte past the ceiling: a file that grew between the ledger walk
            # and this reopen is caught by the length check rather than allocated.
            data = opened.handle.read(inventory.MAX_SUBMITTED_BYTES + 1)
            opened.assert_unchanged(expected_sha256=row["sha256"])
        if len(data) > inventory.MAX_SUBMITTED_BYTES:
            raise ValueError(_too_large(position, len(manifest["files"]), row["sha256"]))
        if digest_bytes(data) != row["sha256"]:
            raise ValueError("submitted source bytes changed after the ledger was read")
        retained += len(data)
        if retained > inventory.MAX_SUBMITTED_BYTES:
            raise ValueError(
                f"this submission holds more than {inventory.MAX_SUBMITTED_BYTES} bytes of "
                "master images, which is more than one triage pass may hold in memory at "
                "once; nothing was written. Prepare it as smaller submitted folders."
            )
        frames.append(producer.SubmittedFrame(path=row["relative_path"], data=data))
    return tuple(frames)


def _too_large(position: int, total: int, digest: str) -> str:
    return (
        f"submitted file {position} of {total} in the ledger's path order (digest "
        f"{digest[:12]}) is larger than the {inventory.MAX_SUBMITTED_BYTES}-byte ceiling on "
        "retained submitted bytes; nothing was written."
    )


def _proxies(
    frames: tuple[producer.SubmittedFrame, ...],
    manifest: Mapping[str, Any],
    config: instrument.InstrumentConfig,
) -> tuple[instrument.ProxySet, ...]:
    """Build every proxy, and name every frame that could not be decoded.

    All-or-nothing is the right semantics here and is kept: the candidate evidence
    is computed over the *set* of frames, so preparing a triage pass over some
    subset would write a ledger and an evidence manifest that disagree about what
    the corpus is. What was wrong was the report. One stray `.DS_Store`, `Thumbs.db`
    or PDF refused the entire folder with "triage proxy source bytes could not be
    decoded" — true, and naming neither how many files failed nor which, while the
    error copy told the operator to "correct that exact input".

    The identification is a position and a digest prefix, never a path: the
    data-handling policy's logging rule keeps declared filenames in the sealed
    manifest and the private refusal report, and allows terminal output counts,
    digests and locations. The ledger is sorted by path, so a position is
    something a person can find in a sorted listing.
    """

    proxies: list[instrument.ProxySet] = []
    undecodable: list[str] = []
    total = len(manifest["files"])
    for position, (frame, row) in enumerate(zip(frames, manifest["files"], strict=True), start=1):
        try:
            proxies.append(instrument.build_proxies_from_bytes(frame.data, config))
        except instrument.InstrumentRefusal:
            undecodable.append(f"file {position} (digest {row['sha256'][:12]})")
    if undecodable:
        listed = ", ".join(undecodable[:_MAX_LISTED_REFUSALS])
        remainder = len(undecodable) - _MAX_LISTED_REFUSALS
        if remainder > 0:
            listed += f", and {remainder} more"
        raise instrument.InstrumentRefusal(
            f"the triage instrument prepares decodable image masters only, and "
            f"{len(undecodable)} of {total} submitted file(s) could not be decoded: {listed}. "
            "Positions count in the ledger's path order. Move them out of the submitted "
            "folder and run ingest again; a container such as a PDF reaches the Door "
            "through `verbatus upload` instead."
        )
    return tuple(proxies)


def _paths(prepared: PreparedIngest) -> list[str]:
    """Every name the commit will create, complete — the promise the preview makes.

    `test_the_committed_folder_holds_exactly_the_files_the_preview_listed` holds
    this to the folder that actually results, on both branches, because a plan
    that is *nearly* the write is the same defect as a plan that is stale: the
    operator approved a list, and the list has to be the one they approved.
    """

    values = [
        "submission-manifest.json",
        "triage-producer-recipe.json",
        "candidate-evidence-manifest.json",
    ]
    for proxy in prepared.proxies:
        values.extend(
            (
                f"signature-proxy-{proxy.source_frame_sha256}.png",
                f"review-proxy-{proxy.source_frame_sha256}.png",
            )
        )
    for record in prepared.evidence:
        left, right = record["both_digests"]
        values.append(f"candidate-evidence-{left}-{right}.json")
    values.extend(("triage-decision-manifest.json", "triage-clusters.json"))
    if prepared.confirmation is not None:
        # `.corpus-register.json.lock` is `common/corpus_register._register_lock`'s
        # deliberate, persistent sibling — it serializes writers and a crash
        # releases it by closing the handle, so it is never removed afterwards.
        # It is listed because the commit really does create it: an operator who
        # approved fifteen names and found sixteen files was shown a plan that
        # was not the write, however harmless the extra one is.
        values.extend(
            (
                "corpus-register.json",
                ".corpus-register.json.lock",
                "triage-confirmation.json",
            )
        )
    values.append("ingest-ready.json")
    return values


def _summary(prepared: PreparedIngest) -> dict[str, Any]:
    candidates = [
        " ".join(
            (
                record["both_digests"][0][:12],
                record["both_digests"][1][:12],
                record["verdict"],
                str(record["thresholds"].get("near_duplicate_reason", "")),
            )
        )
        for record in prepared.evidence
    ]
    return {
        "submission_files": len(prepared.manifest["files"]),
        # The pin's own value: the digest of the whole canonical ledger, including
        # its self-hash field. Never shown — see `submission_ledger_self_hash`.
        "submission_manifest_sha256": digest_of(prepared.manifest),
        # What the operator is shown and what `ingest-ready.json` records. The Door
        # binds `ledger_sha256 = manifest["self_hash"]` into every admitted page, so
        # the self-hash is the value that actually travels; printing the canonical
        # digest under the word "ledger" gave the operator a number that matches
        # nothing in the run their submission later becomes (GOALS 5).
        "submission_ledger_self_hash": prepared.manifest["self_hash"],
        "confirmation_sha256": None
        if prepared.confirmation is None
        else digest_of(prepared.confirmation),
        "instrument_config_sha256": prepared.recipe["instrument_config_sha256"],
        "data_handling_policy_sha256": prepared.data_handling_policy_sha256,
        "mode": prepared.produced.manifest["records"][0]["mode"],
        "candidate_count": len(prepared.evidence),
        "confirmed_cluster_count": len(prepared.produced.clusters),
        "confirmed_clusters": _cluster_lines(prepared.confirmation),
        "planned_files": _paths(prepared),
        "candidates": candidates,
    }


def _cluster_lines(confirmation: Mapping[str, Any] | None) -> list[str]:
    # The preview's digest pins the confirmation's bytes, not what the operator
    # believes those bytes say: a file rewritten before the preview would commit
    # verbatim under an honestly-shown digest. Showing each cluster's page
    # designations and member digest prefixes puts the membership itself in
    # front of the operator, so what they approve is the content, not a number.
    if confirmation is None:
        return []
    return [
        "; ".join(
            f"{page['volume_id']} {page['designation']}: "
            + " ".join(digest[:12] for digest in page["member_frame_sha256"])
            for page in cluster["pages"]
        )
        for cluster in confirmation["clusters"]
    ]


def _commit(prepared: PreparedIngest) -> None:
    # The generic submission helper logs to stdout.  This worker's stdout is a
    # closed JSON protocol, so write the already-built ledger through its same
    # immutable atomic-create primitive instead.
    _write(prepared.output_dir / "submission-manifest.json", prepared.manifest)
    _write(prepared.output_dir / "triage-producer-recipe.json", prepared.recipe)
    _write(prepared.output_dir / "candidate-evidence-manifest.json", prepared.evidence_manifest)
    for proxy in prepared.proxies:
        _write_bytes(
            prepared.output_dir / f"signature-proxy-{proxy.source_frame_sha256}.png",
            proxy.signature_png,
        )
        _write_bytes(
            prepared.output_dir / f"review-proxy-{proxy.source_frame_sha256}.png",
            proxy.review_png,
        )
    for record in prepared.evidence:
        left, right = record["both_digests"]
        _write(prepared.output_dir / f"candidate-evidence-{left}-{right}.json", record)
    if prepared.confirmation is None:
        _write(prepared.output_dir / "triage-decision-manifest.json", prepared.produced.manifest)
        _write(prepared.output_dir / "triage-clusters.json", prepared.produced.clusters)
    else:
        producer.commit_confirmed_production(
            prepared.frames,
            corpus_id=prepared.produced.manifest["corpus_id"],
            mode=prepared.produced.manifest["records"][0]["mode"],
            confirmation=prepared.confirmation,
            instrument_recipe=prepared.recipe,
            evidence_manifest=prepared.evidence_manifest,
            evidence_records=prepared.evidence,
            register_path=prepared.output_dir / "corpus-register.json",
            manifest_path=prepared.output_dir / "triage-decision-manifest.json",
            clusters_path=prepared.output_dir / "triage-clusters.json",
            authority_path=prepared.output_dir / "triage-confirmation.json",
        )
    _write(prepared.output_dir / "ingest-ready.json", _ready_record(prepared))


def _ready_record(prepared: PreparedIngest) -> dict[str, Any]:
    return {
        "schema": "operator-ingest-ready-v1",
        "submission_manifest_sha256": digest_of(prepared.manifest),
        # The ledger identity every admitted page in the eventual run tree carries.
        # Both are recorded: the canonical digest is what this console pinned its
        # own commit to, and the self-hash is what a later reader can reconcile a
        # run against.
        "submission_ledger_self_hash": prepared.manifest["self_hash"],
        "data_handling_policy_sha256": prepared.data_handling_policy_sha256,
        "triage_manifest_sha256": digest_of(prepared.produced.manifest),
        "triage_recipe_sha256": digest_of(prepared.recipe),
        "candidate_evidence_manifest_sha256": digest_of(prepared.evidence_manifest),
        "confirmed_cluster_count": len(prepared.produced.clusters),
        "confirmation_file_retained": prepared.confirmation is not None,
    }


def _write(path: Path, record: Mapping[str, Any]) -> None:
    _write_bytes(path, canonical_bytes(record))


def _write_bytes(path: Path, data: bytes) -> None:
    # `submit._atomic_create` is the established immutable create-or-identical
    # seam. The output folder was required empty in the no-write preview, so an
    # existing result is always an interruption/concurrent-writer alarm, never
    # silently replaced evidence.
    with contextlib.redirect_stdout(io.StringIO()):
        submit._atomic_create(path, data)


def _reply(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - module worker boundary
    raise SystemExit(main())
