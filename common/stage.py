"""What every stage program needs, and nothing a stage should decide for itself.

Stages are programs that exchange versioned artifacts on disk, so this module
holds only shared plumbing (arguments, opening the run tree, publishing with a
correct envelope) and no pipeline logic: logic here would let one stage import
another through a side door.  The fixture is read as TOML data, not imported.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
import platform
import stat
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Protocol

from common import page_accounting, page_edges, page_path, page_reask
from common.alignment import (
    DEFAULT_ALIGNMENT_CONFIG_PATH,
    load_dissent_limits,
    sealed_dissent_budget,
)
from common.armarium_formats import (
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    ArmariumFormats,
    bind_armarium_formats,
)
from common.background import DEFAULT_INK_MAP_CONFIG_PATH
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig, ServingDetails, is_sha256
from common.chairs.protocol import ChairProtocol
from common.chairs.registry import ChairRegistry
from common.contracts.approval import (
    ADVANCE_ACTION,
    ADVANCE_SUBJECT_PREFIX,
    REAL_INGRESS,
    parse_ingress_record,
)
from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    digest_of,
    is_plain_int,
)
from common.contracts.envelope import build_envelope, digest_ref, read_verified
from common.contracts.errors import (
    ContractError,
    FatalAccounting,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import (
    artifact_id,
    attempt_id,
)
from common.contracts.outcomes import (
    BOUNDARY_OUTCOMES,
)
from common.contracts.outcomes import (
    WITNESS_READING_OUTCOMES as _WITNESS_READING_OUTCOMES,
)
from common.contracts.serving import (
    CHAIR_STREAM_CALL_RECORD_SCHEMA,
    RETIRED_SERVING_LAUNCH_AUDIT_SCHEMAS,
    SERVING_CONFIG_INPUTS_FIELDS,
    SERVING_CONFIG_INPUTS_SCHEMA,
    SERVING_LAUNCH_AUDIT_SCHEMA,
)
from common.contracts.stages import (
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    INK_MAP,
    PERLECTOR,
    RECENSOR,
    SEAL_PREDECESSORS,
    SIDE_SEALS,
    STAGES,
    TRIAGE_MODES,
)
from common.corpus_register import read_snapshot, verify_snapshot_is_current
from common.decoding import (
    DEFAULT_DECODING_CONFIG_PATH,
    load_decoding_policy,
    perlector_loop_guard,
    perlector_page_generation,
    verify_call_sampling,
)
from common.durability import is_unpublished_blob_temporary
from common.exemplar_boundary import (
    read_sealed_page,
    verify_reading_region_lineage,
    verify_sealed_page_pixels,
)
from common.hard_failure import (
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.imaging import dimensions
from common.page_accounting import DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH, load_page_accounting_policy
from common.reconstruction import DEFAULT_RECONSTRUCTION_CONFIG_PATH, load_reconstruction_policy
from common.recovery import DEFAULT_RECOVERY_CONFIG_PATH, load_recovery_policy
from common.replay import not_replayed_problem, refuse_imported_stage, replay_of
from common.residual_ink import ink_map_config_digest
from common.review_policy import DEFAULT_REVIEW_CONFIG_PATH, load_review_policy
from common.runtree.store import PublishResult, RunTree, _inode_identity
from common.sealed_config import read_sealed_toml, require_seal_method, require_sealed_config
from common.witness_adapters import validate_witness_adapter_bindings

# Exit codes carry cause: a structural failure that exits 0 reports success over
# work never done.
EXIT_COMPLETE = 0
EXIT_FATAL = 2
EXIT_HELD = 3

# The run-level hard-failure cap is breached.  Defined beside the other exits so
# none can collide with it.
EXIT_RUN_HALTED = 4


class RunHalted(ContractError):
    """The run-level hard-failure cap refuses another stage entry."""


_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
DEFAULT_PDF_RENDER_CONFIG_PATH = _CONFIG_DIR / "pdf_render.toml"
DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH = _CONFIG_DIR / "perlector_protocol.toml"
DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH = _CONFIG_DIR / "perlector_audit.toml"
# Padding changes the crop bytes a witness sees, so it is sealed into the run.
DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH = _CONFIG_DIR / "designator_geometry.toml"
# Grouping thresholds decide which acts exist, so they are sealed too.
DEFAULT_CORPUS_FRAME_CONFIG_PATH = _CONFIG_DIR / "corpus_frame.toml"
DEFAULT_SERVING_RECIPES_CONFIG_PATH = _CONFIG_DIR / "serving_recipes.toml"
DEFAULT_POD_PLACEMENT_CONFIG_PATH = _CONFIG_DIR / "pod_placement.toml"
DEFAULT_TRIAGE_MODES_CONFIG_PATH = _CONFIG_DIR / "triage_modes.toml"

# The run-level blind/named toggle, named once so the CLI, the config digest and
# the Perlectio schema cannot disagree about the closed set.
WITNESS_CONTEXT_REGIMES: Final = ("named", "blinded")

# The files that select the model configuration, given together or not at all:
# a missing one would otherwise fall back to its fixture default.
REAL_CONFIGURATION_FLAGS: Final = (
    "--models-config",
    "--serving-recipes-config",
)


def partial_real_configuration_refusal(given: Iterable[str]) -> str | None:
    """Why a selection naming only some of `REAL_CONFIGURATION_FLAGS` is refused, or None."""
    supplied = set(given)
    missing = [flag for flag in REAL_CONFIGURATION_FLAGS if flag not in supplied]
    if not supplied or not missing:
        return None
    return (
        f"{', '.join(REAL_CONFIGURATION_FLAGS)} select one model configuration together "
        f"(the chairs and the catalogue they are served under); "
        f"supply both or neither. Missing: {', '.join(missing)}"
    )


# A constant, never argv: the real `config_digest` binds no scenario, so an argv
# value would be a run-shaping fact nothing checks.
REAL_SCENARIO: Final = "real-submission"

# Bump whenever the real Door's output can change, so a real run cannot resume
# under pixels from another Door.  Lives here because `common/` rechecks it and
# may not import a stage.
REAL_DOOR_ADAPTER_REVISION: Final = "exemplar-door-v6"


def load_triage_modes(path: str | Path) -> str:
    """Read the closed triage-mode vocabulary and return its seal.

    Used at binding and at the point of use, so a run cannot seal a vocabulary
    that a later stage then refuses.
    """
    record, digest = read_sealed_toml(path, "triage modes configuration")
    if set(record) != set(TRIAGE_MODES) or any(
        not isinstance(policy, dict)
        or set(policy) != {"review_at_or_below_confidence"}
        or not is_plain_int(policy["review_at_or_below_confidence"])
        or not 0 <= policy["review_at_or_below_confidence"] <= 4
        for policy in record.values()
    ):
        raise ContractError("triage modes configuration has the wrong closed schema")
    return digest


# Outcomes where a chair actually served, so a serving receipt exists.  Shared
# by the Attestatores (writes receipts) and the Perlector (demands them).
ATTEMPTED_WITNESS_OUTCOMES = frozenset({"read", "genuinely-empty", "failed"})

# Outcomes that certify the region was read (a failed call does not).
# Re-exported so it cannot drift from `outcomes.witness_coverage`.
WITNESS_READING_OUTCOMES = _WITNESS_READING_OUTCOMES

# One manifest per upstream stage per pass.  `build_manifest` digests every
# artifact and consumers ask per act, so the cost is acts x artifacts, each
# digested.  Safe because an upstream stage is sealed before its consumers open;
# the stage being written is never cached.
_PASS_MANIFESTS: dict[tuple[str, str, str], dict[str, Any]] = {}


def stage_manifest(context, stage: str) -> dict[str, Any]:
    """`context.tree.build_manifest(stage)`, built once per pass where that is safe.

    Not cached for the stage being written, or for a tree without a root and
    run id: keying on `id()` could serve one run's inventory for another's.
    """
    writing = getattr(context, "stage", None)
    tree = context.tree
    root = getattr(tree, "root", None)
    run_id = getattr(tree, "run_id", None)
    if writing is None or root is None or run_id is None or stage == writing:
        return tree.build_manifest(stage)
    key = (str(root), str(run_id), stage)
    manifest = _PASS_MANIFESTS.get(key)
    if manifest is None:
        manifest = tree.build_manifest(stage)
        _PASS_MANIFESTS[key] = manifest
    return manifest


# One closed vocabulary for staged driver selections and the console that
# presents them.  Selection remains an invocation choice, never run-tree bytes.
RUN_MODES: Final = TRIAGE_MODES

# Boundaries a run stops at whatever the mode: the Attestatores and the Armarium
# when they exit held, and a Recensor that holds anything, before the Archetypus
# or the Armarium, until an advance passes its current seal (`boundary_advanced`).
# `test_advance_modes.py` checks this by driving the driver with fake stages.
ALWAYS_HELD_BOUNDARIES: Final = frozenset({ATTESTATORES, RECENSOR, ARMARIUM})


def current_stage_seal(tree: RunTree, stage: str) -> tuple[dict[str, Any], str] | None:
    """A stage's current stored seal and the sha256 of its bytes, or None when it has none.

    Read, not verified: the digest is what an advance record binds, taken from
    the immutable bytes rather than rebuilt from the payload, and the stage
    that reads this seal next verifies it.
    """
    manifest = tree.build_manifest(stage, verify_inputs=False)
    seals = [
        tree.read_artifact(stage, "stage-seal", entry["artifact_id"])
        for entry in manifest["artifacts"]
        if entry["kind"] == "stage-seal"
    ]
    if not seals:
        return None
    seal = latest_attempt(seals, f"{stage} stage seal", operation="seal")
    data = tree.read_bytes(tree.artifact_path(stage, "stage-seal", seal["artifact_id"]))
    return seal, digest_bytes(data)


def boundary_advanced(tree: RunTree, stage: str) -> bool:
    """Whether a person's advance record passes `stage`'s current sealed boundary.

    An advance binds the seal digest it was shown, so a re-seal (a Recensor
    pass that applied new decisions, say) leaves it bound to a boundary that
    is no longer current, and it passes nothing.
    """
    current = current_stage_seal(tree, stage)
    if current is None:
        return False
    subject = f"{ADVANCE_SUBJECT_PREFIX}{stage}"
    return any(
        record["action"] == ADVANCE_ACTION
        and record["subject_ids"] == [subject]
        and record["target_version_hash"] == current[1]
        for _reference, record in tree.approval_records()
    )


def _named_boundary(name: str, role: str) -> str:
    """Refuse a selection endpoint that owns no stage completion boundary."""

    if name not in STAGES:
        raise ContractError(
            f"{role} names {name!r}, which owns no stage completion boundary; "
            f"the boundaries are {', '.join(STAGES)}"
        )
    return name


def held_advance_boundaries(
    mode: str,
    *,
    stage: str,
    from_stage: str | None = None,
    to_stage: str | None = None,
) -> frozenset[str]:
    """Return every boundary a selected invocation can stop at, judging no evidence."""

    if mode not in RUN_MODES:
        raise ContractError(f"unknown staged run mode {mode!r}")
    _named_boundary(stage, "the advanced boundary")
    if mode == "auto":
        if from_stage is not None or to_stage is not None:
            raise ContractError("auto mode names no held range")
        return ALWAYS_HELD_BOUNDARIES
    if mode == "manual":
        if from_stage is not None or to_stage is not None:
            raise ContractError("manual mode names one stage, not a range")
        return frozenset({stage})
    # Explicit, because a mode added to `TRIAGE_MODES` must not fall through as semi.
    if mode != "semi":
        raise ContractError(f"staged run mode {mode!r} names no held-boundary rule")
    if from_stage is None or to_stage is None:
        raise ContractError("semi mode needs both the first and last stage of its range")
    first = STAGES.index(_named_boundary(from_stage, "the semi-mode range start"))
    last = STAGES.index(_named_boundary(to_stage, "the semi-mode range end"))
    if first > last:
        raise ContractError("semi mode cannot run a boundary backwards")
    span = frozenset(STAGES[first : last + 1])
    return frozenset({to_stage}) | (ALWAYS_HELD_BOUNDARIES & span)


# Closed, because a denylist would pass any field a later stage invents.  Which
# combination is legal is decided in `validate_serving_provenance`.
_PROVENANCE_FIELDS = frozenset(
    {
        "chair",
        "chair_state",
        "adapter_revision",
        "absence",
        "resolved_identity",
        "resolved_revision",
        "receipt_ref",
        "witness_regime",
    }
)


class StageChairProtocol(ChairProtocol, Protocol):
    """The small additional config surface a calling stage needs.

    ``ChairRegistry`` in production; the test fake implements it separately.
    """

    config: ModelsConfig


class ServingReader(Protocol):
    """What only the serving package can read back about a live chair call.

    `common/` never imports `operations/`, yet a verifier here re-derives a live
    page call from three serving facts: the sealed serving row a request was
    measured against, the engine's retained reply as the client parsed it, and
    the exact request bytes the client sent. A stage that verifies a page-read
    run opens its context with the serving package's reader
    (`operations.serving.assembly.SERVING_READER`). Each method raises
    `ContractError` when it refuses.
    """

    def serving_row(self, context: "StageContext", chair: ChairIdentity, tier: Any) -> Any:
        """The sealed serving row for `chair` at `tier`, re-read from the run's own seal."""
        ...

    def reading_reply(
        self, *, status: Any, body: bytes, kind: Any, model_id: str, stopped: bool | None = None
    ) -> tuple[str, str | None]:
        """`(content, finish_reason)` of one retained chat reply, parsed as the client did.

        `stopped` is `None` for a whole reply, else the reply was streamed and says
        whether the client stopped it (`chair-stream-call-record`).
        """
        ...

    def request_bytes(
        self,
        payload: Mapping[str, Any],
        *,
        model_id: str,
        seed: Any,
        sampling: Mapping[str, Any] | None = None,
        stream: bool = False,
    ) -> bytes:
        """The exact request bytes the client renders for a sampled `payload`, with the
        chair's sealed `sampling` row added when the payload does not already carry it,
        streamed when `stream`."""
        ...


# Recorded as well as folded into `config_digest`: a digest inside a hash can be
# verified but not named, so a reader of the run tree could not say which policy
# governed it.
SEALED_CONFIG_DIGESTS_FIELD: Final = "sealed_config_digests"


def run_sealed_config_digests(run: Mapping[str, Any]) -> dict[str, str]:
    """The point-of-use recheck digests a run authority recorded for itself.

    Never defaulted to empty, which would misreport every later check as a
    policy the binding step forgot.
    """
    recorded = run.get(SEALED_CONFIG_DIGESTS_FIELD)
    if not isinstance(recorded, dict) or not recorded:
        raise ContractError(
            "this run authority records no sealed configuration digests, so nothing here "
            "can prove which policy bytes governed it; a run created before the sealing "
            "family landed cannot be continued under a point-of-use recheck"
        )
    require_seal_method(run, "this run authority")
    if any(
        not isinstance(name, str) or not name or not is_sha256(digest)
        for name, digest in recorded.items()
    ):
        raise ContractError(
            "this run authority records a sealed configuration digest that is not a "
            "sha256 under a named policy"
        )
    return dict(recorded)


