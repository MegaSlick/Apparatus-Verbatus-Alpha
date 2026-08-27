"""Credential-free worker for one planned pre-Door ingest.

It accepts no output path from rendered content: the trusted parent supplies one
absolute folder in argv-independent JSON and Landlock/Seatbelt grants writes only
beneath that folder.  It has no provider environment and no network route.

Preview and commit are two independent launches of this module, so each reads the
source folder, confirmation file, triage instrument configuration and caller-selected
data-handling policy fresh from disk rather than sharing any state. A commit request
therefore carries the exact digests and output-directory identity its preceding preview
showed, and this module refuses to write unless all five still match — otherwise the
shown-before-written promise is only true when nothing raced it.

The pin is an equality test and nothing more.  It can only ever refuse: no branch
here lets a supplied digest cause a write that would not otherwise happen, and no
matching digest skips a check that would otherwise run.  That is what keeps the
preview a *screen* rather than an authority record — nothing about a preview is
written anywhere, a preview request may not carry a pin at all, and the commit
re-derives every value from disk and only compares.
"""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from common import corpus_register
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, is_sha256
from common.contracts.errors import ContractError
from operations.submit import gate, inventory, submit
from operations.triage import instrument, producer

from .ingest_protocol import (
    EXPECTED_DIGEST_FIELDS,
    EXPECTED_OUTPUT_IDENTITY_FIELDS,
    MAX_CORPUS_ID_CHARACTERS,
    MAX_INGEST_CANDIDATE_PAIRS,
    MAX_INGEST_FRAMES,
    REQUEST_FIELDS,
)

# How many undecodable frames a refusal lists before it summarises the rest. A
# folder of holiday photographs would otherwise produce a refusal longer than the
# 2000 characters `errors.sanitize_detail` keeps, and a truncated list reads as a
# complete one.
_MAX_LISTED_REFUSALS = 10


@dataclass(frozen=True)
class PreparedIngest:
    output_dir: Path
    output_identity: tuple[int, int]
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
        _reply(
            {
                "status": "preview",
                "summary": _summary(prepared),
                "output_identity": _identity_record(prepared.output_identity),
            }
        )
        return 0
    try:
        _commit(prepared)
    except (ContractError, OSError, TypeError, ValueError, UnicodeError) as error:
        _reply({"status": "uncertain", "reason": str(error)})
        return 3
    _reply(
        {
            "status": "committed",
            "summary": _summary(prepared),
            "output_identity": _identity_record(prepared.output_identity),
        }
    )
    return 0