class StageContext:
    """One stage's view of the run it is part of."""

    __slots__ = (
        "tree",
        "run",
        "_fixture",
        "scenario",
        "stage",
        "adapter_revision",
        "args",
        "registry",
        "sealed_config_digests",
        "armarium_formats",
        "serving_config_inputs",
        "_recovery_policy",
        "sealed",
        "page_read_denominator",
        "serving_reader",
    )

    def __init__(
        self,
        tree,
        run,
        fixture,
        scenario,
        stage,
        adapter_revision,
        args,
        registry,
        sealed_config_digests=None,
        armarium_formats: ArmariumFormats | None = None,
        serving_config_inputs: Mapping[str, str] | None = None,
        recovery_policy: Mapping[str, Any] | None = None,
        serving_reader: ServingReader | None = None,
    ):
        self.tree = tree
        self.run = run
        # `None` on a real submission; see the `fixture` property.
        self._fixture = fixture
        self.scenario = scenario
        self.stage = stage
        self.adapter_revision = adapter_revision
        self.args = args
        self.registry = registry
        # Digests as checked against `run.json`, so a later re-read of a config
        # file can prove the file did not change in between.
        self.sealed_config_digests = dict(sealed_config_digests or {})
        # Parsed from the sealed bytes; Armarium must not reopen formats.toml.
        self.armarium_formats = armarium_formats
        self.serving_config_inputs = (
            MappingProxyType(_serving_config_inputs(serving_config_inputs, "StageContext"))
            if serving_config_inputs is not None
            else None
        )
        # Parsed once from the sealed bytes: a second read of
        # `config/recovery.toml` could see a rewrite the run never bound.
        self._recovery_policy = dict(recovery_policy) if recovery_policy is not None else None
        self.sealed = False
        # A page-read run's verified denominator, once a pass has asked for it:
        # its records are sealed before the pass runs, so it is verified once.
        self.page_read_denominator: (
            tuple[dict[int, dict[str, Any]], list[dict[str, Any]]] | None
        ) = None
        # Read back a live page call; `None` refuses one (`ServingReader`).
        self.serving_reader = serving_reader

    @property
    def fixture(self) -> dict[str, Any]:
        """The declared synthetic fixture, or a named refusal on a real submission.

        Refuses rather than returning `{}`, because `fixture.get("act", [])`
        would quietly pass a fixture-shaped check on a real run.
        """
        if self._fixture is None:
            raise ContractError(
                f"{self.stage} asked its context for fixture declarations on a real "
                "submission. Real ingress carries no fixture; this reader must derive the "
                "fact from sealed upstream evidence or refuse by name"
            )
        return self._fixture

    @property
    def config_digest(self) -> str:
        return self.run["config_digest"]

    def require_sealed_config(self, name: str, observed_sha256: str) -> None:
        """Refuse a configuration whose bytes changed after this run bound them."""
        require_sealed_config(self.sealed_config_digests, name, observed_sha256, "this context")

    @property
    def recovery_policy(self) -> dict[str, Any]:
        """This run's sealed re-ask budget, parsed once at binding.

        Refuses when absent, so a missing budget never reads as zero.
        """
        if self._recovery_policy is None:
            raise ContractError(
                "this context carries no run-sealed recovery policy; a stage may not read "
                "the budget from `config/recovery.toml` itself, because a rewrite between "
                "the run's binding check and that read would re-ask under an allowance the "
                "run never sealed. Open the run with `open_context`"
            )
        return dict(self._recovery_policy)

    @property
    def witness_chairs(self) -> list[str]:
        return list(self.run["witness_chairs"])

    @property
    def witness_floor(self) -> int:
        return self.registry.config.witness_floor

    @property
    def witness_context(self) -> str:
        """The sealed named/blinded regime this run's Perlector reads under.

        Read from argv, which is safe because a resume with a different value is
        refused: on fixture runs by `config_digest`, on real runs by the
        `run-policy` digest `_refuse_incompatible_real_reuse` recomputes.
        """
        return self.args.witness_context

    @property
    def perlector_protocol_config_path(self) -> str:
        return self.args.perlector_protocol_config

    @property
    def perlector_audit_config_path(self) -> str:
        return self.args.perlector_audit_config

    @property
    def page_accounting_config_path(self) -> str:
        return self.args.page_accounting_config

    def publish(
        self,
        *,
        kind: str,
        subject_id: str,
        outcome: str,
        payload: dict[str, Any],
        inputs: list[dict[str, str]] | None = None,
        attempt: str | None = None,
        approval_ref: str | None = None,
    ) -> PublishResult:
        """Publish one artifact of this stage, with the envelope filled in."""
        if self.sealed:
            raise SchemaRefusal(
                f"{self.stage} has sealed its completion boundary; publishing {kind!r} afterwards "
                "would make its witnessed inventory false"
            )
        if kind == "serving-receipt":
            raise SchemaRefusal(
                "serving receipts are run receipts, never stage artifacts; "
                "use StageContext.write_serving_receipt"
            )
        return self.tree.publish_artifact(
            self.envelope(
                kind=kind,
                subject_id=subject_id,
                outcome=outcome,
                payload=payload,
                inputs=inputs,
                attempt=attempt,
                approval_ref=approval_ref,
            )
        )

    def envelope(
        self,
        *,
        kind: str,
        subject_id: str,
        outcome: str,
        payload: dict[str, Any],
        inputs: list[dict[str, str]] | None = None,
        attempt: str | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        """The validated envelope `publish` would write, built without writing it.

        Its path and digest are what the published artifact's reference will name.
        """
        return build_envelope(
            run_id=self.tree.run_id,
            artifact_id=artifact_id(self.stage, kind, subject_id, attempt),
            subject_id=subject_id,
            stage=self.stage,
            kind=kind,
            outcome=outcome,
            config_digest=self.config_digest,
            adapter_revision=self.adapter_revision,
            inputs=inputs or [],
            payload=payload,
            attempt=attempt,
            approval_ref=approval_ref,
        )

    def seal_boundary(self) -> PublishResult:
        """Witness this stage's complete on-disk boundary exactly once per change.

        The stored seal is the evidence; a missing seal that a prior manifest
        names is refused, never recreated.
        """
        if self.sealed:
            raise SchemaRefusal(f"{self.stage} completion boundary is already sealed")
        records = _stage_records(self.tree, self.stage, "stage-seal")
        _refuse_deleted_seal(self.tree, self.stage, {record["artifact_id"] for record in records})
        prior = (
            latest_attempt(records, f"{self.stage} stage seal", operation="seal")
            if records
            else None
        )
        ordinal = 1 if prior is None else prior["payload"]["attempt_ordinal"] + 1
        attempt = attempt_id(self.stage, "seal", ordinal)
        # An unchanged restart reuses the previous seal rather than minting one.
        if prior is not None:
            if prior["payload"] == _stage_seal_payload(
                self.tree,
                self.stage,
                prior["payload"]["attempt_ordinal"],
                prior["attempt_id"],
            ):
                self.sealed = True
                return PublishResult(
                    self.tree.artifact_path(self.stage, "stage-seal", prior["artifact_id"]),
                    reused=True,
                )
        environment = _decode_environment(self.stage)
        self.publish(
            kind="decode-environment",
            subject_id=self.stage,
            outcome=_boundary_outcome(self.stage, "decode-environment"),
            attempt=attempt,
            payload=environment,
        )
        # Excluded from the inventory, so publishing it first is not circular.
        payload = _stage_seal_payload(self.tree, self.stage, ordinal, attempt)
        result = self.publish(
            kind="stage-seal",
            subject_id=self.stage,
            outcome=_boundary_outcome(self.stage, "stage-seal"),
            attempt=attempt,
            payload=payload,
        )
        self.sealed = True
        return result

    def write_serving_receipt(
        self, identity: ChairIdentity, serving: ServingDetails
    ) -> dict[str, str]:
        """Reverify one identity, then write this serving moment's receipt.

        Receipts live outside stage artifacts because they hold the endpoint and
        start moment.  Only byte-identical receipts are reused, never one found by
        model identity: a restarted endpoint has different serving facts.
        """
        self.registry.ensure(identity)
        receipt = self.registry.receipt(identity, serving)
        reference, _ = self.tree.write_run_receipt(receipt)
        return reference.to_record()

    def write_serving_launch_audit(self, audit: dict[str, Any]) -> dict[str, str]:
        """Store serving-manager operational evidence as a run-local blob.

        Kept apart from the receipt, whose schema is closed.
        """

        if not isinstance(audit, dict) or not audit:
            raise SchemaRefusal("serving launch audit must be a non-empty object")
        if self.serving_config_inputs is None:
            raise SchemaRefusal(
                "StageContext has no run-sealed serving configuration inputs; "
                "construct serving through open_context"
            )
        self._require_run_sealed_serving_inputs(audit)
        reference = self._write_serving_blob(audit, "serving launch audit")
        try:
            self.tree.note_launch_audit(self.stage, reference["sha256"])
        except (OSError, ContractError) as error:
            # A watcher's convenience; the audit itself is stored and referenced.
            print(
                f"{self.stage}: the launch audit was stored but not noted for the watcher: {error}",
                file=sys.stderr,
                flush=True,
            )
        return reference

    def write_serving_evidence_manifest(
        self,
        receipt_reference: Mapping[str, str],
        audit_reference: Mapping[str, str],
    ) -> dict[str, str]:
        """Bind the receipt and operational audit for one successful service."""

        checked_receipt_reference = _serving_evidence_reference(receipt_reference, "receipt")
        receipt = self.tree.read_run_receipt(checked_receipt_reference)
        checked_audit_reference = _serving_evidence_reference(audit_reference, "launch-audit")
        audit = self._read_serving_launch_audit(checked_audit_reference)
        if receipt["chair"] != audit["chair"]:
            raise SchemaRefusal(
                "serving receipt and launch audit name different chairs: "
                f"{receipt['chair']!r} and {audit['chair']!r}"
            )
        if receipt["started_at"] != audit.get("started_at"):
            raise SchemaRefusal(
                "serving receipt and launch audit name different start moments: "
                f"{receipt['started_at']!r} and {audit.get('started_at')!r}"
            )
        record = {
            "schema": "serving-evidence.v1",
            "receipt_reference": checked_receipt_reference,
            "launch_audit_reference": checked_audit_reference,
        }
        return self._write_serving_blob(record, "serving evidence manifest")

    def _read_serving_launch_audit(self, reference: dict[str, str]) -> dict[str, Any]:
        """Read a launch audit only from its verified stage-local content address."""

        expected_path = self.tree.blob_path(self.stage, reference["sha256"])
        if reference["relative_path"] != expected_path:
            raise SchemaRefusal(
                f"serving launch audit reference {reference['relative_path']!r} is not its "
                f"content-addressed path {expected_path!r}"
            )
        payload = read_verified(self.tree.read_bytes, reference, "serving launch audit")
        try:
            audit = json.loads(payload.decode("utf-8"))
            canonical = canonical_bytes(audit)
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise SchemaRefusal(
                f"serving launch audit {reference['relative_path']} could not be read: {error}"
            ) from error
        if canonical != payload or not isinstance(audit, dict):
            raise SchemaRefusal("serving launch audit is not a canonical JSON object")
        if audit.get("schema") in RETIRED_SERVING_LAUNCH_AUDIT_SCHEMAS:
            raise SchemaRefusal(
                f"serving launch audit was written as {audit['schema']}, which this build no "
                "longer reads; re-run"
            )
        if audit.get("schema") != SERVING_LAUNCH_AUDIT_SCHEMA:
            raise SchemaRefusal("serving launch audit has the wrong or missing schema")
        if not isinstance(audit.get("chair"), str) or not audit["chair"].strip():
            raise SchemaRefusal("serving launch audit has no non-blank chair")
        self._require_run_sealed_serving_inputs(audit)
        return audit

    def _require_run_sealed_serving_inputs(self, audit: dict[str, Any]) -> None:
        observed_inputs = _serving_config_inputs(
            audit.get("configuration_inputs"), "serving launch audit"
        )
        if observed_inputs != dict(self.serving_config_inputs or {}):
            raise SchemaRefusal(
                "serving launch audit configuration inputs differ from the run-sealed inputs"
            )

    def _write_serving_blob(self, value: dict[str, Any], label: str) -> dict[str, str]:
        try:
            payload = canonical_bytes(value)
        except (TypeError, ValueError) as error:
            raise SchemaRefusal(f"{label} is not canonical JSON data: {error}") from error
        return self.retain(payload, label)

    def retain(self, data: bytes, label: str = "a blob") -> dict[str, str]:
        """Store bytes in this stage's blob directory and return their reference.

        Refused after the seal, like `publish`: the blob directory is in the
        sealed inventory, and a late write would look like tampering to the
        next consumer.
        """
        if self.sealed:
            raise SchemaRefusal(
                f"{self.stage} has sealed its completion boundary; storing {label} afterwards "
                "would make its witnessed blob inventory false"
            )
        digest, result = self.tree.put_blob(self.stage, data)
        return {"relative_path": result.relative_path, "sha256": digest}

    def input_ref(self, relative_path: str) -> dict[str, str]:
        """An input reference to something already in this run tree.

        The digest is read from the bytes on disk rather than passed in, so a
        reference cannot claim a digest the file does not have.
        """
        return {
            "relative_path": relative_path,
            "sha256": digest_bytes(self.tree.read_bytes(relative_path)),
        }

    def artifact_ref(self, stage: str, kind: str, artifact_id: str) -> dict[str, str]:
        """A digest-checked reference to one already-published stage artifact."""
        return self.input_ref(self.tree.artifact_path(stage, kind, artifact_id))

    def finish(self, stage: str | None = None) -> None:
        """Write the stage's derived manifest inventory."""
        self.tree.write_manifest(stage or self.stage)


_SEAL_EXCLUDED_KINDS: Final = frozenset({"stage-seal", "decode-environment"})
_DECODE_PATHS: Final = frozenset({"project-png", "pillow", "pdfium", "none"})
# A stage that decodes or transforms image bytes seals `produced_pixels: true`
# (DAI makes the Attestatores one); pass-through stages record `none`.  The
# project-PNG route includes `grayscale_rows`'s Pillow fallback.
_STAGE_DECODE_PATHS: Final = MappingProxyType(
    {
        "door": frozenset({"pillow", "pdfium"}),
        "exemplar": frozenset({"project-png"}),
        "ink-map": frozenset({"project-png"}),
        "designator": frozenset({"project-png"}),
        "attestatores": frozenset({"project-png"}),
        "perlector": frozenset({"project-png"}),
        "recensor": frozenset({"project-png"}),
    }
)
_DECODER_NAMES: Final = frozenset({"pillow", "jpeg-codec", "pillow-heif", "libheif", "pdfium"})
_DECODE_ENVIRONMENT_FIELDS: Final = frozenset(
    {"decoders", "platform", "machine", "decode_paths_used", "produced_pixels"}
)


def _boundary_outcome(stage: str, kind: str) -> str:
    """The non-terminal outcome reserved for one boundary artifact kind."""
    try:
        return BOUNDARY_OUTCOMES[kind]
    except KeyError:
        raise SchemaRefusal(f"{stage} cannot publish unknown boundary kind {kind!r}") from None


def _manifest_artifact(tree: RunTree, stage: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    """Read the exact artifact bytes one manifest snapshot witnessed.

    Rechecked against the snapshot digest, so a file replaced after the
    manifest was built is refused.
    """
    record = tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
    if digest_bytes(canonical_bytes(record)) != entry.get("sha256"):
        raise SchemaRefusal(
            f"{stage} artifact {entry.get('artifact_id')!r} changed between its manifest "
            "snapshot and use; a completion seal cannot authorize replacement bytes"
        )
    return record


def _stage_records(
    tree: RunTree,
    stage: str,
    kind: str,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    snapshot = tree.build_manifest(stage, verify_inputs=False) if manifest is None else manifest
    return [
        _manifest_artifact(tree, stage, entry)
        for entry in snapshot["artifacts"]
        if entry["kind"] == kind
    ]


def _refuse_deleted_seal(tree: RunTree, stage: str, present: set[str]) -> None:
    """A manifest can expose deletion; it must never repair a witnessed seal.

    Compares the full set: deleting only the latest seal leaves a prefix that
    looks whole, and the earlier seal would answer for a boundary it never saw.
    """
    path = tree.resolve(tree.manifest_path(stage))
    if not path.exists():
        return
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SchemaRefusal(
            f"stored {stage} manifest cannot establish its prior seal: {error}"
        ) from error
    if not isinstance(stored, dict) or not isinstance(stored.get("artifacts"), list):
        raise SchemaRefusal(f"stored {stage} manifest cannot establish its prior seal")
    if stored.get("stage") != stage:
        raise SchemaRefusal(
            f"stored {stage} manifest names producer {stored.get('stage')!r}; "
            "a sibling inventory cannot establish which completion seals existed"
        )
    named: set[str] = set()
    for entry in stored["artifacts"]:
        if not isinstance(entry, dict) or entry.get("kind") != "stage-seal":
            continue
        name = entry.get("artifact_id")
        if not isinstance(name, str):
            raise SchemaRefusal(
                f"stored {stage} manifest names a stage-seal without a string artifact_id"
            )
        named.add(name)
    missing = sorted(named - present)
    if missing:
        raise SchemaRefusal(
            f"{stage} stage-seal(s) {missing} are missing although its stored inventory "
            "names them; a completion seal is witnessed evidence and is never re-derived"
        )


def _decode_environment(stage: str) -> dict[str, Any]:
    """The local decoders that can turn identical source bytes into pixels."""
    import pillow_heif
    import pypdfium2 as pdfium
    from PIL import Image, features

    jpg = features.version_codec("jpg") or "unavailable"
    turbo = features.version_feature("libjpeg_turbo")
    heif = pillow_heif.libheif_info().get("libheif", "unavailable")
    paths = _STAGE_DECODE_PATHS.get(stage, frozenset({"none"}))
    return {
        "decoders": [
            {"name": "pillow", "version": Image.__version__},
            {"name": "jpeg-codec", "version": f"{jpg};libjpeg-turbo={turbo}"},
            {"name": "pillow-heif", "version": pillow_heif.__version__},
            {"name": "libheif", "version": str(heif)},
            {"name": "pdfium", "version": str(pdfium.PDFIUM_INFO)},
        ],
        "platform": platform.system(),
        "machine": platform.machine(),
        "decode_paths_used": sorted(paths),
        "produced_pixels": paths != {"none"},
    }


def _validate_decode_environment(value: Any, owner: str) -> dict[str, Any]:
    """Require the closed decode-environment record before comparing it."""
    if not isinstance(value, dict) or set(value) != _DECODE_ENVIRONMENT_FIELDS:
        raise SchemaRefusal(f"{owner} decode-environment does not have the closed field set")
    decoders = value["decoders"]
    if not isinstance(decoders, list) or any(
        not isinstance(row, dict)
        or set(row) != {"name", "version"}
        or not isinstance(row["name"], str)
        or not isinstance(row["version"], str)
        or not row["version"]
        for row in decoders
    ):
        raise SchemaRefusal(f"{owner} decode-environment has a malformed decoder list")
    names = [row["name"] for row in decoders]
    if len(names) != len(set(names)) or set(names) != _DECODER_NAMES:
        raise SchemaRefusal(f"{owner} decode-environment does not name each decoder exactly once")
    for field in ("platform", "machine"):
        if not isinstance(value[field], str):
            raise SchemaRefusal(f"{owner} decode-environment has a malformed {field}")
    paths = value["decode_paths_used"]
    if (
        not isinstance(paths, list)
        or any(not isinstance(path, str) for path in paths)
        or paths != sorted(set(paths))
        or not set(paths) <= _DECODE_PATHS
    ):
        raise SchemaRefusal(f"{owner} decode-environment has malformed decode_paths_used")
    if not isinstance(value["produced_pixels"], bool):
        raise SchemaRefusal(f"{owner} decode-environment has malformed produced_pixels")
    return value


def _stage_seal_payload(
    tree: RunTree,
    stage: str,
    ordinal: int,
    attempt: str,
    *,
    verify_inputs: bool = True,
    verify_blob_addresses: bool = True,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Inputs are verified by default: the seal is the last chance to prove each
    # recorded input still matches.  A supplied `manifest` carries whatever
    # verification its builder chose.
    if manifest is None:
        manifest = tree.build_manifest(stage, verify_inputs=verify_inputs)
    artifacts = [
        entry for entry in manifest["artifacts"] if entry["kind"] not in _SEAL_EXCLUDED_KINDS
    ]
    blobs = _stage_blob_inventory(tree, stage, verify_addresses=verify_blob_addresses)
    census: dict[tuple[str, str], int] = {}
    for entry in artifacts:
        key = (entry["kind"], entry["outcome"])
        census[key] = census.get(key, 0) + 1
    decode_environment_artifact_id = artifact_id(stage, "decode-environment", stage, attempt)
    decode_environment_path = tree.artifact_path(
        stage, "decode-environment", decode_environment_artifact_id
    )
    try:
        decode_environment_sha256 = digest_bytes(tree.read_bytes(decode_environment_path))
    except OSError as error:
        raise SchemaRefusal(
            f"{stage} cannot seal its boundary: decode-environment "
            f"{decode_environment_artifact_id!r} is unreadable: {error}"
        ) from error
    run = tree.read_run()
    return {
        "stage": stage,
        "attempt_ordinal": ordinal,
        "attempt_id": attempt,
        "config_digest": run["config_digest"],
        "register_digest": run["register_digest"],
        "artifact_inventory": digest_of(artifacts),
        "blob_inventory": digest_of(blobs),
        "census": [
            {"kind": kind, "outcome": outcome, "count": count}
            for (kind, outcome), count in sorted(census.items())
        ],
        "decode_environment_artifact_id": decode_environment_artifact_id,
        "decode_environment_sha256": decode_environment_sha256,
    }


def _stage_blob_inventory(
    tree: RunTree, stage: str, *, verify_addresses: bool = True
) -> list[dict[str, str]]:
    """Read canonical blob files through stable, no-follow descriptors.

    Refuses odd spellings, links, name/content mismatches and files that change
    while hashed.  Hashed in chunks so a large image is not read into memory.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise SchemaRefusal("this platform cannot enforce no-follow blob inventory reads")
    blobs_root = tree.resolve(f"{tree.blob_path(stage, '0' * 64)}").parent
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(blobs_root, flags)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise SchemaRefusal(
            f"{stage} cannot seal its blob inventory without following links: {error}"
        ) from error
    try:
        opened_directory = os.fstat(directory_fd)
        named_directory = os.stat(blobs_root, follow_symlinks=False)
        if not stat.S_ISDIR(opened_directory.st_mode) or _inode_identity(
            opened_directory
        ) != _inode_identity(named_directory):
            raise SchemaRefusal(f"{stage} blob inventory is not one contained directory")
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        inventory = []
        folded_names: dict[str, str] = {}
        for name in names:
            # Names differing only by case give different inventories on case-
            # sensitive and case-insensitive filesystems.
            folded = name.casefold()
            previous = folded_names.get(folded)
            if previous is not None:
                raise SchemaRefusal(
                    f"{stage} blob inventory names {previous!r} and {name!r}, which collide "
                    "on a case-insensitive filesystem"
                )
            folded_names[folded] = name
            if name != folded and is_sha256(folded):
                raise SchemaRefusal(
                    f"{stage} blob inventory name {name!r} is a non-canonical case variant "
                    "of a sha256; a seal witnesses no noncanonical content address"
                )
            if not is_sha256(name):
                if is_unpublished_blob_temporary(name):
                    # A writer killed mid-publish leaves its temporary; skipped
                    # only once proven a plain regular file, never a link.
                    _refuse_unpublishable_temporary(directory_fd, name, stage)
                    continue
                raise SchemaRefusal(
                    f"{stage} blob inventory contains noncanonical content address {name!r}"
                )
            observed = _digest_regular_file_at(directory_fd, name, stage, siblings=names)
            if verify_addresses and observed != name:
                raise SchemaRefusal(
                    f"{stage} blob {name!r} contains digest {observed}, not the digest in its name"
                )
            inventory.append({"name": name, "sha256_of_content": observed})
        if _inode_identity(opened_directory) != _inode_identity(
            os.stat(blobs_root, follow_symlinks=False)
        ):
            raise SchemaRefusal(f"{stage} blob inventory directory changed while it was read")
        return inventory
    except OSError as error:
        raise SchemaRefusal(
            f"{stage} blob inventory changed or became unreadable while it was read: {error}"
        ) from error
    finally:
        os.close(directory_fd)


def _refuse_unpublishable_temporary(directory_fd: int, name: str, stage: str) -> None:
    """Prove one entry is a plain regular file before the exception skips it."""
    try:
        inspected = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise SchemaRefusal(
            f"{stage} blob inventory entry {name!r} changed while it was inspected: {error}"
        ) from error
    if stat.S_ISLNK(inspected.st_mode):
        raise SchemaRefusal(
            f"{stage} blob inventory entry {name!r} is a symlink; evidence blobs are no-follow"
        )
    if not stat.S_ISREG(inspected.st_mode):
        raise SchemaRefusal(
            f"{stage} blob inventory entry {name!r} wears the publisher's private temporary "
            "name but is not a regular file"
        )


def _publisher_link_allowance(
    directory_fd: int, siblings: Sequence[str], name: str, identity: tuple[int, int]
) -> int:
    """Extra links to `name` that its own interrupted publisher explains.

    A kill between hard-linking and unlinking the temporary leaves a second
    link to an intact blob.  Only a same-inode temporary with this blob's own
    digest explains a link; any other link is still refused.
    """
    explained = 0
    for other in siblings:
        if other == name or not other.startswith(f".{name}.tmp-"):
            continue
        if not is_unpublished_blob_temporary(other):
            continue
        try:
            sibling = os.stat(other, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:  # pragma: no cover - the temporary vanished mid-inventory
            continue
        if _inode_identity(sibling) == identity:
            explained += 1
    return explained


def _digest_regular_file_at(
    directory_fd: int, name: str, stage: str, *, siblings: Sequence[str] = ()
) -> str:
    """Hash one directory entry without changing which inode the name denotes."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as error:
        raise SchemaRefusal(
            f"{stage} blob {name!r} is not a readable no-follow file: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            named_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            identity = _inode_identity(opened)
            allowed_links = 1 + _publisher_link_allowance(directory_fd, siblings, name, identity)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink > allowed_links
                or identity != _inode_identity(named_before)
            ):
                raise SchemaRefusal(
                    f"{stage} blob {name!r} is not one contained regular file: it is reachable "
                    "under a name this store did not publish it under"
                )
            observed = hashlib.file_digest(handle, "sha256").hexdigest()
            closed_over = os.fstat(handle.fileno())
            named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise SchemaRefusal(
            f"{stage} blob {name!r} changed or became unreadable while it was inventoried: {error}"
        ) from error

    if (
        identity != _inode_identity(closed_over)
        or identity != _inode_identity(named_after)
        or not stat.S_ISREG(named_after.st_mode)
        or closed_over.st_nlink > allowed_links
        or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        != (closed_over.st_size, closed_over.st_mtime_ns, closed_over.st_ctime_ns)
    ):
        raise SchemaRefusal(f"{stage} blob {name!r} changed while it was inventoried")
    return observed


def verify_predecessor_seal(
    tree: RunTree, stage: str, *, manifest: Mapping[str, Any] | None = None
) -> None:
    """Refuse a missing, forged, or changed predecessor or side-branch boundary by name."""
    predecessor = SEAL_PREDECESSORS.get(stage)
    if predecessor is not None:
        _verify_stage_seal(tree, predecessor, stage, "predecessor", manifest=manifest)
    for producer in SIDE_SEALS.get(stage, ()):
        _verify_stage_seal(tree, producer, stage, "side branch")


def verify_stage_seal(tree: RunTree, producer: str, reader: str) -> None:
    """Prove one producer's completion seal as `reader` proves it when it opens."""
    role = "side branch" if producer in SIDE_SEALS.get(reader, ()) else "predecessor"
    _verify_stage_seal(tree, producer, reader, role)


def verify_final_seal(tree: RunTree) -> dict[str, Any]:
    """Prove the Armarium boundary, which has no successor stage to check it.

    ``SEAL_PREDECESSORS`` is consumer-keyed, so it cannot be used here.
    """
    manifest = tree.build_manifest(ARMARIUM, verify_inputs=False)
    _verify_stage_seal(tree, ARMARIUM, "the orchestrator", "final boundary", manifest=manifest)
    expected_id = artifact_id(ARMARIUM, "export", "export", None)
    exports = [
        entry
        for entry in manifest["artifacts"]
        if entry["kind"] == "export" and entry["artifact_id"] == expected_id
    ]
    if len(exports) != 1:
        raise SchemaRefusal(
            "the orchestrator refuses armarium final boundary: its manifest does not name "
            "exactly one derived export artifact"
        )
    return _manifest_artifact(tree, ARMARIUM, exports[0])


def _verify_stage_seal(
    tree: RunTree,
    producer: str,
    reader: str,
    role: str,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> None:
    """Keep stage consumers and the final orchestrator reader on one seal contract."""
    seals = _stage_records(tree, producer, "stage-seal", manifest=manifest)
    if not seals:
        raise SchemaRefusal(
            f"{reader} refuses: {role} {producer} has no stage-seal; "
            "a missing witnessed statement is never re-derived"
        )
    # The consumer checks too: tampering need never re-invoke the producer.
    _refuse_deleted_seal(tree, producer, {record["artifact_id"] for record in seals})
    seal = latest_attempt(seals, f"{producer} stage seal", operation="seal")
    payload = seal["payload"]
    expected_id = artifact_id(producer, "decode-environment", producer, seal["attempt_id"])
    if payload.get("decode_environment_artifact_id") != expected_id:
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: wrong decode environment name"
        )
    # One read for both comparisons, so they cannot straddle a rewrite.
    run_authority = tree.read_run()
    if payload.get("config_digest") != run_authority.get("config_digest"):
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: config_digest differs from run authority"
        )
    if payload.get("register_digest") != run_authority.get("register_digest"):
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: register_digest differs from run authority"
        )
    try:
        environment = tree.read_artifact(producer, "decode-environment", expected_id)
        environment_bytes = tree.read_bytes(
            tree.artifact_path(producer, "decode-environment", expected_id)
        )
    except (ContractError, OSError) as error:
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: its decode-environment is missing or damaged"
        ) from error
    _validate_decode_environment(environment.get("payload"), f"{producer} stored")
    actual_environment_sha256 = digest_bytes(environment_bytes)
    if payload.get("decode_environment_sha256") != actual_environment_sha256:
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: its decode-environment digest "
            "differs from the witnessed bytes"
        )
    try:
        expected = _stage_seal_payload(
            tree,
            producer,
            payload.get("attempt_ordinal"),
            seal["attempt_id"],
            manifest=manifest,
        )
    except (ContractError, OSError) as error:
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: its named inventory no longer "
            f"matches disk: {error}"
        ) from error
    if payload != expected:
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: its named inventory no longer matches disk"
        )


def _serving_evidence_reference(value: Mapping[str, str], label: str) -> dict[str, str]:
    """A string-shape check only; containment (including symlinks) is
    ``RunTree.resolve()``'s job when the path is read.

    ``digest_ref`` requires an exact ``dict``; a non-dict ``Mapping`` such as
    ``MappingProxyType`` is copied first so the signature's own promise holds.
    """
    if isinstance(value, Mapping) and not isinstance(value, dict):
        value = dict(value)
    return digest_ref(value, f"serving evidence {label} reference")


def _serving_config_inputs(value: object, label: str) -> dict[str, str]:
    """Validate the two exact TOML inputs that shape a serving launch."""

    if not isinstance(value, Mapping) or set(value) != SERVING_CONFIG_INPUTS_FIELDS:
        raise SchemaRefusal(
            f"{label} serving configuration must contain exactly schema, recipes, and placement digests"
        )
    schema = value["schema"]
    recipes_digest = value["serving_recipes_sha256"]
    placement_digest = value["pod_placement_sha256"]
    if schema != SERVING_CONFIG_INPUTS_SCHEMA:
        raise SchemaRefusal(
            f"{label} serving configuration schema must be {SERVING_CONFIG_INPUTS_SCHEMA!r}"
        )
    if not is_sha256(recipes_digest) or not is_sha256(placement_digest):
        raise SchemaRefusal(
            f"{label} serving configuration digests must be lowercase SHA-256 values"
        )
    return {
        "schema": schema,
        "serving_recipes_sha256": recipes_digest,
        "pod_placement_sha256": placement_digest,
    }


def stage_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    # No `choices`: the fixture declares scenarios, and `scenario_for` refuses others.
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--fixture-root", default="proof")
    parser.add_argument(
        "--corpus-register",
        default=None,
        help="append-only corpus register to snapshot at ingress and verify at later stages",
    )
    parser.add_argument(
        "--repository-commit",
        default=None,
        help=(
            "the commit this run's code is at, forwarded by the orchestrator from its own "
            "caller (on a pod, the commit the bootstrap checked out and verified). The Door "
            "seals it into the run authority so a fetched tree can say which code produced "
            "its bytes; absent means nobody named one, and the run records none"
        ),
    )
    parser.add_argument("--models-config", default="config/models.toml")
    parser.add_argument("--cache-root", default=None)
    parser.add_argument("--store-root", default=None)
    parser.add_argument(
        "--mechanics-qualification",
        action="store_true",
        help=(
            "explicit mechanics-only run: permit optically unproven serving "
            "profiles; every real launch and receipt remains required"
        ),
    )
    parser.add_argument(
        "--decoding-config",
        default=str(DEFAULT_DECODING_CONFIG_PATH),
        help="the sealed decoding posture of every reading chair",
    )
    parser.add_argument(
        "--serving-recipes-config",
        default=str(DEFAULT_SERVING_RECIPES_CONFIG_PATH),
        help=(
            "complete serving-profile catalogue sealed into this run; the default is the "
            "fixture-only catalogue"
        ),
    )
    parser.add_argument("--alignment-config", default=str(DEFAULT_ALIGNMENT_CONFIG_PATH))
    parser.add_argument(
        "--ink-map-config",
        default=str(DEFAULT_INK_MAP_CONFIG_PATH),
        help="the sealed ink-measurement policy: background, page-spanning bound, coverage audit",
    )
    parser.add_argument(
        "--page-accounting-config",
        default=str(DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH),
        help="the sealed thresholds of the check that a page reading missed nothing",
    )
    parser.add_argument("--pdf-render-config", default=str(DEFAULT_PDF_RENDER_CONFIG_PATH))
    parser.add_argument(
        "--designator-geometry-config", default=str(DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH)
    )
    parser.add_argument(
        "--perlector-protocol-config",
        default=str(DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH),
        help="the sealed Perlector protocol: page feed, page render and truncation",
    )
    parser.add_argument(
        "--perlector-audit-config",
        default=str(DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH),
        help="the sealed Perlector Pass-C audit declaration",
    )
    parser.add_argument("--formats-config", default=str(DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH))
    parser.add_argument(
        "--reconstruction-config",
        default=str(DEFAULT_RECONSTRUCTION_CONFIG_PATH),
        help="the sealed Coniector switches and bounds: whether its chair runs, whether the "
        "pages are consecutive, and how far a reconstruction may depart",
    )
    parser.add_argument("--recovery-config", default=str(DEFAULT_RECOVERY_CONFIG_PATH))
    parser.add_argument("--hard-failure-config", default=str(DEFAULT_HARD_FAILURE_CONFIG_PATH))
    parser.add_argument(
        "--review-config",
        default=str(DEFAULT_REVIEW_CONFIG_PATH),
        help="the share of pages a run may hold after the Recensor before it is called systemic",
    )
    parser.add_argument("--pdf-target-dpi", type=int, default=None)
    parser.add_argument(
        "--witness-context",
        default="named",
        choices=WITNESS_CONTEXT_REGIMES,
        help="the run-level named/blinded toggle a Perlectio's dossier is built under (spec 08)",
    )
    parser.add_argument(
        "--placement-tier",
        default=None,
        help=(
            "the measured placement tier of the card actually serving this run "
            "(e.g. generic-48gb); required to resolve a live serving profile "
            "(serving_mode_for), refused by name when a live catalogue is selected "
            "without it. Deliberately NOT sealed into config_digest: it is a measured "
            "runtime fact of the card, not run configuration, so it carries no "
            "'--no-placement-tier' companion and is simply omitted from a fixture "
            "run's argv. The record protects the past, which is "
            "why the receipt records the caps that actually bound the serving "
            "moment (the launch audit's profile.tier) rather than folding this into "
            "the reproducibility contract config_digest exists to protect."
        ),
    )
    parser.add_argument(
        "--capacity-plan",
        default=None,
        help=(
            "the capacity plan green PREFLIGHT derived for the card serving this run "
            "(canonical JSON, operations/serving/capacity.py): how many sequences each "
            "chair is launched with, never fewer than its row's max_num_seqs. Unsealed "
            "like --placement-tier: a measured runtime fact of the card, recorded in each "
            "launch audit; omitted, every row launches as written."
        ),
    )
    return parser


# The declaration file under a run's --fixture-root.
FIXTURE_DECLARATION = "skeleton_fixture.toml"


def load_fixture(fixture_root: str) -> dict[str, Any]:
    """Read the declared fixture as data; a missing one is a failure, not an empty run."""
    path = Path(fixture_root) / FIXTURE_DECLARATION
    if not path.exists():
        raise ContractError(
            f"no fixture declaration at {path}. The skeleton runs on declared "
            "synthetic pages only; a run with no input is a failure, not an "
            "empty success"
        )
    with open(path, "rb") as handle:
        fixture = tomllib.load(handle)
    if not fixture.get("page"):
        raise ContractError(f"{path} declares no pages")
    if "page_witness_chairs" in fixture:
        raise ContractError(
            f"{path} declares page_witness_chairs, a key retired to the models configuration's "
            "witness_scope. A stale fixture carrying it would be silently ignored rather "
            "than honoured; remove the key so the sealed roster is the only source of scope."
        )
    return fixture


def require_witness_context_regime(witness_context: str) -> None:
    """Refuse a regime outside the closed set before a run tree exists, on every path."""
    if witness_context not in WITNESS_CONTEXT_REGIMES:
        raise ContractError(
            f"witness_context {witness_context!r} is not one of {WITNESS_CONTEXT_REGIMES}"
        )


def real_run_policy_digest(
    *,
    witness_context: str,
    mechanics_qualification: bool = False,
) -> str:
    """The digest a real run seals its run-level reading knobs under.

    The real `config_digest` cannot be recomputed downstream, so these argv
    knobs need their own seal or a resume could change them unchecked.  Called
    at creation and at every stage open, so both sides hash the same set.
    """
    if not isinstance(mechanics_qualification, bool):
        raise ContractError(
            f"mechanics_qualification must be a bool, got {mechanics_qualification!r}"
        )
    return digest_of(
        {
            "witness_context_regime": witness_context,
            # Sealed so a run cannot mix ordinary and mechanics-only artefacts.
            "mechanics_qualification": mechanics_qualification,
        }
    )


def run_config_bindings(
    models: ModelsConfig,
    fixture: dict[str, Any],
    scenario: str,
    *,
    pdf_render_config_path: str | Path = DEFAULT_PDF_RENDER_CONFIG_PATH,
    pdf_render_config_sha256: str | None = None,
    designator_geometry_config_path: str | Path = DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH,
    alignment_config_path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
    page_accounting_config_path: str | Path = DEFAULT_PAGE_ACCOUNTING_CONFIG_PATH,
    ink_map_config_path: str | Path = DEFAULT_INK_MAP_CONFIG_PATH,
    reconstruction_config_path: str | Path = DEFAULT_RECONSTRUCTION_CONFIG_PATH,
    pdf_target_dpi: int | None = None,
    armarium_formats_config_path: str | Path = DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    recovery_config_path: str | Path = DEFAULT_RECOVERY_CONFIG_PATH,
    hard_failure_config_path: str | Path = DEFAULT_HARD_FAILURE_CONFIG_PATH,
    review_config_path: str | Path = DEFAULT_REVIEW_CONFIG_PATH,
    witness_context: str = "named",
    perlector_protocol_config_path: str | Path = DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    perlector_audit_config_path: str | Path = DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    mechanics_qualification: bool = False,
    serving_recipes_config_path: str | Path = DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    pod_placement_config_path: str | Path = DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    corpus_frame_config_path: str | Path = DEFAULT_CORPUS_FRAME_CONFIG_PATH,
    decoding_config_path: str | Path = DEFAULT_DECODING_CONFIG_PATH,
    triage_modes_config_path: str | Path = DEFAULT_TRIAGE_MODES_CONFIG_PATH,
) -> dict[str, Any]:
    """The three `run.json` bindings, and everything that shapes them.

    `config_digest` covers everything that shapes the run's behaviour.  The
    scenario must stay in it: two scenarios can declare identical pages, and
    reusing a run id under another must be refused before any write.  The real
    Door's decoder recipe is bound by ``door._real_bindings`` instead.
    """
    # The Door passes the digest of the bytes it parsed, so a rewrite between
    # two reads cannot seal one DPI and record another.
    if pdf_render_config_sha256 is not None:
        if not is_sha256(pdf_render_config_sha256):
            raise ContractError(
                "the supplied PDF render configuration digest is not a sha256; a binding "
                "may not be sealed under a value nothing could have hashed"
            )
        pdf_render_config_digest = pdf_render_config_sha256
    else:
        pdf_render_config_digest = read_sealed_toml(
            pdf_render_config_path, "PDF render configuration"
        )[1]
    perlector_protocol_config_digest = read_sealed_toml(
        perlector_protocol_config_path, "Perlector protocol configuration"
    )[1]
    perlector_audit_config_digest = read_sealed_toml(
        perlector_audit_config_path, "Perlector audit configuration"
    )[1]
    geometry_config_digest = read_sealed_toml(
        designator_geometry_config_path, "Designator geometry configuration"
    )[1]
    _, alignment_config_digest = load_dissent_limits(alignment_config_path)
    page_accounting_config_digest = load_page_accounting_policy(page_accounting_config_path).sha256
    reconstruction_config_digest = load_reconstruction_policy(reconstruction_config_path).sha256
    ink_map_digest = ink_map_config_digest(ink_map_config_path)
    corpus_frame_policy, corpus_frame_config_digest = load_corpus_frame_policy(
        corpus_frame_config_path
    )
    _decoding_policy, decoding_config_digest = load_decoding_policy(decoding_config_path)
    triage_modes_config_digest = load_triage_modes(triage_modes_config_path)
    armarium_formats_digest, armarium_formats = bind_armarium_formats(armarium_formats_config_path)
    serving_recipes_config_digest = read_sealed_toml(
        serving_recipes_config_path, "serving recipes configuration"
    )[1]
    pod_placement_config_digest = read_sealed_toml(
        pod_placement_config_path, "pod placement configuration"
    )[1]
    serving_config_inputs = {
        "schema": SERVING_CONFIG_INPUTS_SCHEMA,
        "serving_recipes_sha256": serving_recipes_config_digest,
        "pod_placement_sha256": pod_placement_config_digest,
    }
    recovery_policy = load_recovery_policy(recovery_config_path)
    hard_failure_policy = load_hard_failure_policy(hard_failure_config_path)
    review_policy = load_review_policy(review_config_path)
    validate_witness_adapter_bindings(models)
    require_witness_context_regime(witness_context)
    return {
        "witness_chairs": list(models.witness_chairs),
        "config_digest": digest_of(
            {
                "fixture": fixture,
                "scenario": scenario,
                "models": models.to_record(),
                "pdf_render_config_sha256": pdf_render_config_digest,
                "designator_geometry_config_sha256": geometry_config_digest,
                "alignment_config_sha256": alignment_config_digest,
                "page_accounting_config_sha256": page_accounting_config_digest,
                "reconstruction_config_sha256": reconstruction_config_digest,
                "ink_map_config_sha256": ink_map_digest,
                "corpus_frame_policy": corpus_frame_policy,
                "corpus_frame_config_sha256": corpus_frame_config_digest,
                "decoding_config_sha256": decoding_config_digest,
                "triage_modes_config_sha256": triage_modes_config_digest,
                "pdf_target_dpi_override": pdf_target_dpi,
                "armarium_formats_config_sha256": armarium_formats_digest,
                "armarium_formats": armarium_formats.to_record(),
                "recovery_policy": recovery_policy,
                "hard_failure_policy": hard_failure_policy,
                # Argv knobs: a resume under different values fails the digest.
                "witness_context_regime": witness_context,
                "perlector_protocol_config_sha256": perlector_protocol_config_digest,
                "perlector_audit_config_sha256": perlector_audit_config_digest,
                "mechanics_qualification": mechanics_qualification,
                "serving_config_inputs": serving_config_inputs,
            }
        ),
        "adapter_recipes": dict(sorted(models.adapter_recipes.items())),
        "serving_config_inputs": serving_config_inputs,
        # For point-of-use rechecks (`require_sealed_config`).  Every name has a
        # point of use; one without would promise a check nothing makes.
        # `hard-failure` is read before the run exists, so it is proven at the
        # first moment a run authority does.
        "sealed_config_digests": {
            "designator-geometry": geometry_config_digest,
            "alignment": alignment_config_digest,
            "page-accounting": page_accounting_config_digest,
            "reconstruction": reconstruction_config_digest,
            "ink-map": ink_map_digest,
            "corpus-frame-shard": corpus_frame_config_digest,
            "decoding": decoding_config_digest,
            "perlector-protocol": perlector_protocol_config_digest,
            "perlector-audit": perlector_audit_config_digest,
            "pdf-render": pdf_render_config_digest,
            "recovery": recovery_policy["config_sha256"],
            "hard-failure": hard_failure_policy["config_sha256"],
            "review": review_policy["config_sha256"],
            "triage-modes": triage_modes_config_digest,
        },
        "armarium_formats": armarium_formats,
        # Carried so no stage re-reads the file.
        "recovery_policy": recovery_policy,
    }