def _request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise ValueError("ingest request has an invalid shape")
    if value["operation"] not in {"preview", "commit"}:
        raise ValueError("ingest request names an unknown operation")
    if not all(
        isinstance(value[name], str) and value[name].strip()
        for name in ("source", "output_dir", "policy", "corpus_id")
    ):
        raise ValueError("ingest request has a missing path or corpus id")
    if len(value["corpus_id"]) > MAX_CORPUS_ID_CHARACTERS:
        raise ValueError(f"ingest corpus id is longer than {MAX_CORPUS_ID_CHARACTERS} characters")
    if value["mode"] not in {"manual", "semi", "auto"}:
        raise ValueError("ingest request names an undeclared triage mode")
    if value["confirmation_file"] is not None and not isinstance(value["confirmation_file"], str):
        raise ValueError("ingest confirmation file path is invalid")
    for name in EXPECTED_DIGEST_FIELDS:
        if value[name] is not None and not is_sha256(value[name]):
            raise ValueError(f"ingest request field {name!r} must be a lowercase sha256 or null")
    for name in EXPECTED_OUTPUT_IDENTITY_FIELDS:
        identity_part = value[name]
        if identity_part is not None and (
            not isinstance(identity_part, int)
            or isinstance(identity_part, bool)
            or identity_part < 0
        ):
            raise ValueError(
                f"ingest request field {name!r} must be a non-negative integer or null"
            )
    if value["operation"] == "preview" and any(
        value[name] is not None
        for name in (*EXPECTED_DIGEST_FIELDS, *EXPECTED_OUTPUT_IDENTITY_FIELDS)
    ):
        # A preview is what *establishes* the digests a later commit is pinned to; a
        # preview request that already carries one is not a preview, it is a caller
        # trying to seed the very check meant to catch it changing its mind.
        raise ValueError("a preview request may not already carry an expected digest")
    if value["operation"] == "commit" and any(
        value[name] is None for name in EXPECTED_OUTPUT_IDENTITY_FIELDS
    ):
        raise ValueError("a commit request must carry the previewed output directory identity")
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
    output_identity = _directory_identity(output_dir)
    if request["operation"] == "commit" and output_identity != (
        request["expected_output_device"],
        request["expected_output_inode"],
    ):
        raise ValueError(
            "the ingest output folder changed after the preview was shown; nothing was "
            "written. Choose a new empty approved folder and run ingest again."
        )
    if gate.same_or_inside(source, output_dir):
        # Filesystem identity, not spelling: a case-variant path on default
        # (case-insensitive) APFS defeats a textual `is_relative_to` here.
        # The Door inventories the entire submitted folder, so an ingest output
        # inside it would become a submitted source and make the ready folder unusable.
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
    if len(manifest["files"]) > MAX_INGEST_FRAMES:
        raise ValueError(
            f"this submission holds more than {MAX_INGEST_FRAMES} image masters, which is "
            "more than one bounded triage pass accepts; nothing was written. Prepare it as "
            "smaller submitted folders."
        )
    frames = _frames(source, manifest)
    config = instrument.load_config()
    proxies = _proxies(frames, manifest, config)
    evidence, evidence_manifest = _candidate_evidence(proxies, config)
    recipe = instrument.producer_recipe(config)
    confirmation = (
        None
        if request["confirmation_file"] is None
        else producer.load_confirmation(Path(request["confirmation_file"]))
    )
    # These four reads determine authorization, generated evidence, and written
    # records. Commit may only compare their freshly derived digests with preview's;
    # the supplied digests never skip validation or authorize another write. The
    # output directory's device/inode check above is the fifth pin.
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
    # A supplied confirmation authorizes a corpus-register append; an empty one
    # must be refused before this worker receives write rights, using the later
    # commit seam's exact refusal text.
    if confirmation is not None and not produced.clusters:
        raise producer.ProducerRefusal(
            "confirmation names no cluster; no manifest documents were written"
        )
    if _directory_identity(output_dir) != output_identity:
        raise ValueError(
            "the ingest output folder changed while the plan was being prepared; nothing "
            "was written. Choose a new empty approved folder and run ingest again."
        )
    return PreparedIngest(
        output_dir=output_dir,
        output_identity=output_identity,
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


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ValueError("the ingest output location is not a readable directory") from error
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("the ingest output location is not a directory")
    return (status.st_dev, status.st_ino)


def _identity_record(identity: tuple[int, int]) -> dict[str, int]:
    return {"device": identity[0], "inode": identity[1]}


def _frames(source: Path, manifest: Mapping[str, Any]) -> tuple[producer.SubmittedFrame, ...]:
    """Reopen ledgered files through the bounded, anchored no-follow seam.

    The producer retains whole frames, so this uses the inventory module's one
    retained-byte ceiling; exceeding it must be a refusal, never an OOM with no result.
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
    """Build every proxy, or identify every undecodable frame without paths.

    Candidate evidence covers the full frame set, so partial proxy production is
    invalid. Terminal policy permits ledger positions and digest prefixes, not names.
    """

    proxies: list[instrument.ProxySet] = []
    undecodable: list[str] = []
    total = len(manifest["files"])
    for position, (frame, row) in enumerate(zip(frames, manifest["files"], strict=True), start=1):
        try:
            proxy = instrument.build_proxies_from_bytes(frame.data, config)
        except instrument.InstrumentRefusal:
            undecodable.append(f"file {position} (digest {row['sha256'][:12]})")
            continue
        if (
            digest_bytes(proxy.signature_png) != proxy.signature_png_sha256
            or digest_bytes(proxy.review_png) != proxy.review_png_sha256
        ):
            raise instrument.InstrumentRefusal(
                "a triage proxy does not match the digest computed while it was prepared; "
                "nothing was written"
            )
        proxies.append(proxy)
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


def _candidate_evidence(
    proxies: tuple[instrument.ProxySet, ...], config: instrument.InstrumentConfig
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Bound both expensive comparisons and the refused-pair evidence list."""

    selection = instrument.select_candidate_pairs([proxy.signature for proxy in proxies], config)
    reached = len(selection.pairs) + len(selection.dimension_refused)
    if reached > MAX_INGEST_CANDIDATE_PAIRS:
        raise instrument.InstrumentRefusal(
            f"the triage instrument selected or explicitly refused {reached} candidate "
            f"pairs, above the {MAX_INGEST_CANDIDATE_PAIRS}-pair ceiling for one ingest; "
            "nothing was written. Prepare the submission as smaller folders."
        )
    evidence = [
        instrument.compare_signatures(proxies[left].signature, proxies[right].signature, config)
        for left, right in selection.pairs
    ]
    return evidence, instrument.evidence_manifest(proxies, selection, evidence, config)


def _paths(prepared: PreparedIngest) -> list[str]:
    """Every name commit creates; preview approval requires exact set equality."""

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
        # Commit pins the canonical ledger including its self-hash field.
        "submission_manifest_sha256": digest_of(prepared.manifest),
        # The Door binds the self-hash into every admitted page, so this is the
        # ledger identity shown to the operator and recorded in `ingest-ready.json`.
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
    _assert_output_identity(prepared)
    if any(prepared.output_dir.iterdir()):
        raise ValueError(
            "the ingest output folder changed after it was prepared; no existing entry was "
            "reused or overwritten"
        )
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
        committed_production, register_sha256 = producer.commit_confirmed_production(
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
        if committed_production != prepared.produced:
            raise ValueError(
                "confirmed triage production changed between preparation and publication"
            )
        register_path = prepared.output_dir / "corpus-register.json"
        if corpus_register.register_digest(register_path.read_bytes()) != register_sha256:
            raise ValueError(
                "the published corpus register does not match the verified chain digest"
            )
    _assert_output_identity(prepared)
    _assert_pre_ready_entries(prepared)
    _write(prepared.output_dir / "ingest-ready.json", _ready_record(prepared))


def _assert_output_identity(prepared: PreparedIngest) -> None:
    if _directory_identity(prepared.output_dir) != prepared.output_identity:
        raise ValueError(
            "the ingest output folder changed after the preview was shown; the ready record "
            "was not published"
        )


def _assert_pre_ready_entries(prepared: PreparedIngest) -> None:
    expected = set(_paths(prepared))
    expected.remove("ingest-ready.json")
    entries = list(prepared.output_dir.iterdir())
    if {entry.name for entry in entries} != expected or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise ValueError(
            "the ingest output folder does not contain exactly the regular files in the "
            "previewed plan; the ready record was not published"
        )


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
    # This flow requires a freshly empty folder.  An identical existing target is
    # therefore a concurrent change, not an idempotent retry: accepting it could
    # follow a planted symlink and call an output record written when it was not.
    if not submit._atomic_create(path, data):
        raise ValueError(
            "an ingest output entry appeared after the empty-folder check; no existing "
            "entry was accepted as this commit's evidence"
        )


def _reply(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":  # pragma: no cover - module worker boundary
    raise SystemExit(main())