# Sealed by the Door from a Door-only flag: later stages can require the name
# but not recompute its value.
_REAL_DOOR_ONLY_SEALED_NAMES: Final = ("data-handling",)


def real_run_bindings(models: ModelsConfig, args) -> dict[str, Any]:
    """The downstream-relevant subset of a real run's bindings, recomputed at open.

    No `config_digest`: the real one binds the Door machine's ledger and
    decoders.  `models`, `armarium-formats` and `run-policy` exist only here,
    standing in for what the fixture path's `config_digest` rechecks whole.
    """
    validate_witness_adapter_bindings(models)
    require_witness_context_regime(args.witness_context)
    _, alignment_config_digest = load_dissent_limits(args.alignment_config)
    _corpus_frame_policy, corpus_frame_config_digest = load_corpus_frame_policy(
        DEFAULT_CORPUS_FRAME_CONFIG_PATH
    )
    _decoding_policy, decoding_config_digest = load_decoding_policy(args.decoding_config)
    armarium_formats_digest, armarium_formats = bind_armarium_formats(args.formats_config)
    serving_recipes_config_digest = read_sealed_toml(
        args.serving_recipes_config, "serving recipes configuration"
    )[1]
    pod_placement_config_digest = read_sealed_toml(
        DEFAULT_POD_PLACEMENT_CONFIG_PATH, "pod placement configuration"
    )[1]
    recovery_policy = load_recovery_policy(args.recovery_config)
    hard_failure_policy = load_hard_failure_policy(args.hard_failure_config)
    adapter_recipes = dict(sorted(models.adapter_recipes.items()))
    adapter_recipes[DOOR] = REAL_DOOR_ADAPTER_REVISION
    return {
        "witness_chairs": list(models.witness_chairs),
        "adapter_recipes": adapter_recipes,
        "serving_config_inputs": {
            "schema": SERVING_CONFIG_INPUTS_SCHEMA,
            "serving_recipes_sha256": serving_recipes_config_digest,
            "pod_placement_sha256": pod_placement_config_digest,
        },
        "sealed_config_digests": {
            "designator-geometry": read_sealed_toml(
                args.designator_geometry_config, "Designator geometry configuration"
            )[1],
            "alignment": alignment_config_digest,
            "page-accounting": load_page_accounting_policy(args.page_accounting_config).sha256,
            "reconstruction": load_reconstruction_policy(args.reconstruction_config).sha256,
            "ink-map": ink_map_config_digest(args.ink_map_config),
            "corpus-frame-shard": corpus_frame_config_digest,
            "decoding": decoding_config_digest,
            "perlector-protocol": read_sealed_toml(
                args.perlector_protocol_config, "Perlector protocol configuration"
            )[1],
            "perlector-audit": read_sealed_toml(
                args.perlector_audit_config, "Perlector audit configuration"
            )[1],
            "pdf-render": read_sealed_toml(args.pdf_render_config, "PDF render configuration")[1],
            "recovery": recovery_policy["config_sha256"],
            "hard-failure": hard_failure_policy["config_sha256"],
            "review": load_review_policy(args.review_config)["config_sha256"],
            "triage-modes": load_triage_modes(DEFAULT_TRIAGE_MODES_CONFIG_PATH),
            "serving-recipes": serving_recipes_config_digest,
            "pod-placement": pod_placement_config_digest,
            "models": models.models_digest,
            "armarium-formats": armarium_formats_digest,
            "run-policy": real_run_policy_digest(
                witness_context=args.witness_context,
                mechanics_qualification=getattr(args, "mechanics_qualification", False),
            ),
        },
        "armarium_formats": armarium_formats,
        "recovery_policy": recovery_policy,
    }


def load_corpus_frame_policy(path: str | Path) -> tuple[dict[str, int], str]:
    """Read the bounded corpus-frame policy and its seal."""
    record, digest = read_sealed_toml(path, "corpus-frame shard configuration")
    if set(record) != {"max_pages_per_shard"}:
        raise ContractError("corpus-frame shard configuration has the wrong closed schema")
    limit = record["max_pages_per_shard"]
    if not is_plain_int(limit) or not 1 <= limit <= 1000:
        raise ContractError("corpus-frame max_pages_per_shard must be an integer in [1, 1000]")
    return {"max_pages_per_shard": limit}, digest


def require_corpus_frame_shard(
    page_count: int,
    sealed_config_digests: Mapping[str, str],
    path: str | Path = DEFAULT_CORPUS_FRAME_CONFIG_PATH,
) -> None:
    """Point-of-use recheck for the sealed ≤1,000-page shard boundary."""
    policy, observed = load_corpus_frame_policy(path)
    bound = sealed_config_digests.get("corpus-frame-shard")
    if bound is None:
        # Unsealed and changed are different faults with different fixes.
        raise ContractError(
            "this run sealed no digest for the corpus-frame shard configuration; "
            "a shard may not be created under an unbound policy"
        )
    if bound != observed:
        raise ContractError(
            "the corpus-frame shard configuration changed between run binding and its "
            "run-creation check; a shard may not be created under unsealed bytes"
        )
    if page_count > policy["max_pages_per_shard"]:
        raise ContractError(
            f"corpus frame has {page_count} pages, above its sealed shard limit "
            f"of {policy['max_pages_per_shard']}"
        )


def require_triage_modes(
    sealed_config_digests: Mapping[str, str],
    path: str | Path | None = None,
) -> None:
    """Refuse a mode schema or content that differs from the run's sealed vocabulary."""
    if path is None:
        path = DEFAULT_TRIAGE_MODES_CONFIG_PATH
    bound = sealed_config_digests.get("triage-modes")
    if bound is None:
        raise ContractError("this run sealed no digest for the triage modes configuration")
    observed = load_triage_modes(path)
    if bound != observed:
        raise ContractError(
            "the triage modes configuration changed between run binding and its "
            f"point-of-use check: this run sealed {bound}, and {path} now seals to {observed}"
        )


# The roles addressed by name beside the witnesses, kept here so
# `unaddressed_chairs` sees the whole set.  `PERLECTOR_CHAIR` equals the stage
# name only by coincidence: chairs and stages are separate vocabularies.
PERLECTOR_CHAIR = PERLECTOR

# Absent by default, and named here so the absence is resolved and recorded:
# enabling a real detector must not silently turn every run `partial`.
SECONDARY_PROPOSER_CHAIR = "secondary_proposer"

# Surya's text-line and layout detector: the Designator runs it on every page,
# as a check that no ink goes unseen. It decides nothing.
DESIGNATOR_SURYA_CHAIR = "designator_surya"
# The Coniector's chair: the Perlector's model, asked text only.
RECONSTRUCTOR_CHAIR = "reconstructor"


def unaddressed_chairs(models: ModelsConfig) -> tuple[str, ...]:
    """Configured roles no stage in this pipeline will ever ask for.

    A misspelt role is still valid configuration, and would otherwise be
    silently never asked.  Absent chairs, and the base of an
    addressed adapter (recorded in its receipt), count as addressed.
    """
    addressed = set(models.witness_chairs) | {
        PERLECTOR_CHAIR,
        SECONDARY_PROPOSER_CHAIR,
        DESIGNATOR_SURYA_CHAIR,
        RECONSTRUCTOR_CHAIR,
    }
    for role in list(addressed):
        value = models.chairs.get(role)
        while isinstance(value, ChairIdentity) and value.adapter_of is not None:
            addressed.add(value.adapter_of)
            value = models.chairs.get(value.adapter_of)
    return tuple(
        sorted(
            role
            for role, value in models.chairs.items()
            if role not in addressed and not isinstance(value, AbsentChair)
        )
    )


def adapter_recipe_for(run: dict[str, Any], stage: str) -> str:
    """The recipe sealed for this producer, never a stage-local fallback."""
    recipes = run.get("adapter_recipes")
    if not isinstance(recipes, dict):
        raise ContractError("run.json has no adapter recipe map")
    recipe = recipes.get(stage)
    if not isinstance(recipe, str) or not recipe:
        raise ContractError(
            f"run.json has no non-blank adapter recipe for stage {stage!r}; "
            "a stage may not invent or substitute one"
        )
    return recipe


def fixture_serving_details(identity: ChairIdentity) -> ServingDetails:
    """The declared serving details of the walking skeleton's offline seam.

    Declared, not observed, and marked so (`fixture://`, `fixture`).
    `started_at` is constant because the determinism tests hash these receipts.
    """
    return ServingDetails(
        tokenizer_revision=identity.receipt_revision,
        seed=0,
        context_cap=4096,
        pixel_cap=52_000,
        engine=identity.serving_recipe,
        engine_version="fixture-v0",
        dtype="fixture",
        adapter_identity=None,
        endpoint="fixture://offline-chair-runner",
        started_at="2026-08-03T00:00:00Z",
    )


def validate_serving_provenance(
    context: StageContext,
    provenance: Any,
    *,
    producer_stage: str,
    require_receipt: bool,
) -> ChairIdentity | None:
    """Validate the identity/receipt projection a downstream stage consumes.

    Endpoint and start time stay in the receipt.  A configured identity must
    match the sealed models config, its revision and the receipt exactly.
    """
    if not isinstance(provenance, dict):
        raise SchemaRefusal("model provenance is not an object")
    leaked = sorted({"endpoint", "started_at"} & set(provenance))
    if leaked:
        raise SchemaRefusal(
            f"model provenance leaks serving-only field(s) {leaked}; use the run receipt reference"
        )
    # The check above names the known leaks; this allowlist closes the rest.
    unexpected = sorted(set(provenance) - _PROVENANCE_FIELDS)
    if unexpected:
        raise SchemaRefusal(
            f"model provenance carries unknown field(s) {unexpected}; a reading's provenance "
            "is a closed schema, and a field nothing validates is a field nothing can trust"
        )
    # Required of the Perlector, forbidden of everyone else.
    regime = provenance.get("witness_regime")
    if producer_stage == PERLECTOR:
        if regime not in WITNESS_CONTEXT_REGIMES:
            raise SchemaRefusal(
                f"a Perlectio records the witness regime it ran under; this one carries "
                f"{regime!r}, which is not one of {sorted(WITNESS_CONTEXT_REGIMES)}"
            )
    elif "witness_regime" in provenance:
        raise SchemaRefusal(
            f"only the Perlector records a witness regime; provenance produced by "
            f"{producer_stage!r} carries one"
        )
    if provenance.get("adapter_revision") != adapter_recipe_for(context.run, producer_stage):
        raise SchemaRefusal(
            f"model provenance does not carry the sealed adapter recipe for {producer_stage!r}"
        )

    state = provenance.get("chair_state")
    chair = provenance.get("chair")
    if not isinstance(chair, str) or not chair:
        raise SchemaRefusal("model provenance has no chair name")
    if state == "absent":
        configured = context.registry.resolve(chair)
        if not isinstance(configured, AbsentChair):
            raise SchemaRefusal(f"model provenance calls configured chair {chair!r} absent")
        if provenance.get("absence") != configured.to_record():
            raise SchemaRefusal(
                f"model provenance absence for {chair!r} differs from models config"
            )
        if any(
            provenance.get(field) is not None
            for field in ("resolved_identity", "resolved_revision", "receipt_ref")
        ):
            raise SchemaRefusal(f"absent chair {chair!r} carries a model identity or receipt")
        if require_receipt:
            raise SchemaRefusal(f"absent chair {chair!r} cannot have produced a reading")
        return None

    if state != "configured":
        raise SchemaRefusal(f"model provenance has unknown chair state {state!r}")
    # `absence` is allowed only on absent chairs, which returned above.
    if "absence" in provenance:
        raise SchemaRefusal(
            f"configured chair {chair!r} carries an absence record; a chair is configured "
            "or absent, and provenance claiming both is provenance nothing can trust"
        )
    record = provenance.get("resolved_identity")
    if not isinstance(record, dict):
        raise SchemaRefusal(f"configured chair {chair!r} has no resolved identity")
    try:
        identity = ChairIdentity(**record)
    except TypeError as error:
        raise SchemaRefusal(
            f"resolved identity for {chair!r} has the wrong schema: {error}"
        ) from error
    if chair != identity.role or record != identity.to_record():
        raise SchemaRefusal(f"resolved identity for {chair!r} is malformed")
    configured = context.registry.resolve(identity.role)
    if not isinstance(configured, ChairIdentity) or configured != identity:
        raise SchemaRefusal(
            f"resolved identity for {chair!r} differs from the sealed models config"
        )
    revision = provenance.get("resolved_revision")
    if revision != {
        "kind": identity.receipt_revision_kind,
        "value": identity.receipt_revision,
    }:
        raise SchemaRefusal(f"resolved revision for {chair!r} differs from its immutable identity")

    reference = provenance.get("receipt_ref")
    if not require_receipt:
        if reference is not None:
            raise SchemaRefusal(f"chair {chair!r} was not run but carries a serving receipt")
        return identity
    if not isinstance(reference, dict):
        raise SchemaRefusal(f"reading from chair {chair!r} has no serving receipt reference")
    receipt = context.tree.read_run_receipt(reference)
    expected = {
        "chair": identity.role,
        "source": identity.source,
        "resolved": identity.source_reference,
        "revision": identity.receipt_revision,
        "revision_kind": identity.receipt_revision_kind,
        "digest_manifest": identity.digest_manifest,
    }
    differing = [field for field, value in expected.items() if receipt.get(field) != value]
    if differing:
        raise SchemaRefusal(
            f"serving receipt for {chair!r} differs from the resolved identity at {differing}"
        )
    adapter = receipt.get("adapter_identity")
    if identity.adapter_of is None:
        if adapter is not None:
            raise SchemaRefusal(
                f"serving receipt for unadapted chair {chair!r} carries an adapter base identity"
            )
    else:
        configured_base = context.registry.resolve(identity.adapter_of)
        if not isinstance(configured_base, ChairIdentity) or adapter != configured_base.to_record():
            raise SchemaRefusal(
                f"serving receipt for adapter chair {chair!r} does not retain the configured "
                "base identity that also produced the reading"
            )
    return identity


def scenario_for(fixture: dict[str, Any], name: str) -> dict[str, Any]:
    """The declared scenario, refused loudly when the fixture does not name it."""
    for scenario in fixture.get("scenario", []):
        if scenario["name"] == name:
            return scenario
    declared = [scenario["name"] for scenario in fixture.get("scenario", [])]
    raise ContractError(f"the fixture declares no scenario {name!r}; declared: {declared}")


def _payload_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _sealed_page_rectangle(context, page_id: str, ordinal: int, what: str) -> dict[str, int]:
    """The whole rectangle of a sealed page, measured from its verified pixels."""
    sources = [
        source
        for source in context.run.get("source_manifest", [])
        if source.get("ordinal") == ordinal
    ]
    if len(sources) != 1:
        raise FatalAccounting(
            f"{what}'s page ordinal {ordinal} does not name exactly one sealed source"
        )
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    page_bytes = verify_sealed_page_pixels(context.tree, context.run, sources[0], page)
    width, height = dimensions(page_bytes)
    return {"x": 0, "y": 0, "w": width, "h": height}


# --- the page-read denominator ------------------------------------------------------
#
# A run counts the acts the Perlector established on each page it read whole.
# The records are the Perlector's page path (`pipeline/4_perlector/CONTRACT.md`,
# "Page reading"); what each says that decides the count or a hold -- the
# feed, the reading's answer and problems, the accounting, each entry's
# act-region and Perlectio, its dissent included -- is recomputed here from
# the sealed evidence with stage 4's own derivations (`common/page_path.py`),
# never trusted. The run tree binds every record read to this run's
# configuration. The contract, including what is bound rather than
# recomputed, is in `common/README.md`, "Page-read denominator".

READING_CLASS: Final = page_path.READING_CLASS
READING_UNPLACED_CLASS: Final = page_path.UNPLACED_CLASS
PAGE_UNREAD_CLASS: Final = "page-unread"
PAGE_BLANK_CLASS: Final = "page-blank"
# The row of a page the Exemplar refused: not an act and never counted as one,
# but a row, so no consumer can miss the page.
PAGE_REFUSED_CLASS: Final = "page-refused"
# The classes whose rows are counted units; a `page-refused` row is not one.
COUNTED_READING_CLASSES: Final = frozenset(
    {READING_CLASS, READING_UNPLACED_CLASS, PAGE_UNREAD_CLASS, PAGE_BLANK_CLASS}
)
# The hold each page-level row carries on top of the page's own codes, and the
# hold on every entry of a read page that names only `other` entries.
PAGE_UNREAD_HOLD: Final = "page-unread"
PAGE_BLANK_HOLD: Final = "page-blank-unconfirmed"
NO_ACT_ON_PAGE_HOLD: Final = "no-act-on-page-unconfirmed"

READING_DISPOSITIONS: Final = frozenset({page_path.READ, page_path.HELD})
PAGE_REFUSED_DISPOSITION: Final = "refused"
PAGE_READING_ROW_FIELDS: Final = frozenset(
    {
        "page_id",
        "page_ordinal",
        "parse_state",
        "disposition",
        "finish_reason",
        "reading_ref",
        "feed_ref",
        "accounting_ref",
        "reask_ref",
        "trigger_accounting_ref",
        "entry_count",
    }
)
READING_ACT_FIELDS: Final = frozenset(
    {
        "act_id",
        "act_key",
        "page_id",
        "page_ordinal",
        "n",
        "kind",
        "class",
        "disposition",
        "region_ref",
        "reading_ref",
        "perlectio_ref",
        "accounting_ref",
        "hold_codes",
        "flag_codes",
        "continues_from_previous_page",
        "continues_to_next_page",
        "reading_attempt",
        "reading_n",
    }
)


def _sealed_perlector_protocol(context) -> dict[str, Any]:
    protocol, digest = read_sealed_toml(
        context.perlector_protocol_config_path, "Perlector protocol declaration"
    )
    context.require_sealed_config("perlector-protocol", digest)
    return protocol


def _sealed_audit_not_run(context) -> dict[str, Any]:
    """The audit record every page reading of this run must carry: the sealed policy, not run."""
    from common.perlector_audit import audit_not_run

    policy, digest = read_sealed_toml(
        context.perlector_audit_config_path, "Perlector audit declaration"
    )
    context.require_sealed_config("perlector-audit", digest)
    if not isinstance(policy, dict) or "round_cap" not in policy:
        raise FatalAccounting(
            "the sealed Perlector audit declaration names no round cap, so no page reading's "
            "audit record can be checked against it"
        )
    return audit_not_run(policy, digest)


def reading_denominator(context) -> dict[str, Any]:
    """The acts every downstream count is taken over, verified once.

    `{"pages": page_readings rows, "acts": reading_acts rows}`.
    """
    pages, acts = _page_read_denominator(context)
    return {"pages": pages, "acts": acts}


def page_readings(context) -> dict[int, dict[str, Any]]:
    """One verified row per sealed Exemplar page of a page-read run, by page ordinal.

    Each sealed page has its first `page-reading` (attempt `page-read:1`) and
    its `page-accounting`, and its re-ask (`page-read:2`) and the accounting
    of both exactly when the re-ask plan recomputed over the first reading
    names ids; a sealed page with no reading is refused, never counted as
    zero acts. Row (`PAGE_READING_ROW_FIELDS`): `page_id`, `page_ordinal`,
    `parse_state`, `disposition` (`read` for a parsed, valid answer, else
    `held`) and `finish_reason`, all the current whole-page reading's (the
    first reading, or the page's last operator re-read); `reading_ref` (that
    reading), `feed_ref`, `accounting_ref` (the page's last accounting),
    `reask_ref` and `trigger_accounting_ref` (the re-ask and the first
    accounting that planned it, `None` on a page not re-asked or whose current
    reading is an operator re-read), and
    `entry_count` (the entries the last accounting counts, `act` and `other`
    alike; `None` for a page whose first answer was not read). It verifies
    the whole run, as `reading_acts` does; a caller needing both takes
    `reading_denominator`.
    """
    return _page_read_denominator(context)[0]


def reading_acts(context) -> list[dict[str, Any]]:
    """Every unit a page-read run counts, in page order, each proven from its records.

    One row per entry the page's last accounting counts (class `reading`, or
    `reading-unplaced` when it cites no placing id, by
    `page_accounting.placement_boxes`; a re-asked page's first reading's
    entries, then its re-ask's), one `page-unread` row for a sealed page whose
    first reading is not a parsed, valid answer, one `page-blank` row for a
    read page whose readings count no entry, and one `page-refused` row, not
    counted, for a page the Exemplar refused; every submitted page has at
    least one row. Fields are `READING_ACT_FIELDS`; see `common/README.md`,
    "Page-read denominator". A caller needing `page_readings` too takes
    `reading_denominator`, which verifies the run once.
    """
    return _page_read_denominator(context)[1]


def _page_read_denominator(
    context,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """The run's page-read denominator, verified once per context; each caller gets a copy."""
    if context.page_read_denominator is None:
        context.page_read_denominator = _verify_page_read_denominator(context)
    return copy.deepcopy(context.page_read_denominator)


def _verify_page_read_denominator(
    context,
) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    index = _PageReadRecords(context)
    pages = exemplar_page_ids(context)
    index.refuse_strays(set(pages.values()))
    rows: dict[int, dict[str, Any]] = {}
    acts: list[dict[str, Any]] = []
    for ordinal, page_id in sorted(pages.items()):
        page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
        if page.get("outcome") != "sealed":
            acts.append(_refused_page_row(context, index, ordinal, page_id))
            continue
        row, page_acts = _verify_page_reading(context, index, ordinal, page_id)
        rows[ordinal] = row
        acts.extend(page_acts)
    if not rows:
        raise FatalAccounting(
            "the Exemplar sealed no page, so a page-read run has nothing to count"
        )
    act_ids = [act["act_id"] for act in acts if act["class"] in COUNTED_READING_CLASSES]
    if len(set(act_ids)) != len(act_ids):
        raise FatalAccounting("two counted units of a page-read run share one act identity")
    return rows, acts


# Every kind the Perlector publishes: the page path's own, its Perlectio, the
# `reader-sent` marker of a live page call and the stage boundary records every
# stage writes. Any other kind is refused.
_PAGE_READ_TREE_KINDS: Final = page_path.PAGE_PATH_KINDS | {
    page_path.PERLECTIO_KIND,
    "reader-sent",
    "stage-seal",
    "decode-environment",
}


class _PageReadRecords:
    """The page-read run's sealed evidence, read once and grouped for the denominator."""

    def __init__(self, context) -> None:
        tree = context.tree
        self.tree = tree
        self._decisions: dict[str, tuple[Any, dict[str, Any]]] | None = None
        manifest = tree.build_manifest(PERLECTOR, verify_inputs=False)
        _verify_stage_seal(
            tree, PERLECTOR, "the page-read denominator", "reading", manifest=manifest
        )
        for entry in manifest["artifacts"]:
            if entry["kind"] not in _PAGE_READ_TREE_KINDS:
                raise FatalAccounting(
                    f"the Perlector published a {entry['kind']} ({entry['artifact_id']}), a kind "
                    "the page reading never writes, so this run's acts cannot be counted"
                )
        self.by_kind: dict[str, list[dict[str, Any]]] = {
            kind: _stage_records(tree, PERLECTOR, kind, manifest=manifest)
            for kind in (
                page_path.PAGE_READING_KIND,
                page_path.PAGE_ACCOUNTING_KIND,
                page_path.ACT_REGION_KIND,
                page_path.PERLECTIO_KIND,
            )
        }
        for record in self.by_kind[page_path.PERLECTIO_KIND]:
            schema = _payload_of(record).get("schema")
            if schema != page_path.PERLECTIO_SCHEMA:
                raise FatalAccounting(
                    f"Perlectio {record.get('artifact_id')!r} has schema {schema!r}, not the page "
                    f"reading's {page_path.PERLECTIO_SCHEMA!r}, so this run's acts cannot be "
                    "counted"
                )
        protocol = _sealed_perlector_protocol(context)
        self.protocol = protocol
        self.audit = _sealed_audit_not_run(context)
        self.truncation_policy = protocol.get("truncation")
        self.accounting_policy = page_accounting.require_page_accounting_policy(
            context, context.page_accounting_config_path
        )
        try:
            self.reask_budget = page_reask.reask_budget(context.recovery_policy)
        except ContractError as error:
            raise FatalAccounting(f"the page re-ask budget cannot be read: {error}") from error
        self.designator_entries = tree.build_manifest(DESIGNATOR, verify_inputs=False)["artifacts"]
        self.ink_entries = tree.build_manifest(INK_MAP, verify_inputs=False)["artifacts"]
        self.surya_census = page_path.sealed_surya_census(context, self.designator_entries)
        self.record_detector_configured = isinstance(
            context.registry.resolve(SECONDARY_PROPOSER_CHAIR), ChairIdentity
        )
        self.fixture_placeholders = not is_real_ingress(context.run)
        # Each page's current page Testimonium per chair, every record validated
        # here as stage 4 validated it. Imported here: `page_testimonia` reads
        # this module.
        from common import page_testimonia

        try:
            self.testimonia = page_testimonia.current_page_testimonia(context)
        except ContractError as error:
            raise FatalAccounting(
                f"a page Testimonium the page-read run was shown does not verify: {error}"
            ) from error

    @property
    def decisions(self) -> dict[str, tuple[Any, dict[str, Any]]]:
        """The run's stored review decisions by digest, read once, for operator re-reads."""
        if self._decisions is None:
            # Imported here: `page_reread` reads the Recensor's reviews through this module.
            from common.page_reread import stored_decisions

            self._decisions = stored_decisions(self.tree)
        return self._decisions

    def by_subject(self, kind: str, subject: str) -> list[dict[str, Any]]:
        return [record for record in self.by_kind[kind] if record.get("subject_id") == subject]

    def on_page(self, kind: str, page_id: str) -> list[dict[str, Any]]:
        return [
            record for record in self.by_kind[kind] if _payload_of(record).get("page_id") == page_id
        ]

    def ref(self, record: Mapping[str, Any]) -> dict[str, str]:
        path = self.tree.artifact_path(PERLECTOR, record["kind"], record["artifact_id"])
        return {"relative_path": path, "sha256": digest_bytes(self.tree.read_bytes(path))}

    def refuse_strays(self, page_ids: set[str]) -> None:
        """No page record may name a page this run's Exemplar never published."""
        for kind, records in self.by_kind.items():
            for record in records:
                page = (
                    record.get("subject_id")
                    if kind in (page_path.PAGE_READING_KIND, page_path.PAGE_ACCOUNTING_KIND)
                    else _payload_of(record).get("page_id")
                )
                if page not in page_ids:
                    raise FatalAccounting(
                        f"Perlector {kind} {record.get('artifact_id')!r} names page {page!r}, "
                        "which this run's Exemplar never published"
                    )


def _one(records: list[dict[str, Any]], what: str, attempt: str) -> dict[str, Any]:
    """The one record of a subject, at the one attempt the page path publishes for it."""
    if not records:
        raise FatalAccounting(f"{what} is missing; a counted unit without it is not proven")
    if len(records) > 1:
        raise FatalAccounting(
            f"{what} has {len(records)} records; the page path publishes one, and nothing "
            "may choose between them"
        )
    record = records[0]
    if record.get("attempt_id") != attempt:
        raise FatalAccounting(
            f"{what} is attempt {record.get('attempt_id')!r}, not the page path's "
            f"attempt {attempt!r}"
        )
    return record


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FatalAccounting(message)


def _problem_codes(problems: Any, what: str) -> list[str]:
    """The code of each recorded problem; a problem without a non-empty string code refuses."""
    _require(isinstance(problems, list), f"{what} records no list of problems")
    codes = [problem.get("code") if isinstance(problem, Mapping) else None for problem in problems]
    _require(
        all(isinstance(code, str) and code for code in codes),
        f"{what} records a problem with no code; a hold is named or it is not a hold",
    )
    return codes


def _refused_page_row(
    context, index: _PageReadRecords, ordinal: int, page_id: str
) -> dict[str, Any]:
    """The row of a page the Exemplar refused, proven from its one `not-run` reading.

    The Exemplar's refusal is that page's account; nothing was read from it, so
    no accounting or act record may name it.
    """
    what = f"refused page {ordinal} ({page_id})"
    for kind in (page_path.ACT_REGION_KIND, page_path.PERLECTIO_KIND):
        _require(
            not index.on_page(kind, page_id),
            f"{what} has no sealed pixels, yet the Perlector published a {kind} for it",
        )
    _require(
        not index.by_subject(page_path.PAGE_ACCOUNTING_KIND, page_id),
        f"{what} has no sealed pixels, yet the Perlector published a page-accounting for it",
    )
    reading = _one(
        index.by_subject(page_path.PAGE_READING_KIND, page_id),
        f"{what}'s page reading",
        page_path.page_reading_attempt(page_id, page_edges.FIRST_READING),
    )
    payload = _payload_of(reading)
    exemplar_ref = context.artifact_ref(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    _require(
        payload.get("schema") == page_path.PAGE_READING_SCHEMA
        and payload.get("page_id") == page_id
        and payload.get("page_ordinal") == ordinal
        and payload.get("parse_state") == page_path.NOT_RUN
        and payload.get("disposition") == page_path.HELD
        and reading.get("outcome") == page_path.HELD
        and all(
            payload.get(name) is None
            for name in (
                "feed_ref",
                "request_digest",
                "engine_call",
                "sampling",
                "capacity",
                "finish_reason",
                "stop_reason",
                "answer",
            )
        )
        and _problem_codes(payload.get("problems"), f"{what}'s page reading")
        == [page_path.PAGE_NOT_SEALED]
        and reading.get("inputs") == [exemplar_ref],
        f"{what}'s page reading is not the not-run reading of that refused page under this "
        "run; only a not-run reading can stand for a page with no sealed pixels",
    )
    return {
        "act_id": None,
        "act_key": f"p{ordinal}:refused",
        "page_id": page_id,
        "page_ordinal": ordinal,
        "n": None,
        "kind": None,
        "class": PAGE_REFUSED_CLASS,
        "disposition": PAGE_REFUSED_DISPOSITION,
        "region_ref": None,
        "reading_ref": index.ref(reading),
        "perlectio_ref": None,
        "accounting_ref": None,
        "hold_codes": [],
        "flag_codes": [],
        "continues_from_previous_page": None,
        "continues_to_next_page": None,
        "reading_attempt": None,
        "reading_n": None,
    }


def _verify_page_reading(
    context, index: _PageReadRecords, ordinal: int, page_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One sealed page's readings, accountings and act records, recomputed from the evidence.

    The first reading (attempt 1) and its accounting are required. The re-ask
    (attempt 2) and the accounting of both readings exist exactly when
    `page_reask.reask_plan`, run again over the verified first reading and
    accounting, names ids. Each operator re-read (attempt 3 on, without a gap)
    is verified like a first reading, with its own accounting, and must answer
    stored page re-asks of this page and supersede every earlier reading
    (`_verify_rereads`); the last is the page's current reading, whose entries
    are counted. A stray or missing attempt is refused.
    """
    what = f"page {ordinal} ({page_id})"
    readings = index.by_subject(page_path.PAGE_READING_KIND, page_id)
    if not readings:
        raise FatalAccounting(
            f"sealed {what} has no page reading; a sealed page nobody read is never counted "
            "as zero acts"
        )
    accountings = index.by_subject(page_path.PAGE_ACCOUNTING_KIND, page_id)
    _require(bool(accountings), f"{what}'s page accounting is missing; its reading is not proven")
    # An operator re-read is a reading at attempt 3 on that names the decisions it
    # answers; every other reading is the first or the re-ask, counted 1..N.
    reread_records = [
        record
        for record in readings
        if page_path.is_operator_reread(_payload_of(record).get("attempt_ordinal"))
        and page_path.OPERATOR_REREAD_FIELD in _payload_of(record)
    ]
    reading_by = attempt_ordinals(
        [record for record in readings if record not in reread_records],
        f"{what}'s page reading",
        operation=page_path.PAGE_READ_OPERATION,
    )
    rereads_by = _reread_ordinals(reread_records, f"{what}'s operator re-read", page_id)
    rereads = sorted(rereads_by)
    reread_accountings = {
        page_path.page_reading_attempt(page_id, reading): reading for reading in rereads
    }
    accounting_attempts = {
        page_path.page_reading_attempt(page_id, reading): reading
        for reading in page_path.READING_ORDINALS
    }
    accounting_by = attempt_ordinals(
        [record for record in accountings if record.get("attempt_id") not in reread_accountings],
        f"{what}'s page accounting",
        operation=page_path.PAGE_READ_OPERATION,
        ordinal_of=lambda record: accounting_attempts.get(record.get("attempt_id")),
    )
    reread_accounting_list = [
        record for record in accountings if record.get("attempt_id") in reread_accountings
    ]
    reread_accounting_by = {
        reread_accountings[record["attempt_id"]]: record for record in reread_accounting_list
    }
    _require(
        sorted(reread_accounting_by) == rereads and len(reread_accounting_list) == len(rereads),
        f"{what}'s operator re-reads {rereads} are not each accounted exactly once",
    )
    reading = reading_by[page_edges.FIRST_READING]
    payload = _payload_of(reading)
    _require_page_reading(what, reading, page_id, ordinal, page_edges.FIRST_READING)
    _require(
        payload.get("reask") is None,
        f"{what}'s first page reading records a re-ask; only attempt 2 is one",
    )
    _require(
        payload.get("audit") == index.audit,
        f"{what}'s page reading does not record the sealed Pass-C audit policy as not run",
    )
    codes = _problem_codes(payload.get("problems"), f"{what}'s page reading")
    reading_ref = index.ref(reading)
    feed_ref = payload.get("feed_ref")
    _require(
        isinstance(feed_ref, dict) and feed_ref in reading.get("inputs", []),
        f"{what}'s page reading names no feed among its inputs",
    )
    try:
        feed_record = context.tree.read_artifact_reference(
            feed_ref, stage=PERLECTOR, kind=page_path.PAGE_FEED_KIND, subject_id=page_id
        )
    except ContractError as error:
        raise FatalAccounting(f"{what}'s page feed does not verify: {error}") from error
    witnesses = _verify_feed(context, index, what, ordinal, page_id, feed_record)
    feed = _payload_of(feed_record)
    _verify_reply(context, index, what, ordinal, page_id, payload, feed)
    _verify_request(context, what, reading, payload, feed)
    _verify_disposition(what, payload)
    plans = _entry_plans(index, what, payload, feed, page_id)
    first_accounting = accounting_by.get(page_edges.FIRST_READING)
    _require(first_accounting is not None, f"{what}'s first page accounting is missing")
    measure = partial(
        _verify_accounting, context, index, feed=feed, feed_ref=feed_ref, witnesses=witnesses
    )
    trigger = measure(what, first_accounting, reading=payload, reading_ref=reading_ref, plans=plans)
    try:
        named = page_reask.reask_plan(
            payload, trigger, feed, budget=index.reask_budget, policy=index.accounting_policy
        )
    except (ContractError, KeyError, TypeError) as error:
        raise FatalAccounting(f"{what}'s re-ask cannot be planned again: {error}") from error
    expected = list(page_path.READING_ORDINALS if named else (page_edges.FIRST_READING,))
    _require(
        sorted(reading_by) == expected and sorted(accounting_by) == expected,
        f"{what} carries page reading attempts {sorted(reading_by)} and accounting attempts "
        f"{sorted(accounting_by)}, but its first reading and accounting plan "
        f"{'one re-ask' if named else 'no re-ask'} under the sealed page_level_reread, so "
        f"attempts {expected}; a stray, missing or further attempt is never counted",
    )
    trigger_ref = index.ref(first_accounting)
    by_reading = {page_edges.FIRST_READING: (payload, reading_ref)}
    accounting_ref, act_plans, reask_ref = trigger_ref, plans, None
    page_holds, page_flags = trigger["holds"], trigger["flags"]
    if named:
        second, reask_ref, reask_plans = _verify_reask(
            context,
            index,
            f"{what}'s re-ask",
            reading_by[page_edges.REASK_READING],
            feed=feed,
            first=payload,
            refs=(feed_ref, reading_ref, trigger_ref),
            named=named,
            first_count=len(plans),
        )
        last = accounting_by[page_edges.REASK_READING]
        combined = measure(
            f"{what}'s re-ask",
            last,
            reading=payload,
            reading_ref=reading_ref,
            plans=plans,
            reask={
                "reading": second,
                "reading_ref": reask_ref,
                "plans": reask_plans,
                "named": page_reask.named_ids(named),
            },
        )
        _require(
            combined["entries"][: len(trigger["entries"])] == trigger["entries"],
            f"{what}'s re-ask accounting does not restate its first reading's entries exactly",
        )
        act_plans = page_path.reask_act_plans(combined, plans, reask_plans, what)
        accounting_ref = index.ref(last)
        page_holds, page_flags = combined["holds"], combined["flags"]
        by_reading[page_edges.REASK_READING] = (second, reask_ref)
    superseded: list[dict[str, str]] = []
    if rereads:
        superseded = [reading_ref, *([reask_ref] if named else [])]
        current = _verify_rereads(
            context,
            index,
            what,
            {o: (rereads_by[o], reread_accounting_by[o]) for o in rereads},
            feed=feed,
            feed_ref=feed_ref,
            supersedes=superseded,
            counted=act_plans,
            measure=measure,
        )
        payload, reading_ref, act_plans, last, page_holds, page_flags, superseded = current
        codes = _problem_codes(payload.get("problems"), f"{what}'s current page reading")
        accounting_ref, reask_ref, named = index.ref(last), None, None
        by_reading = {payload["attempt_ordinal"]: (payload, reading_ref)}
    # As the Perlector did, over every entry the page publishes, before its records.
    page_path.hold_doubtful_page(act_plans, index.accounting_policy)
    row = {
        "page_id": page_id,
        "page_ordinal": ordinal,
        "parse_state": payload["parse_state"],
        "disposition": payload["disposition"],
        "finish_reason": payload.get("finish_reason"),
        "reading_ref": reading_ref,
        "feed_ref": feed_ref,
        "accounting_ref": accounting_ref,
        "reask_ref": reask_ref,
        "trigger_accounting_ref": trigger_ref if named else None,
        "entry_count": len(act_plans) if payload["disposition"] == page_path.READ else None,
    }
    refs = {"reading_ref": reading_ref, "accounting_ref": accounting_ref, "feed_ref": feed_ref}
    # An earlier reading's act records stay as read, superseded and not counted.
    old = {reference["relative_path"] for reference in superseded}
    if not act_plans:
        _require(
            not _current_on_page(index, page_path.ACT_REGION_KIND, page_id, old)
            and not _current_on_page(index, page_path.PERLECTIO_KIND, page_id, old),
            f"{what} has no act entry to count, yet the Perlector published act records for it",
        )
        return row, [
            _page_row(context, ordinal, page_id, payload, codes, page_holds, page_flags, refs)
        ]
    acts = _verify_entries(
        context,
        index,
        ordinal,
        page_id,
        by_reading,
        act_plans,
        refs,
        page_holds,
        feed,
        witnesses,
        page_flags=page_flags,
        superseded=old,
    )
    return row, acts


def _current_on_page(
    index: _PageReadRecords, kind: str, page_id: str, superseded: set[str]
) -> list[dict[str, Any]]:
    """A page's act records of `kind` but those of a reading an operator re-read superseded."""
    return [
        record
        for record in index.on_page(kind, page_id)
        if (_payload_of(record).get("page_reading_ref") or {}).get("relative_path")
        not in superseded
    ]


def _reread_ordinals(
    records: list[dict[str, Any]], what: str, page_id: str
) -> dict[int, dict[str, Any]]:
    """A page's operator re-reads by ordinal, which must run from 3 without a gap.

    Each record's attempt id must derive from its page and ordinal, and no
    ordinal may repeat: a lost re-read is never passed over.
    """
    found: dict[int, dict[str, Any]] = {}
    for record in records:
        ordinal = _payload_of(record)["attempt_ordinal"]
        if ordinal in found:
            raise FatalAccounting(f"{what} carries attempt {ordinal} twice")
        if record.get("attempt_id") != page_path.page_reading_attempt(page_id, ordinal):
            raise FatalAccounting(
                f"{what} {record.get('artifact_id')!r} claims attempt {ordinal}, which its "
                "sealed attempt identity does not bind"
            )
        found[ordinal] = record
    first = page_edges.OPERATOR_REREAD_FIRST
    _require(
        sorted(found) == list(range(first, first + len(found))),
        f"{what}s {sorted(found)} do not run from {first} without a gap; a lost re-read is "
        "never passed over",
    )
    return found


def _verify_rereads(
    context,
    index: _PageReadRecords,
    what: str,
    rereads: Mapping[int, tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    feed: Mapping[str, Any],
    feed_ref: dict[str, str],
    supersedes: list[dict[str, str]],
    counted: list[dict[str, Any]],
    measure,
) -> tuple[
    Mapping[str, Any],
    dict[str, str],
    list[dict[str, Any]],
    Mapping[str, Any],
    list[str],
    list[dict[str, str]],
]:
    """A page's operator re-reads, each proven like a first reading, in attempt order.

    Each must answer stored page re-asks of this page that no other re-read
    answers, supersede exactly the page's earlier readings, input them and its
    decisions, and be the first reading's request over the same feed; its own
    accounting is measured again. Returns the last one's payload, reference,
    entry plans, accounting record and page holds: the page's current reading,
    and every reading it supersedes, earlier re-reads included. `counted` is the
    entry plans the page counted before its first re-read; each re-read is
    planned against those, or the last re-read's that kept every act it replaced
    (`page_path.keeps_counted`), as stage 4 plans it.
    """
    ordinal, page_id = feed["page_ordinal"], feed["page_id"]
    answered: set[str] = set()
    current = None
    supersedes = list(supersedes)
    for attempt, (reading, accounting) in sorted(rereads.items()):
        reading_what = f"{what}'s operator re-read {attempt}"
        payload = _payload_of(reading)
        _require_page_reading(reading_what, reading, page_id, ordinal, attempt)
        hashes = page_path.require_operator_reread(
            payload.get(page_path.OPERATOR_REREAD_FIELD),
            run_id=context.tree.run_id,
            page_id=page_id,
            stored=index.decisions,
            supersedes=supersedes,
            what=reading_what,
        )
        _require(
            not answered & set(hashes),
            f"{reading_what} answers a decision an earlier re-read of the page answered",
        )
        answered |= set(hashes)
        inputs = reading.get("inputs", [])
        approvals = [
            item["approval_ref"] for item in payload[page_path.OPERATOR_REREAD_FIELD]["decisions"]
        ]
        _require(
            payload.get("reask") is None
            and payload.get("audit") == index.audit
            and payload.get("feed_ref") == feed_ref
            and all(reference in inputs for reference in (feed_ref, *supersedes, *approvals)),
            f"{reading_what} is not a whole-page reading of the page's feed that inputs the "
            "readings it supersedes and the decisions it answers",
        )
        _verify_reply(context, index, reading_what, ordinal, page_id, payload, feed)
        _verify_request(context, reading_what, reading, payload, feed)
        _verify_disposition(reading_what, payload)
        plans = _entry_plans(
            index, reading_what, payload, feed, page_id, attempt=attempt, superseded=counted
        )
        if page_path.keeps_counted(plans):
            counted = plans
        reference = index.ref(reading)
        measured = measure(
            reading_what,
            accounting,
            reading=payload,
            reading_ref=reference,
            plans=plans,
            attempt=attempt,
        )
        current = (
            payload,
            reference,
            plans,
            accounting,
            measured["holds"],
            measured["flags"],
            list(supersedes),
        )
        supersedes.append(reference)
    _require(current is not None, f"{what} has no operator re-read to stand on")
    return current


def _require_page_reading(
    what: str, reading: Mapping[str, Any], page_id: str, ordinal: int, attempt: int
) -> None:
    """A page reading is the page path's, of this page, at this attempt, with a known state."""
    payload = _payload_of(reading)
    _require(
        payload.get("schema") == page_path.PAGE_READING_SCHEMA
        and payload.get("page_id") == page_id
        and payload.get("page_ordinal") == ordinal
        and payload.get("attempt_ordinal") == attempt
        and payload.get("disposition") in READING_DISPOSITIONS
        and payload.get("parse_state") in page_accounting.PARSE_STATES
        and reading.get("outcome")
        == page_path.reading_outcome(payload["parse_state"], payload["disposition"]),
        f"{what}'s page reading is not a page-path reading of this page under this run",
    )


def _verify_reask(
    context,
    index: _PageReadRecords,
    what: str,
    reading: Mapping[str, Any],
    *,
    feed: Mapping[str, Any],
    first: Mapping[str, Any],
    refs: tuple[dict[str, str], dict[str, str], dict[str, str]],
    named: list[dict[str, Any]],
    first_count: int,
) -> tuple[Mapping[str, Any], dict[str, str], list[dict[str, Any]]]:
    """A page's re-ask, proven from the first reading and accounting that planned it.

    Its `reask` must be the one the plan, the first reading's entries and the
    re-ask prompt builder give over the feed (`page_path.reask_record`), its
    reply is read again against the named ids, and its request, capacity and
    engine call are the re-ask's. Returns its payload, reference and entry
    plans, numbered on after the first reading's `first_count` entries.
    """
    feed_ref, first_ref, trigger_ref = refs
    ordinal, page_id = feed["page_ordinal"], feed["page_id"]
    payload = _payload_of(reading)
    _require_page_reading(what, reading, page_id, ordinal, page_edges.REASK_READING)
    _problem_codes(payload.get("problems"), f"{what}'s page reading")
    inputs = reading.get("inputs", [])
    _require(
        payload.get("feed_ref") == feed_ref
        and all(reference in inputs for reference in (feed_ref, first_ref, trigger_ref)),
        f"{what} does not input the feed, first reading and accounting it was planned from",
    )
    chair = context.registry.resolve(PERLECTOR_CHAIR)
    _require(isinstance(chair, ChairIdentity), f"{what} was asked, yet the chair is absent")
    shown = page_reask.render_reask(first["answer"], named)
    try:
        expected = page_path.reask_record(
            reading_ref=first_ref,
            accounting_ref=trigger_ref,
            shown=shown,
            budget=index.reask_budget,
            serving_recipe=chair.serving_recipe,
            feed=feed,
        )
    except (ContractError, KeyError, TypeError) as error:
        raise FatalAccounting(f"{what}'s prompt cannot be built again: {error}") from error
    _require(
        payload.get("reask") == expected,
        f"{what} records another re-ask (trigger records, named ids, prior entries, budget or "
        "prompt) than its first reading and accounting plan",
    )
    identifiers = page_reask.named_ids(named)
    _verify_reply(context, index, what, ordinal, page_id, payload, feed, named=identifiers)
    _verify_request(context, what, reading, payload, feed, shown=shown)
    _verify_disposition(what, payload)
    plans = _entry_plans(
        index, what, payload, feed, page_id, named=identifiers, first_count=first_count
    )
    return payload, index.ref(reading), plans


def _entry_plans(
    index: _PageReadRecords,
    what: str,
    payload: Mapping[str, Any],
    feed: Mapping[str, Any],
    page_id: str,
    *,
    named: list[str] | None = None,
    first_count: int = 0,
    attempt: int | None = None,
    superseded: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The entry plans of a read answer, a re-ask's with its named ids; one not read has none.

    `attempt` names an operator re-read's ordinal, planned against `superseded`, the
    plans the page counted before it; otherwise it is the first reading's, or the
    re-ask's when `named` is given.
    """
    if payload["disposition"] != page_path.READ:
        return []
    try:
        return page_path.entry_plans(
            payload["answer"],
            feed,
            page_id=page_id,
            stop_reason=payload.get("stop_reason"),
            truncation_policy=index.truncation_policy,
            accounting_policy=index.accounting_policy,
            attempt=attempt
            if attempt is not None
            else page_edges.FIRST_READING
            if named is None
            else page_edges.REASK_READING,
            named=named,
            first_count=first_count,
            superseded=superseded,
        )
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise FatalAccounting(
            f"{what}'s answer entries cannot be planned against its feed: {error!r}"
        ) from error


def _refs_by_path(references: Any, what: str) -> list[dict[str, str]]:
    """`page_path.refs_by_path`, refusing inputs that are not a list of path references."""
    _require(
        isinstance(references, list)
        and all(
            isinstance(reference, Mapping) and isinstance(reference.get("relative_path"), str)
            for reference in references
        ),
        f"{what} names inputs that are not a list of path references",
    )
    return page_path.refs_by_path(references)


# The fields a reading derives from what the engine said, or from why it was not asked.
_REPLY_FIELDS: Final = (
    "parse_state",
    "answer",
    "problems",
    "finish_reason",
    "stop_reason",
    page_path.ANSWER_REPAIRS_FIELD,
)
_REPLY_STATES: Final = frozenset(
    {page_path.PARSED, page_path.MALFORMED, page_path.CUT_OFF, page_path.REPETITION_LOOP}
)


def _verify_reply(
    context,
    index: _PageReadRecords,
    what: str,
    ordinal: int,
    page_id: str,
    payload: Mapping[str, Any],
    feed: Mapping[str, Any],
    *,
    named: list[str] | None = None,
) -> None:
    """The reading's parse state, answer, problems, finish and repair, derived again, never trusted.

    A fixture run's operator re-read is answered with the page's declared
    answer, as its first reading is.

    A reply is read again with stage 4's own reader (`page_path.read_reply`),
    a re-ask's against its `named` ids: a live reading's from the response
    bytes its `engine_call` names, a fixture run's from the fixture's
    declared page answer or re-ask answer. A page not asked has exactly the
    reasons `page_path.not_run_problems` gives from its feed, the roster and
    its witnesses; a re-ask is always asked. A page the engine could not take
    or answer records only that, with no call and no answer.
    """
    state = payload["parse_state"]
    engine_call = payload.get("engine_call")
    failure = payload.get("failure")
    replayed = replay_of(context.run) is not None
    _require(
        named is None or state != page_path.NOT_RUN or replayed,
        f"{what} is recorded as not run; a planned re-ask is always asked",
    )
    if state == page_path.NOT_RUN:
        chair = context.registry.resolve(PERLECTOR_CHAIR)
        derived: dict[str, Any] = {
            "parse_state": state,
            "answer": None,
            # A replay asks a re-ask only when its source run sent that very request.
            "problems": [not_replayed_problem(context.run)]
            if named is not None
            else page_path.not_run_problems(
                feed,
                chair_present=isinstance(chair, ChairIdentity),
                no_testimony=not index.testimonia.get(page_id),
            ),
            "finish_reason": None,
            "stop_reason": None,
            page_path.ANSWER_REPAIRS_FIELD: None,
        }
        _require(
            engine_call is None and failure is None and payload.get("request_digest") is None,
            f"{what}'s reading was not asked, yet it names a request, a call or a failure",
        )
    elif state in (page_path.REFUSED_CAPACITY, page_path.CALL_FAILED):
        refused = state == page_path.REFUSED_CAPACITY
        _require(
            engine_call is None
            and (failure is None if refused else isinstance(failure, Mapping))
            and _problem_codes(payload["problems"], f"{what}'s page reading")
            == [page_path.REFUSED_CAPACITY if refused else failure.get("code")],
            f"{what}'s reading is {state!r}, but does not record exactly that, with no call and "
            "no answer",
        )
        derived = {
            "parse_state": state,
            "answer": None,
            "problems": payload["problems"]
            if refused
            else [{"code": failure.get("code"), "detail": failure.get("detail")}],
            "finish_reason": None,
            "stop_reason": None,
            page_path.ANSWER_REPAIRS_FIELD: None,
        }
    else:
        _require(
            state in _REPLY_STATES and failure is None,
            f"{what}'s reading is {state!r}, not a state a page reading records",
        )
        try:
            if engine_call is not None:
                reply = page_path.retained_reply(
                    context.tree.read_bytes, engine_call, serving_reader(context, what)
                )
                _require(
                    reply["finish_reason"] == engine_call.get("finish_reason"),
                    f"{what}'s engine_call names a finish its retained response does not give",
                )
                content, finish, stop = (
                    reply["content"],
                    reply["finish_reason"],
                    reply["stop_reason"],
                )
            else:
                _require(
                    not is_real_ingress(context.run),
                    f"{what}'s reading of a real page names no engine call to read its reply from",
                )
                row = (
                    page_path.fixture_page_answer(context, ordinal)
                    if named is None
                    else page_path.fixture_reask_answer(context, ordinal, planned=True)
                )
                content, finish = row["answer"], row.get("stop_reason", "stop")
                stop = finish
            parse_state, answer, problems, repairs = page_path.read_reply(
                content, stop, feed, index.accounting_policy, named
            )
        except (ContractError, KeyError, TypeError, ValueError, OSError) as error:
            raise FatalAccounting(f"{what}'s reply cannot be read again: {error}") from error
        derived = {
            "parse_state": parse_state,
            "answer": answer,
            "problems": problems,
            "finish_reason": finish,
            "stop_reason": stop,
            # Present only when the one repair was made (`page_answer`).
            page_path.ANSWER_REPAIRS_FIELD: repairs or None,
        }
    mismatched = sorted(name for name in _REPLY_FIELDS if payload.get(name) != derived[name])
    _require(
        not mismatched,
        f"{what}'s reading is not what its reply gives ({', '.join(mismatched)}): the answer and "
        "its problems are read again, never taken from the record",
    )


def _verify_request(
    context,
    what: str,
    reading: Mapping[str, Any],
    payload: Mapping[str, Any],
    feed: Mapping[str, Any],
    *,
    shown: Mapping[str, Any] | None = None,
) -> None:
    """The request a reading records, and the engine call it names, built again from the feed.

    A page that was asked records the digest of its request, the prompt stage
    4's builder renders from the feed (a re-ask's, from the feed and `shown`,
    `page_reask.render_reask`'s) and the images the feed names. A live
    page records its capacity, and that is exactly what the sealed serving row
    (at the tier the record names) admits or refuses for this request under the
    sealed page answer cap. A live answer's `engine_call` names a call record
    and response the reading inputs, and that call is this request: its bytes
    rebuilt from the prompt and images carry the call's own digest, its images,
    capacity, receipt, model and sampling are the reading's, and its sampling is
    the Perlector's sealed row with the receipt's seed.
    """
    state = payload["parse_state"]
    capacity = payload.get("capacity")
    engine_call = payload.get("engine_call")
    if state == page_path.NOT_RUN:
        return
    chair = context.registry.resolve(PERLECTOR_CHAIR)
    _require(
        isinstance(chair, ChairIdentity),
        f"{what}'s reading was asked, yet this run's Perlector chair is absent",
    )
    try:
        text = (
            page_path.request_text(chair.serving_recipe, feed)
            if shown is None
            else page_path.reask_request_text(chair.serving_recipe, feed, shown)
        )
        image_sha256s = page_path.request_image_sha256s(feed)
    except (ContractError, KeyError, TypeError) as error:
        raise FatalAccounting(f"{what}'s request cannot be built again: {error}") from error
    refused = state == page_path.REFUSED_CAPACITY
    _require(
        payload.get("request_digest")
        == (None if refused else page_path.request_digest(text, image_sha256s)),
        f"{what}'s reading records a request digest its feed's prompt and images do not give",
    )
    if capacity is None:
        _require(
            not refused and engine_call is None,
            f"{what}'s reading was refused or answered live, yet records no capacity",
        )
        return
    _verify_capacity(context, what, chair, payload, feed, text, shown)
    if engine_call is not None:
        _verify_engine_call(context, what, chair, reading, payload, feed, text, image_sha256s)


def serving_reader(context, what: str) -> ServingReader:
    """The context's `ServingReader`, or a refusal: a live call is read again, never trusted."""
    reader = context.serving_reader
    _require(
        reader is not None,
        f"{what}'s reading was asked live, but this stage opened its context with no serving "
        "reader, so its call cannot be read again",
    )
    return reader


def _verify_capacity(context, what, chair, payload, feed, text, shown) -> None:
    """A live reading's capacity is what its sealed serving row gives for this request.

    A re-ask (`shown` not `None`) is admitted as stage 4 admits it
    (`page_path.reask_request_capacity`), its answer reserved on its named units.
    """
    from common.request_capacity import RequestCapacityRefusal

    reader = serving_reader(context, what)
    capacity = payload["capacity"]
    record = capacity.get("capacity") if isinstance(capacity, Mapping) else None
    tier = record.get("tier") if isinstance(record, Mapping) else None
    try:
        row = reader.serving_row(context, chair, tier)
        policy, _digest = sealed_decoding_policy(context)
        expected: Any = (
            page_path.request_capacity(
                row, chair.serving_recipe, feed, text, perlector_page_generation(policy)
            )
            if shown is None
            else page_path.reask_request_capacity(
                row, chair.serving_recipe, feed, shown, text, perlector_page_generation(policy)
            )
        )
        problems = None
    except RequestCapacityRefusal as refusal:
        if refusal.capacity is None:
            raise FatalAccounting(f"{what}'s request cannot be measured: {refusal}") from refusal
        expected = {"capacity": refusal.capacity, "answer_reserve": None, "max_tokens": None}
        problems = [{"code": page_path.REFUSED_CAPACITY, "detail": str(refusal)}]
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise FatalAccounting(f"{what}'s request cannot be measured again: {error}") from error
    refused = payload["parse_state"] == page_path.REFUSED_CAPACITY
    _require(
        capacity == expected
        and (problems is not None) == refused
        and (not refused or payload["problems"] == problems),
        f"{what}'s reading records a capacity its sealed serving row does not give for its "
        "request: whether a page fitted is measured again, never taken from the record",
    )


def _verify_engine_call(context, what, chair, reading, payload, feed, text, image_sha256s) -> None:
    """A live answer's call record is this page's request, answered under the sealed row."""
    from common.contracts.envelope import read_verified
    from common.perlector_audit import decode_recorded_generation

    reader = serving_reader(context, what)
    engine_call = payload["engine_call"]
    inputs = reading.get("inputs", [])
    _require(
        engine_call.get("raw_response_ref") in inputs
        and engine_call.get("call_record_ref") in inputs,
        f"{what}'s reading does not input the response and call record its engine_call names",
    )
    provenance = payload.get("provenance")
    try:
        call = json.loads(
            read_verified(
                context.tree.read_bytes, engine_call["call_record_ref"], "a page call record"
            )
        )
        _require(isinstance(call, dict), f"{what}'s call record is not a JSON object")
        verify_retained_call_sampling(context, call, PERLECTOR_CHAIR)
        generation = decode_recorded_generation(call.get("generation_sent"))
        body = reader.request_bytes(
            {
                **generation,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            *(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,"
                                        + base64.b64encode(image).decode("ascii")
                                    },
                                }
                                for image in page_path.request_images(feed, context.tree.read_bytes)
                            ),
                            {"type": "text", "text": text},
                        ],
                    }
                ],
            },
            model_id=engine_call["served_model_id"],
            seed=generation.get("seed"),
            stream=True,
        )
        policy, _digest = sealed_decoding_policy(context)
        stream = call.get("stream")
        guard = perlector_loop_guard(policy)
    except FatalAccounting:
        raise
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise FatalAccounting(f"{what}'s call record cannot be read again: {error}") from error
    _require(
        call.get("schema") == CHAIR_STREAM_CALL_RECORD_SCHEMA
        and isinstance(stream, Mapping)
        and stream.get("loop_guard") == guard
        and call.get("chair") == PERLECTOR_CHAIR
        and call.get("kind") == "chat-completions"
        and call.get("request_sha256") == digest_bytes(body)
        and call.get("image_sha256s") == image_sha256s
        and call.get("raw_response_ref") == engine_call["raw_response_ref"]
        and call.get("served_model_id") == engine_call["served_model_id"]
        and call.get("response_model") == engine_call["served_model_id"]
        and call.get("capacity") == payload["capacity"]["capacity"]
        and generation.get("max_tokens") == payload["capacity"]["max_tokens"]
        and isinstance(provenance, Mapping)
        and call.get("receipt_ref") == provenance.get("receipt_ref")
        and payload.get("sampling") == page_path.page_sampling(policy, chair.role),
        f"{what}'s engine_call is not this page's request: its call record's request, images, "
        "capacity, receipt, model, sampling or repetition-loop guard is not what the feed and "
        "the sealed rows give",
    )


def _verify_disposition(what: str, payload: Mapping[str, Any]) -> None:
    """A reading is `read` exactly when its answer parsed and nothing holds it whole."""
    expected = (
        page_path.READ
        if payload["parse_state"] == page_path.PARSED and not payload["problems"]
        else page_path.HELD
    )
    _require(
        payload["disposition"] == expected,
        f"{what}'s reading says {payload['disposition']!r}, but its answer against its feed "
        f"is {expected!r}",
    )


def _verify_accounting(
    context,
    index: _PageReadRecords,
    what: str,
    accounting: Mapping[str, Any],
    *,
    feed: Mapping[str, Any],
    feed_ref: dict[str, str],
    witnesses: list[dict[str, Any]],
    reading: Mapping[str, Any],
    reading_ref: dict[str, str],
    plans: list[dict[str, Any]],
    reask: Mapping[str, Any] | None = None,
    attempt: int = page_edges.FIRST_READING,
) -> dict[str, Any]:
    """The page accounting measured again must be exactly the sealed one; return it.

    With `reask` (`{reading, reading_ref, plans, named}`) it is the re-ask's,
    measuring both readings together, as `page_path.accounting_inputs` takes it.
    """
    recomputed, inputs = _measure_page_accounting(
        context, index, what, feed, feed_ref, witnesses, reading, reading_ref, plans, reask, attempt
    )
    _require(
        _refs_by_path(accounting.get("inputs"), f"{what}'s page accounting")
        == _refs_by_path(inputs, f"{what}'s recomputed page accounting")
        and _payload_of(accounting) == recomputed
        and accounting.get("outcome")
        == (page_path.HELD if recomputed["holds"] else page_path.READ),
        f"{what}'s page accounting is not what its sealed inputs measure: the page's holds "
        "are recomputed, never taken from the record",
    )
    return recomputed


def _measure_page_accounting(
    context,
    index: _PageReadRecords,
    what: str,
    feed: Mapping[str, Any],
    feed_ref: dict[str, str],
    witnesses: list[dict[str, Any]],
    reading: Mapping[str, Any],
    reading_ref: dict[str, str],
    plans: list[dict[str, Any]],
    reask: Mapping[str, Any] | None = None,
    attempt: int = page_edges.FIRST_READING,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """The page accounting of one reading, or of it and its re-ask, and its inputs.

    Every input is read as stage 4 read it (`page_path.accounting_inputs`):
    the feed (built again by `_verify_feed`), the reading and its entry plans,
    each page witness's current page Testimonium (`witnesses`, as the feed
    took them), the Designator's Surya and detector records and the Ink Map's
    runs, under the sealed policy. Rule (e)'s alignment is bounded by the
    policy's sealed work budget, counted rather than timed, so the same inputs
    measure the same way here as in stage 4.
    """
    try:
        measured, inputs = page_path.accounting_inputs(
            context,
            feed=feed,
            feed_ref=feed_ref,
            reading=reading,
            reading_ref=reading_ref,
            witnesses=witnesses,
            plans=plans,
            surya_census=index.surya_census,
            designator_entries=index.designator_entries,
            ink_entries=index.ink_entries,
            record_detector_configured=index.record_detector_configured,
            fixture_placeholders=index.fixture_placeholders,
            reask=reask,
            attempt=attempt,
        )
        recomputed = page_accounting.page_accounting(**measured, policy=index.accounting_policy)
    except ContractError as error:
        raise FatalAccounting(
            f"{what}'s page accounting cannot be measured again: {error}"
        ) from error
    return recomputed, inputs


def _verify_feed(
    context,
    index: _PageReadRecords,
    what: str,
    ordinal: int,
    page_id: str,
    feed_record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The page feed built again from the sealed inputs stage 4 built it from.

    Built by stage 4's own builder (`page_path.page_feed_of`) from the sealed
    page, protocol, witness roster, each chair's current page Testimonium, the
    Surya census and the Perlector chair; its page render must be the bytes
    stage 4 retained. The sealed feed, `feed_digest` included, and the feed
    record's inputs must be exactly these. Returns the page witnesses, as the
    feed took them, for the accounting.
    """
    chair = context.registry.resolve(PERLECTOR_CHAIR)
    try:
        _page, page_bytes = read_sealed_page(context.tree, page_id)
        feed, witnesses, inputs = page_path.page_feed_of(
            context,
            page_id=page_id,
            ordinal=ordinal,
            page_size=dimensions(page_bytes),
            protocol_config=index.protocol,
            page_chairs=page_path.declared_page_witness_chairs(context),
            current=index.testimonia.get(page_id, []),
            surya_census=index.surya_census,
            serving_recipe=chair.serving_recipe if isinstance(chair, ChairIdentity) else None,
            fixture_placeholders=index.fixture_placeholders,
            retain=_already_retained(context, PERLECTOR),
        )
    except (ContractError, OSError) as error:
        raise FatalAccounting(f"{what}'s page feed cannot be built again: {error}") from error
    _require(
        _payload_of(feed_record) == feed
        and _refs_by_path(feed_record.get("inputs"), f"{what}'s page feed")
        == page_path.refs_by_path(inputs),
        f"{what}'s page feed is not the feed its sealed inputs build: what the reading was "
        "shown is rebuilt, never taken from the record",
    )
    return witnesses


def _already_retained(context, stage: str):
    """A `retain` for a rebuilt record: the bytes must already be `stage`'s blob."""

    def retained(data: bytes, label: str = "a blob") -> dict[str, str]:
        digest = digest_bytes(data)
        reference = {"relative_path": context.tree.blob_path(stage, digest), "sha256": digest}
        if context.input_ref(reference["relative_path"]) != reference:
            raise SchemaRefusal(f"{label} rebuilt from the sealed evidence is not {stage}'s blob")
        return reference

    return retained


def _page_row(
    context,
    ordinal: int,
    page_id: str,
    payload: Mapping[str, Any],
    problem_codes: list[str],
    page_holds: list[str],
    page_flags: list[str],
    refs: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """The one row that stands for a page with no act entry, over the page rectangle."""
    unread = payload["disposition"] == page_path.HELD
    act_class = PAGE_UNREAD_CLASS if unread else PAGE_BLANK_CLASS
    codes = {PAGE_UNREAD_HOLD, *problem_codes} if unread else {PAGE_BLANK_HOLD}
    rectangle = _sealed_page_rectangle(context, page_id, ordinal, f"page {ordinal}")
    return {
        "act_id": derive_act_id(page_id, act_class, rectangle),
        "act_key": f"p{ordinal}:{'unread' if unread else 'blank'}",
        "page_id": page_id,
        "page_ordinal": ordinal,
        "n": None,
        "kind": "act",
        "class": act_class,
        "disposition": page_path.HELD,
        "region_ref": None,
        "reading_ref": refs["reading_ref"],
        "perlectio_ref": None,
        "accounting_ref": refs["accounting_ref"],
        "hold_codes": sorted(codes | set(page_holds)),
        "flag_codes": sorted(set(page_flags)),
        "continues_from_previous_page": None,
        "continues_to_next_page": None,
        "reading_attempt": None,
        "reading_n": None,
    }


# The act-region fields that name its crop: proven by the region's lineage when
# it is placed, and all `None` when it is not.
_REGION_CROP_FIELDS: Final = frozenset(
    {"region_id", "image_path", "image_sha256", "transform", "transform_digest"}
)


def _verify_entries(
    context,
    index: _PageReadRecords,
    ordinal: int,
    page_id: str,
    readings: Mapping[int, tuple[Mapping[str, Any], dict[str, str]]],
    plans: list[dict[str, Any]],
    refs: dict[str, dict[str, str]],
    page_holds: list[str],
    feed: Mapping[str, Any],
    witnesses: list[dict[str, Any]],
    *,
    page_flags: list[str] = (),
    superseded: set[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Each entry the page's last accounting counts, proven against its act-region and Perlectio.

    `readings` maps each reading's ordinal to its payload and reference: an
    entry names and repeats the reading it came from, the first, the re-ask or
    the current operator re-read. The act records of a reading in `superseded`
    (run-tree paths) stay as read and are not counted.
    """
    what = f"page {ordinal} ({page_id})"
    _require(
        [plan["n"] for plan in plans] == list(range(1, len(plans) + 1)),
        f"{what}'s readings do not number their entries 1..{len(plans)}",
    )
    # A page naming no act is held until the Recensor confirms nothing on it is one.
    no_act = {NO_ACT_ON_PAGE_HOLD} if all(p["act"]["kind"] != "act" for p in plans) else set()
    feed_ref = refs["feed_ref"]
    dissent_budget = sealed_dissent_budget(context)
    rows = []
    for plan in plans:
        act, act_id, union = plan["act"], plan["act_id"], plan["union_box_px"]
        n = plan["n"]
        reading, reading_ref = readings[plan["reading_attempt"]]
        attempt = page_path.page_reading_attempt(page_id, plan["reading_attempt"])
        entry_what = f"{what}'s entry {n} ({act_id})"
        region = _one(
            index.by_subject(page_path.ACT_REGION_KIND, act_id),
            f"{entry_what} act-region",
            page_path.region_attempt(act_id),
        )
        region_payload = _payload_of(region)
        region_holds = plan["region_holds"]
        expected_region = {
            "schema": page_path.ACT_REGION_SCHEMA,
            "page_id": page_id,
            "page_ordinal": ordinal,
            "n": n,
            "kind": act["kind"],
            "label": act.get("label"),
            "cites": act["cites"],
            "cited_ids": plan["cited_ids"],
            "act_class": plan["act_class"],
            "page_reading_attempt": attempt,
            "region_boxes_px": plan["region_boxes_px"],
            "union_box_px": union,
            "page_reading_ref": reading_ref,
            "page_accounting_ref": refs["accounting_ref"],
            "feed_ref": feed_ref,
            "holds": region_holds,
            "page_holds": page_holds,
            **page_path.recovered_fields(plan),
        }
        mismatched = sorted(
            name for name, value in expected_region.items() if region_payload.get(name) != value
        )
        _require(
            set(region_payload) == set(expected_region) | _REGION_CROP_FIELDS,
            f"{entry_what} act-region carries fields other than its closed schema",
        )
        _require(
            not mismatched,
            f"{entry_what} act-region does not match its answer entry and page records "
            f"({', '.join(mismatched)})",
        )
        _require(
            region.get("outcome")
            == (page_path.HELD if region_holds or page_holds else page_path.READ),
            f"{entry_what} act-region's outcome does not follow its holds",
        )
        if union is None:
            _require(
                all(region_payload.get(name) is None for name in _REGION_CROP_FIELDS),
                f"{entry_what} is unplaced, yet its act-region names a crop",
            )
        else:
            try:
                verify_reading_region_lineage(context.tree, context.run, region)
            except ContractError as error:
                raise FatalAccounting(
                    f"{entry_what} act-region does not trace to the Exemplar: {error}"
                ) from error
        region_ref = index.ref(region)
        perlectio = _one(
            index.by_subject(page_path.PERLECTIO_KIND, act_id),
            f"{entry_what} Perlectio",
            page_path.perlectio_attempt(act_id),
        )
        perlectio_payload = _payload_of(perlectio)
        reading_holds = plan["reading_holds"]
        try:
            expected_perlectio = page_path.expected_perlectio(
                page_id=page_id,
                ordinal=ordinal,
                plan=plan,
                refs={
                    "act_region_ref": region_ref,
                    "page_reading_ref": reading_ref,
                    "page_accounting_ref": refs["accounting_ref"],
                    "feed_ref": feed_ref,
                },
                page_holds=page_holds,
                reading=reading,
            )
        except KeyError as error:
            raise FatalAccounting(
                f"{entry_what} page reading carries no {error} for its Perlectio to repeat"
            ) from error
        mismatched = sorted(
            name
            for name, value in expected_perlectio.items()
            if perlectio_payload.get(name) != value
        )
        _require(
            set(perlectio_payload) == set(expected_perlectio) | {"dissent"},
            f"{entry_what} Perlectio carries fields other than its closed schema",
        )
        _require(
            not mismatched,
            f"{entry_what} Perlectio does not match its act-region, answer entry and page "
            f"records ({', '.join(mismatched)})",
        )
        _require(
            perlectio.get("outcome")
            == (page_path.HELD if reading_holds or page_holds else page_path.READ),
            f"{entry_what} Perlectio's outcome does not follow its holds",
        )
        try:
            dissent_holds = page_path.dissent_holds(
                perlectio_payload.get("dissent"),
                plan["text"],
                feed,
                plan["cited_ids"],
                witnesses,
                dissent_budget,
            )
        except ContractError as error:
            raise FatalAccounting(f"{entry_what} dissent cannot be computed: {error}") from error
        _require(
            dissent_holds,
            f"{entry_what} Perlectio's dissent is not where its reading departs from the "
            "witness units it cites",
        )
        hold_codes = sorted(set(reading_holds) | set(page_holds) | no_act)
        rows.append(
            {
                "act_id": act_id,
                "act_key": f"p{ordinal}:{n}",
                "page_id": page_id,
                "page_ordinal": ordinal,
                "n": n,
                "kind": act["kind"],
                "class": plan["act_class"],
                "disposition": page_path.HELD if hold_codes else page_path.READ,
                "region_ref": region_ref,
                "reading_ref": reading_ref,
                "perlectio_ref": index.ref(perlectio),
                "accounting_ref": refs["accounting_ref"],
                "hold_codes": hold_codes,
                # The page's review flags: measured, recorded, holding nothing.
                "flag_codes": sorted(set(page_flags)),
                "continues_from_previous_page": act["continues_from_previous_page"],
                "continues_to_next_page": act["continues_to_next_page"],
                "reading_attempt": plan["reading_attempt"],
                "reading_n": plan["reading_n"],
            }
        )
    expected_ids = sorted(plan["act_id"] for plan in plans)
    for kind in (page_path.ACT_REGION_KIND, page_path.PERLECTIO_KIND):
        found = sorted(
            record.get("subject_id")
            for record in _current_on_page(index, kind, page_id, superseded)
        )
        _require(
            found == expected_ids,
            f"{what}'s {kind} records do not match the {len(plans)} entries its last "
            "accounting counts",
        )
    return rows


def is_real_ingress(run: Mapping[str, Any]) -> bool:
    """Whether a run authority names the real route.

    An absent ingress record means synthetic (older test trees lack it).
    """
    return "ingress" in run and parse_ingress_record(run["ingress"]) == REAL_INGRESS


def refuse_unlive_real_reading(
    context: StageContext,
    chair: ChairIdentity | AbsentChair,
    serving_mode: str,
    *,
    stage: str = "Perlector",
) -> None:
    """Refuse a non-live serving row on a real submission, before anything is published.

    The Perlector and the Coniector read the synthetic fixture's declared answers when
    their chair's sealed row is not live. A real submission has no declarations, and
    a declared answer cannot stand in for a model's reply to real ink. A fixture run,
    a live row and an absent chair, which reads nothing, all pass.
    """
    if serving_mode == "live" or not is_real_ingress(context.run) or isinstance(chair, AbsentChair):
        return
    raise ContractError(
        f"the {stage} cannot read a real submission from declared fixture answers: the "
        f"sealed serving-recipe row for chair {chair.role!r} is not a live row, and a "
        "declared answer cannot stand in for a model's reply to real ink. Start a new run "
        f"sealed under a catalogue whose {stage} row is live; a sealed run's catalogue "
        "cannot be changed"
    )


def sealed_decoding_policy(context: StageContext) -> tuple[dict[str, Any], str]:
    """The run's decoding policy, refused unless it is the one the run sealed."""
    policy, digest = load_decoding_policy(context.args.decoding_config)
    context.require_sealed_config("decoding", digest)
    return policy, digest


def verify_retained_call_sampling(
    context: StageContext,
    call: Mapping[str, Any],
    chair: str,
    *,
    attempt_ordinal: int = 1,
    sends_seed: bool = True,
) -> None:
    """Hold one retained call record to its chair's sealed decoding row and seed.

    The seed the call must have sent is none when `sends_seed` is false (a
    Chandra native request), and otherwise its serving receipt's. For a reader that holds only
    the stage context and the parsed record; raises `ContractError`.
    """
    policy, _digest = sealed_decoding_policy(context)
    expected_seed: int | None
    if not sends_seed:
        expected_seed = None
    else:
        receipt_ref = call.get("receipt_ref")
        if not isinstance(receipt_ref, Mapping):
            raise ContractError(f"a {chair} call record names no serving receipt")
        expected_seed = context.tree.read_run_receipt(dict(receipt_ref)).get("seed")
    verify_call_sampling(
        call, policy, chair, attempt_ordinal=attempt_ordinal, expected_seed=expected_seed
    )


def open_context(
    args,
    stage: str,
    *,
    registry_factory: Callable[[str], StageChairProtocol] = ChairRegistry.from_toml,
    tree: RunTree | None = None,
    run: Mapping[str, Any] | None = None,
    serving_reader: ServingReader | None = None,
) -> StageContext:
    """Open an existing run for a stage that is not the first to write.

    `tree` and `run` come together or not at all, so the route and the binding
    check use one read of `run.json`. `serving_reader` is the stage's
    `ServingReader`, for a stage that verifies a page-read run's live calls.
    """
    if (tree is None) != (run is None):
        raise ContractError(
            "open_context takes the run tree and its read authority together or neither; "
            "a binding checked against a second read of run.json can straddle a rewrite"
        )
    fixture = load_fixture(args.fixture_root)
    scenario_for(fixture, args.scenario)
    registry = _open_registry(args, registry_factory)
    bindings = run_config_bindings(
        registry.config,
        fixture,
        args.scenario,
        pdf_render_config_path=args.pdf_render_config,
        designator_geometry_config_path=args.designator_geometry_config,
        alignment_config_path=args.alignment_config,
        page_accounting_config_path=args.page_accounting_config,
        ink_map_config_path=args.ink_map_config,
        reconstruction_config_path=args.reconstruction_config,
        pdf_target_dpi=args.pdf_target_dpi,
        armarium_formats_config_path=args.formats_config,
        recovery_config_path=args.recovery_config,
        hard_failure_config_path=args.hard_failure_config,
        review_config_path=args.review_config,
        witness_context=args.witness_context,
        perlector_protocol_config_path=args.perlector_protocol_config,
        perlector_audit_config_path=args.perlector_audit_config,
        mechanics_qualification=getattr(args, "mechanics_qualification", False),
        serving_recipes_config_path=args.serving_recipes_config,
        decoding_config_path=args.decoding_config,
    )
    if tree is None:
        tree = RunTree(Path(args.run_root), args.run_id)
        run = tree.read_run()
    refuse_imported_stage(run, stage)
    verify_snapshot_is_current(run, args.corpus_register)
    read_snapshot(tree, run)
    # Compared separately: an equal `config_digest` proves the bytes, not that
    # they are filed under the names the run recorded.
    if SEALED_CONFIG_DIGESTS_FIELD in run:
        require_seal_method(run, f"run {args.run_id!r}")
    fields = ("config_digest", "adapter_recipes", "witness_chairs", SEALED_CONFIG_DIGESTS_FIELD)
    differing = [
        field
        for field in fields
        # A run authority older than the map is not called changed here.
        if (field != SEALED_CONFIG_DIGESTS_FIELD or field in run)
        and run.get(field) != bindings[field]
    ]
    if differing:
        # Name the policies that moved, not just the field holding them.
        named = ", ".join(differing)
        if SEALED_CONFIG_DIGESTS_FIELD in differing and isinstance(
            run.get(SEALED_CONFIG_DIGESTS_FIELD), Mapping
        ):
            sealed_before = run[SEALED_CONFIG_DIGESTS_FIELD]
            sealed_now = bindings[SEALED_CONFIG_DIGESTS_FIELD]
            moved = sorted(
                name
                for name in set(sealed_before) | set(sealed_now)
                if sealed_before.get(name) != sealed_now.get(name)
            )
            if moved:
                named += f" (sealed configuration {', '.join(moved)} moved)"
        raise IncompatibleReuse(
            f"run {args.run_id!r} is bound to different {named} than "
            "the currently loaded run inputs. No stage work was written. Resume with "
            "the original sealed inputs, or start a new run for the changed inputs"
        )
    predecessor = SEAL_PREDECESSORS.get(stage)
    predecessor_manifest = (
        tree.build_manifest(predecessor, verify_inputs=False) if predecessor is not None else None
    )
    verify_predecessor_seal(tree, stage, manifest=predecessor_manifest)
    refuse_halted_run(
        tree, stage, args.hard_failure_config, predecessor_manifest=predecessor_manifest
    )
    return _bound_context(
        tree, run, fixture, args.scenario, stage, args, registry, bindings, serving_reader
    )


def open_stage_context(
    args,
    stage: str,
    *,
    registry_factory: Callable[[str], StageChairProtocol] = ChairRegistry.from_toml,
    serving_reader: ServingReader | None = None,
) -> StageContext:
    """Open an existing run for any stage after the Door, on either ingress route.

    One read of `run.json` decides the route and is passed down, never re-read.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    if not is_real_ingress(run):
        return open_context(
            args,
            stage,
            registry_factory=registry_factory,
            tree=tree,
            run=run,
            serving_reader=serving_reader,
        )
    return _open_real_context(args, stage, tree, run, registry_factory, serving_reader)


def _open_real_context(
    args,
    stage: str,
    tree: RunTree,
    run: Mapping[str, Any],
    registry_factory: Callable[[str], StageChairProtocol],
    serving_reader: ServingReader | None,
) -> StageContext:
    """Open a real submission's run for a stage after the Door.

    Mirrors `open_context`'s checks in the same order.  The real
    `config_digest` is not recomputed: it binds the Door's inputs and decoder
    versions, and re-binding those would refuse a sound run after a library
    upgrade.  The sealed map is rechecked name by name instead.
    """
    refuse_imported_stage(run, stage)
    verify_snapshot_is_current(run, args.corpus_register)
    read_snapshot(tree, run)
    registry = _open_registry(args, registry_factory)
    bindings = real_run_bindings(registry.config, args)
    # Before the seal check, so a moved policy is named as one.
    _refuse_incompatible_real_reuse(run, bindings, run_id=args.run_id)
    predecessor = SEAL_PREDECESSORS.get(stage)
    predecessor_manifest = (
        tree.build_manifest(predecessor, verify_inputs=False) if predecessor is not None else None
    )
    verify_predecessor_seal(tree, stage, manifest=predecessor_manifest)
    refuse_halted_run(
        tree, stage, args.hard_failure_config, predecessor_manifest=predecessor_manifest
    )
    return _bound_context(
        tree, run, None, REAL_SCENARIO, stage, args, registry, bindings, serving_reader
    )


def _open_registry(args, registry_factory: Callable[..., StageChairProtocol]) -> StageChairProtocol:
    cache_root = getattr(args, "cache_root", None)
    store_root = getattr(args, "store_root", None)
    if store_root is not None:
        from common.chairs.model_store import StoreRoleFetcher

        return registry_factory(
            args.models_config, cache_root=cache_root, fetcher=StoreRoleFetcher(store_root)
        )
    if cache_root is None:
        return registry_factory(args.models_config)
    return registry_factory(args.models_config, cache_root=cache_root)


def _bound_context(
    tree: RunTree,
    run: Mapping[str, Any],
    fixture: dict[str, Any] | None,
    scenario: str,
    stage: str,
    args,
    registry: StageChairProtocol,
    bindings: Mapping[str, Any],
    serving_reader: ServingReader | None,
) -> StageContext:
    """A context over the bindings just checked; the adapter recipe comes from `run.json` only."""
    return StageContext(
        tree=tree,
        run=run,
        fixture=fixture,
        scenario=scenario,
        stage=stage,
        adapter_revision=adapter_recipe_for(run, stage),
        args=args,
        registry=registry,
        sealed_config_digests=bindings["sealed_config_digests"],
        armarium_formats=bindings["armarium_formats"],
        serving_config_inputs=bindings["serving_config_inputs"],
        recovery_policy=bindings["recovery_policy"],
        serving_reader=serving_reader,
    )


def _refuse_incompatible_real_reuse(
    run: Mapping[str, Any], bindings: Mapping[str, Any], *, run_id: str
) -> None:
    """Refuse a real run resumed under inputs other than the ones it sealed.

    Absent and changed names are reported apart: one needs a new run, the
    other the original file.
    """
    differing: list[str] = []
    if list(run.get("witness_chairs", [])) != sorted(bindings["witness_chairs"]):
        differing.append("witness_chairs")
    if run.get("adapter_recipes") != bindings["adapter_recipes"]:
        differing.append("adapter_recipes")
    sealed = run_sealed_config_digests(run)
    moved: list[str] = []
    absent: list[str] = []
    for name, digest in sorted(bindings["sealed_config_digests"].items()):
        recorded = sealed.get(name)
        if recorded is None:
            absent.append(name)
        elif recorded != digest:
            moved.append(name)
    absent.extend(name for name in _REAL_DOOR_ONLY_SEALED_NAMES if name not in sealed)
    if not differing and not moved and not absent:
        return
    # One sentence per fault, so each reads correctly on its own.
    named: list[str] = []
    if differing:
        named.append(
            f"run {run_id!r} is bound to different {', '.join(differing)} than the currently "
            "loaded run inputs"
        )
    if moved:
        named.append(f"run {run_id!r} sealed configuration {', '.join(moved)} moved")
    if absent:
        named.append(
            f"run {run_id!r} sealed no digest for the {', '.join(absent)} configuration, so a "
            "stage cannot prove which bytes it is bound to"
        )
    raise IncompatibleReuse(
        ". ".join(named) + ". No stage work was written. Resume with the original sealed "
        "inputs, or start a new run for the changed inputs"
    )


def submission_identity(run: Mapping[str, Any]) -> str | None:
    """The one identity a real submission carries: its filename ledger's self-hash.

    `None` on the fixture route, whose fixture id is a different concept.
    """
    if not is_real_ingress(run):
        return None
    rows = run.get("source_manifest")
    if not isinstance(rows, list) or not rows:
        raise ContractError("run.json has no submitted source manifest to name a submission by")
    canaries = canary_ordinals(run)
    hashes = {
        row.get("ledger_sha256") if isinstance(row, Mapping) else None
        for row in rows
        if not isinstance(row, Mapping) or row.get("ordinal") not in canaries
    }
    if len(hashes) != 1:
        raise ContractError(
            f"run.json source rows name {len(hashes)} filename ledgers, not one; a real "
            "submission is one ledger and its identity cannot be chosen among several"
        )
    identity = next(iter(hashes))
    if not is_sha256(identity):
        raise ContractError(
            "run.json source rows carry no filename-ledger sha256; a real submission's "
            "identity is that ledger's self-hash and nothing may stand in for it"
        )
    return identity


def canary_ordinals(run: Mapping[str, Any]) -> set[int]:
    """Canary membership comes only from the ledger digest sealed by the Door."""
    digests = run.get("sealed_config_digests", {})
    mark = digests.get("canary-ledger") if isinstance(digests, Mapping) else None
    if mark is None:
        return set()
    if not is_sha256(mark):
        raise ContractError("run.json has a malformed sealed canary-ledger digest")
    rows = run.get("source_manifest")
    if not isinstance(rows, list):
        raise ContractError("run.json has no source rows for its sealed canary ledger")
    ordinals = {
        row.get("ordinal")
        for row in rows
        if isinstance(row, Mapping) and row.get("ledger_sha256") == mark
    }
    if not ordinals or any(not is_plain_int(ordinal) or ordinal < 1 for ordinal in ordinals):
        raise ContractError("run.json sealed a canary ledger with no valid page ordinals")
    return ordinals


def real_page_entries(run: Mapping[str, Any], entries: Iterable[Mapping[str, Any]]) -> list:
    """The entries on the run's real pages: every one not on a canary page.

    Canary pages are controls, so an entry on one is never read beside a real
    page's: never a side of a page break, its evidence, a reconstruction or its
    context.
    """
    canaries = canary_ordinals(run)
    return [entry for entry in entries if entry["page_ordinal"] not in canaries]


def real_pages(run: Mapping[str, Any], pages: Mapping[int, Any]) -> dict[int, Any]:
    """`pages`, keyed by page ordinal, without the canary pages (`real_page_entries`)."""
    canaries = canary_ordinals(run)
    return {ordinal: page for ordinal, page in pages.items() if ordinal not in canaries}


def exemplar_page_ids(context) -> dict[int, str]:
    """Every Exemplar page by submitted ordinal, sealed and refused alike.

    Read from the Exemplar's `page` artifacts, the only source that carries a
    container page's full identity.  Says which page an ordinal names, not
    that its bytes are sound. Every submitted ordinal has exactly one page, so
    no submitted page is silently absent from what a stage reads or counts.
    """
    submitted = {
        row.get("ordinal")
        for row in context.run.get("source_manifest", [])
        if isinstance(row, Mapping)
    }
    pages: dict[int, str] = {}
    for entry in context.tree.build_manifest(EXEMPLAR)["artifacts"]:
        if entry["kind"] != "page":
            continue
        page = context.tree.read_artifact(EXEMPLAR, "page", entry["artifact_id"])
        payload = page.get("payload")
        ordinal = payload.get("ordinal") if isinstance(payload, Mapping) else None
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                f"Exemplar page {page.get('artifact_id')!r} has no integer ordinal, so it "
                "cannot be matched to one submitted source"
            )
        if ordinal in pages:
            raise FatalAccounting(
                f"more than one Exemplar page names ordinal {ordinal}; one submitted source "
                "is one page, and nothing may decide which of two pages it became"
            )
        if ordinal not in submitted:
            raise FatalAccounting(
                f"Exemplar page {page['subject_id']!r} names ordinal {ordinal}, which the run "
                "authority's source manifest never submitted"
            )
        cited = {
            row.get("ordinal")
            for row in payload.get("submission_rows", [])
            if isinstance(row, Mapping)
        } - {ordinal}
        if cited:
            raise FatalAccounting(
                f"Exemplar page {page['subject_id']!r} for ordinal {ordinal} also cites "
                f"submitted ordinal(s) {sorted(cited)}; one page per ordinal is the contract, "
                "and those ordinals would otherwise leave this index silently"
            )
        pages[ordinal] = page["subject_id"]
    missing = submitted - set(pages)
    if missing:
        raise FatalAccounting(
            f"submitted page ordinal(s) {sorted(missing, key=str)} have no Exemplar page; a "
            "submitted page is never silently absent"
        )
    return dict(sorted(pages.items()))


def refuse_halted_run(
    tree: RunTree,
    stage: str,
    hard_failure_config_path: str | Path,
    *,
    predecessor_manifest: dict[str, Any] | None = None,
) -> None:
    """Apply the sealed run-level cap when no orchestrator guards stage entry."""
    run = tree.read_run()
    policy = load_hard_failure_policy(hard_failure_config_path)
    require_sealed_config(run_sealed_config_digests(run), "hard-failure", policy["config_sha256"])
    # Inputs are not verified here, so lineage damage is reported by the
    # boundary that owns it.
    predecessor = SEAL_PREDECESSORS.get(stage)
    manifests = (
        {predecessor: predecessor_manifest}
        if predecessor is not None and predecessor_manifest is not None
        else None
    )
    tally = tally_hard_failures(tree, policy, verify_inputs=False, manifests=manifests)
    if tally["breached"]:
        raise RunHalted(
            f"{stage} refuses to start: {tally['count']} hard failure(s) exceed the run-level "
            f"cap of {tally['threshold']}; no stage writes after a halted run"
        )


def run_stage(main) -> int:
    """Run a stage's main and turn a contract refusal into an honest exit code."""
    try:
        return int(main() or EXIT_COMPLETE)
    except RunHalted as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_RUN_HALTED
    except ContractError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FATAL


def latest_attempt(records: list[dict[str, Any]], what: str, *, operation: str) -> dict[str, Any]:
    """The current record for a subject: the latest attempt, with its honest status.

    The one place "current" is derived, over `attempt_ordinals`' checked run 1..N.
    """
    ordinals = attempt_ordinals(records, what, operation=operation)
    return ordinals[max(ordinals)]


def attempt_ordinals(
    records: list[dict[str, Any]],
    what: str,
    *,
    operation: str,
    ordinal_of: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[int, dict[str, Any]]:
    """A subject's records by attempt ordinal, which must run 1..N without a gap.

    A missing ordinal is fatal, never 0: a default 0 lets listing order pick
    the current record. `ordinal_of` reads a record's ordinal (its payload's
    `attempt_ordinal` by default), and `operation` lets the attempt id be
    re-derived from subject and ordinal: the envelope does not bind the
    payload's ordinal, so a forged high ordinal would otherwise become
    current. Ordinals must run 1..N; a gap is a lost attempt.
    """
    if not records:
        raise FatalAccounting(f"no {what} to derive a current outcome from")
    if ordinal_of is None:
        ordinal_of = lambda record: record.get("payload", {}).get("attempt_ordinal")  # noqa: E731
    ordinals: dict[int, dict[str, Any]] = {}
    for record in records:
        ordinal = ordinal_of(record)
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                f"a {what} artifact carries no attempt ordinal, so which attempt is "
                "current cannot be derived. A guess here silently picks a stale record"
            )
        if ordinal in ordinals:
            raise FatalAccounting(
                f"{what} carries duplicate attempt ordinal {ordinal} in artifacts "
                f"{ordinals[ordinal].get('artifact_id', '<unknown>')!r} and "
                f"{record.get('artifact_id')!r}; a tie is not a latest attempt and may not be "
                "selected"
            )
        subject = record.get("subject_id")
        expected = attempt_id(subject, operation, ordinal) if isinstance(subject, str) else None
        if record.get("attempt_id") != expected:
            raise FatalAccounting(
                f"a {what} artifact claims attempt ordinal {ordinal} in its payload but its "
                f"sealed attempt identity {record.get('attempt_id')!r} does not derive from "
                f"({subject!r}, {operation!r}, {ordinal}). The ordinal decides which record is "
                "current, so an ordinal the identity does not bind is a reading nobody sealed"
            )
        ordinals[ordinal] = record
    if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
        raise FatalAccounting(
            f"{what} carries attempt ordinals {sorted(ordinals)}, which is not the contiguous "
            "run 1.. that append-only attempts produce; a gap is an attempt that is no longer "
            "here, and nothing is lost silently"
        )
    return ordinals


def latest_per_chair(records: list[dict[str, Any]], what: str) -> list[dict[str, Any]]:
    """One record per chair: each chair's own latest attempt, honest status kept.

    Attempts are append-only per (act, chair), so consumers must collapse each
    chair's history before treating the group as evidence.
    """
    by_chair: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        chair = record.get("payload", {}).get("chair")
        if not isinstance(chair, str) or not chair:
            raise FatalAccounting(f"a {what} artifact carries no chair to group its attempts by")
        by_chair.setdefault(chair, []).append(record)
    return [
        latest_attempt(group, f"{what} from chair {chair}", operation=f"read:{chair}")
        for chair, group in sorted(by_chair.items())
    ]
