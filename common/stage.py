"""What every stage program needs, and nothing a stage should decide for itself.

Stages are programs that exchange versioned artifacts on disk, so this module
holds only shared plumbing (arguments, opening the run tree, publishing with a
correct envelope) and no pipeline logic: logic here would let one stage import
another through a side door.  The fixture is read as TOML data, not imported.
"""

import argparse
import hashlib
import json
import os
import platform
import stat
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Protocol

from common import fixture_identity
from common.alignment import DEFAULT_ALIGNMENT_CONFIG_PATH, load_alignment_limits
from common.armarium_formats import (
    DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    ArmariumFormats,
    bind_armarium_formats,
)
from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig, ServingDetails, is_sha256
from common.chairs.protocol import ChairProtocol
from common.chairs.registry import ChairRegistry
from common.chandra_presentation import (
    STRUCTURE_REQUEST_IMAGE_FIELDS,
    STRUCTURE_REQUEST_IMAGE_KIND,
    STRUCTURE_REQUEST_IMAGE_SCHEMA,
)
from common.contracts.approval import REAL_INGRESS, parse_ingress_record
from common.contracts.canonical import (
    canonical_bytes,
    digest_bytes,
    digest_of,
    is_plain_int,
    verify_self_hash,
)
from common.contracts.envelope import build_envelope, digest_ref, verify_input_bytes
from common.contracts.errors import (
    ContractError,
    FatalAccounting,
    IdentityRefusal,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import PROPOSAL_SEAL_ID, act_bindings, artifact_id, attempt_id
from common.contracts.identities import act_id as derive_act_id
from common.contracts.identities import verify as verify_identity
from common.contracts.outcomes import (
    BOUNDARY_OUTCOMES,
    classify,
)
from common.contracts.outcomes import (
    WITNESS_READING_OUTCOMES as _WITNESS_READING_OUTCOMES,
)
from common.contracts.serving import (
    CHAIR_CALL_RECORD_FIELDS,
    CHAIR_CALL_RECORD_FIELDS_V1,
    CHAIR_CALL_RECORD_SCHEMA,
    CHAIR_CALL_RECORD_SCHEMA_V1,
    CHAIR_CALL_RECORD_SCHEMAS,
    CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
    CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA,
    CHAIR_TRANSPORT_PROBLEM_FIELDS,
    CHAIR_TRANSPORT_PROBLEM_SCHEMA,
    SERVING_CONFIG_INPUTS_FIELDS,
    SERVING_CONFIG_INPUTS_SCHEMA,
)
from common.contracts.stages import (
    ARMARIUM,
    ATTESTATORES,
    DESIGNATOR,
    DOOR,
    EXEMPLAR,
    PERLECTOR,
    RECENSOR,
    SEAL_PREDECESSORS,
    STAGES,
    TRIAGE_MODES,
)
from common.corpus_register import read_snapshot, verify_snapshot_is_current
from common.decoding import (
    DEFAULT_DECODING_CONFIG_PATH,
    load_decoding_policy,
    structure_recovery_policy,
)
from common.exemplar_boundary import verify_sealed_page_pixels
from common.hard_failure import (
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.imaging import dimensions
from common.native_witness import validate_presented, validate_presented_page_binding
from common.recovery import (
    DEFAULT_RECOVERY_CONFIG_PATH,
    RECOVERY_KINDS,
    load_recovery_policy,
    reconcile_recovery_requests,
    recovery_kind_budget,
)
from common.runtree.store import PublishResult, RunTree, _inode_identity
from common.sealed_config import read_sealed_toml, require_seal_method, require_sealed_config
from common.witness_adapters import validate_witness_adapter_bindings
from common.witness_context import validate_witness_context_configuration

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
DEFAULT_WITNESS_CONTEXT_CONFIG_PATH = _CONFIG_DIR / "witness_context.toml"
DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH = _CONFIG_DIR / "perlector_protocol.toml"
DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH = _CONFIG_DIR / "perlector_audit.toml"
# Padding changes the crop bytes a witness sees, so it is sealed into the run.
DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH = _CONFIG_DIR / "designator_padding.toml"
DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH = _CONFIG_DIR / "designator_geometry.toml"
# Grouping thresholds decide which acts exist, so they are sealed too.
DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH = _CONFIG_DIR / "designator_grouping.toml"
DEFAULT_CORPUS_FRAME_CONFIG_PATH = _CONFIG_DIR / "corpus_frame.toml"
DEFAULT_SERVING_RECIPES_CONFIG_PATH = _CONFIG_DIR / "serving_recipes.toml"
DEFAULT_POD_PLACEMENT_CONFIG_PATH = _CONFIG_DIR / "pod_placement.toml"
DEFAULT_TRIAGE_MODES_CONFIG_PATH = _CONFIG_DIR / "triage_modes.toml"

# The run-level blind/named toggle, named once so the CLI, the config digest and
# the Perlectio schema cannot disagree about the closed set.
WITNESS_CONTEXT_REGIMES: Final = ("named", "blinded")
MAX_NUDA_PER_MILLE: Final = 1000
MAX_PERLECTOR_INSTRUMENT_PER_MILLE: Final = 1000
# Experiment identities, not approval evidence: a changed design needs a new
# subject; a changed rate needs a new approval of the resulting `config_digest`.
# Approvals resolve after the run authority exists, so an approval is never part
# of the configuration it approves.
NUDA_APPROVAL_SUBJECT: Final = "lectio-nuda-sampling-design.v1"
PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT: Final = "perlector-prior-draft-instrument-design.v1"

# A constant, never argv: the real `config_digest` binds no scenario, so an argv
# value would be a run-shaping fact nothing checks.
REAL_SCENARIO: Final = "real-submission"

# Bump whenever the real Door's output can change, so a real run cannot resume
# under pixels from another Door.  Lives here because `common/` rechecks it and
# may not import a stage.
REAL_DOOR_ADAPTER_REVISION: Final = "exemplar-door-v5"


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

# Stages that can return EXIT_HELD after sealing whatever the mode.
# `test_advance_modes.py` checks this against the driver's source.
ALWAYS_HELD_BOUNDARIES: Final = frozenset({ATTESTATORES, ARMARIUM})


def _named_boundary(name: str, role: str) -> str:
    """Refuse a selection endpoint that owns no stage completion boundary.

    `recovery` is a legal driver member but has no seal, so it gets the same
    refusal as a typo.
    """

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
        "engine_call",
    }
)

# The structure chair's serving posture, on every structural-pass artifact: the
# one provenance field saying a model was actually asked, since a fixture
# receipt names a chair nothing called.  Per-page calls are named by each page's
# `structure-answer` record.
STRUCTURE_CALL_SCHEMA: Final = "structure-chair-call.v1"
STRUCTURE_CALL_FIELDS: Final = frozenset(
    {"schema", "call_kind", "decoding_policy", "decoding_config_sha256"}
)
STRUCTURE_CALL_KIND: Final = "chat-completions"
# The structure pass may run at a sampled temperature, unlike the witnesses'
# `reading_of_record`, so its section is named explicitly.
STRUCTURE_DECODING_POLICY: Final = "structure"

# Named once for the Designator, which writes them, and the verifier here.
STRUCTURE_ANSWER_KIND: Final = "structure-answer"
STRUCTURE_ANSWER_RECORD_SCHEMA: Final = "designator-structure-answer.v1"
STRUCTURE_ANSWER_RECORD_SCHEMA_V2: Final = "designator-structure-answer.v2"
STRUCTURE_ANSWER_RECORD_SCHEMA_V3: Final = "designator-structure-answer.v3"
STRUCTURE_ANSWER_RECORD_SCHEMAS: Final = frozenset(
    {
        STRUCTURE_ANSWER_RECORD_SCHEMA,
        STRUCTURE_ANSWER_RECORD_SCHEMA_V2,
        STRUCTURE_ANSWER_RECORD_SCHEMA_V3,
    }
)
STRUCTURE_ATTEMPT_KIND: Final = "structure-attempt"
STRUCTURE_ANSWER_PARSED: Final = "parsed"


class StageChairProtocol(ChairProtocol, Protocol):
    """The small additional config surface a calling stage needs.

    ``ChairRegistry`` in production; the test fake implements it separately.
    """

    config: ModelsConfig


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
        """This run's sealed bounded-recovery policy, parsed once at binding.

        Refuses when absent, so a missing budget never reads as zero.
        """
        if self._recovery_policy is None:
            raise ContractError(
                "this context carries no run-sealed recovery policy; a stage may not read "
                "the budget from `config/recovery.toml` itself, because a rewrite between "
                "the run's binding check and that read publishes reviews and requests "
                "under an allowance the run never sealed. Open the run with `open_context`"
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
    def witness_context_config_path(self) -> str:
        return self.args.witness_context_config

    # The four sampling knobs below are read from argv and sealed like `witness_context`.
    @property
    def nuda_per_mille(self) -> int:
        """The Lectio nuda sampling rate, in thousandths."""
        return self.args.nuda_per_mille

    @property
    def nuda_approval_ref(self) -> str:
        """The sampling design this run draws nuda under; empty when nothing is sampled."""
        return self.args.nuda_approval_ref

    @property
    def perlector_instrument_per_mille(self) -> int:
        """The instrumented-reading rate, in thousandths."""
        return self.args.perlector_instrument_per_mille

    @property
    def perlector_instrument_approval_ref(self) -> str:
        return self.args.perlector_instrument_approval_ref

    @property
    def draft_fed(self) -> bool:
        return self.args.draft_fed

    @property
    def perlector_protocol_config_path(self) -> str:
        return self.args.perlector_protocol_config

    @property
    def perlector_audit_config_path(self) -> str:
        return self.args.perlector_audit_config

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
        envelope = build_envelope(
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
        return self.tree.publish_artifact(envelope)

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
        return self._write_serving_blob(audit, "serving launch audit")

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
        try:
            payload = self.tree.read_bytes(reference["relative_path"])
        except OSError as error:
            raise SchemaRefusal(
                f"serving launch audit {reference['relative_path']} could not be read: {error}"
            ) from error
        actual_digest = digest_bytes(payload)
        if actual_digest != reference["sha256"]:
            raise SchemaRefusal(
                f"serving launch audit {reference['relative_path']} has digest {actual_digest}, "
                f"not the reference digest {reference['sha256']}"
            )
        try:
            audit = json.loads(payload.decode("utf-8"))
            canonical = canonical_bytes(audit)
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise SchemaRefusal(
                f"serving launch audit {reference['relative_path']} could not be read: {error}"
            ) from error
        if canonical != payload or not isinstance(audit, dict):
            raise SchemaRefusal("serving launch audit is not a canonical JSON object")
        if audit.get("schema") != "serving-launch-audit.v1":
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
        """Canonical content-addressed storage shared by serving evidence records.

        Refused after the seal, like `publish`: the blob directory is in the
        sealed inventory, and a late write would look like tampering to the
        next consumer.  Receipts need no guard; they live outside any stage.
        """
        if self.sealed:
            raise SchemaRefusal(
                f"{self.stage} has sealed its completion boundary; storing {label} afterwards "
                "would make its witnessed blob inventory false"
            )
        try:
            payload = canonical_bytes(value)
        except (TypeError, ValueError) as error:
            raise SchemaRefusal(f"{label} is not canonical JSON data: {error}") from error
        digest, result = self.tree.put_blob(self.stage, payload)
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
    # `read_run` requires no particular field, so a missing one is refused by
    # name rather than as a KeyError that escapes the exit-code mapping.
    run = tree.read_run()
    missing = [field for field in ("config_digest", "register_digest") if field not in run]
    if missing:
        raise SchemaRefusal(
            f"{stage} cannot seal its boundary: the run authority carries no "
            f"{', '.join(missing)}, so the seal would witness a binding that is not there"
        )
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
                if _is_unpublished_blob_temporary(name):
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


def _is_unpublished_blob_temporary(name: str) -> bool:
    """True only for ``RunTree.put_blob``'s ``.<digest>.tmp-<unique>`` name."""
    if not name.startswith("."):
        return False
    target, separator, unique = name[1:].partition(".tmp-")
    return bool(separator and unique and is_sha256(target))


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
        if not _is_unpublished_blob_temporary(other):
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


def verify_predecessor_seal(tree: RunTree, stage: str) -> None:
    """Refuse a missing, forged, or changed predecessor boundary by name."""
    predecessor = SEAL_PREDECESSORS.get(stage)
    if predecessor is None:
        return
    _verify_stage_seal(tree, predecessor, stage, "predecessor")


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
    previous_environment = _validate_decode_environment(
        environment.get("payload"), f"{producer} stored"
    )
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
    # The producer's environment, rebuilt here: the reader's own decode work
    # differs by construction.
    current_environment = _validate_decode_environment(
        _decode_environment(producer), f"{reader} current for {producer}"
    )
    differences = _decode_difference(previous_environment, current_environment)
    if differences:
        # Reported, not refused: no rule yet says when a difference is fatal.
        print(
            f"decode environment differs by name from {producer}: {differences}",
            file=sys.stderr,
        )


def _decode_difference(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Every decode-environment field that differs, by name."""
    changes = []
    previous_decoders = {row["name"]: row["version"] for row in previous["decoders"]}
    current_decoders = {row["name"]: row["version"] for row in current["decoders"]}
    for name in sorted(set(previous_decoders) | set(current_decoders)):
        if previous_decoders.get(name) != current_decoders.get(name):
            changes.append(name)
    for field in ("platform", "machine", "decode_paths_used", "produced_pixels"):
        if previous.get(field) != current.get(field):
            changes.append(field)
    return changes


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


class _StageArgumentParser(argparse.ArgumentParser):
    """Shared operation-argument refusal for stage programs.

    ``--chair`` is shared argv so orchestration passes one shape, but only the
    Attestatores implements it; other stages refuse it before touching the run.
    """

    def __init__(self, *args, accepts_chair: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._accepts_chair = accepts_chair

    def parse_args(self, args=None, namespace=None) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        if parsed.chair is not None and not self._accepts_chair:
            raise ContractError(
                "--chair is implemented only by the Attestatores reread operation; "
                "this stage does not accept it"
            )
        return parsed


def stage_parser(description: str, *, accepts_chair: bool = False) -> argparse.ArgumentParser:
    parser = _StageArgumentParser(description=description, accepts_chair=accepts_chair)
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
        help="the sealed decoding posture for record readings and variance experiments",
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
    parser.add_argument("--pdf-render-config", default=str(DEFAULT_PDF_RENDER_CONFIG_PATH))
    parser.add_argument(
        "--designator-padding-config", default=str(DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH)
    )
    parser.add_argument(
        "--designator-geometry-config", default=str(DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH)
    )
    parser.add_argument(
        "--designator-grouping-config",
        default=str(DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH),
        help=(
            "the sealed grouping, structure and conservation thresholds the Designator's "
            "pass resolves per page"
        ),
    )
    parser.add_argument(
        "--perlector-instrument-per-mille",
        type=int,
        default=0,
        help="the sealed prior-draft control rate in thousandths (0 disables the control)",
    )
    parser.add_argument(
        "--perlector-instrument-approval-ref",
        default="",
        help="the project lead's reference for the predeclared prior-draft instrument design",
    )
    parser.add_argument(
        "--perlector-protocol-config",
        default=str(DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH),
        help="the sealed Perlector prior-draft protocol declaration",
    )
    parser.add_argument(
        "--perlector-audit-config",
        default=str(DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH),
        help="the sealed Perlector Pass-C audit declaration",
    )
    parser.add_argument(
        "--draft-fed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="whether Pass B receives the prior draft (default: fed)",
    )
    parser.add_argument("--formats-config", default=str(DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH))
    parser.add_argument("--recovery-config", default=str(DEFAULT_RECOVERY_CONFIG_PATH))
    parser.add_argument("--hard-failure-config", default=str(DEFAULT_HARD_FAILURE_CONFIG_PATH))
    parser.add_argument("--pdf-target-dpi", type=int, default=None)
    parser.add_argument(
        "--witness-context",
        default="named",
        choices=WITNESS_CONTEXT_REGIMES,
        help="the run-level named/blinded toggle a Perlectio's dossier is built under (spec 08)",
    )
    parser.add_argument(
        "--witness-context-config",
        default=str(DEFAULT_WITNESS_CONTEXT_CONFIG_PATH),
        help="the Perlector-owned factual witness-context declaration this run seals",
    )
    parser.add_argument(
        "--nuda-per-mille",
        type=int,
        default=0,
        help="the sealed Lectio nuda sampling rate, in thousandths (0 disables it)",
    )
    parser.add_argument(
        "--nuda-approval-ref",
        default="",
        help=(
            "the project lead's reference for the predeclared Lectio nuda sampling design; "
            "required whenever --nuda-per-mille is not 0"
        ),
    )
    parser.add_argument("--operation", default="initial")
    parser.add_argument(
        "--act", default=None, help="one act id, for a recovery or reread operation"
    )
    parser.add_argument(
        "--recovery-request",
        default=None,
        help="the exact Recensor recovery-request artifact a Designator recrop answers",
    )
    parser.add_argument(
        "--chair", default=None, help="one chair role, for an Attestatores reread operation"
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
            "run's argv. Principle 6 — 'the record itself protects the past' — is "
            "why the receipt records the caps that actually bound the serving "
            "moment (the launch audit's profile.tier) rather than folding this into "
            "the reproducibility contract config_digest exists to protect."
        ),
    )
    return parser


def load_fixture(fixture_root: str) -> dict[str, Any]:
    """Read the declared fixture as data; a missing one is a failure, not an empty run."""
    path = Path(fixture_root) / "skeleton_fixture.toml"
    if not path.exists():
        raise ContractError(
            f"no fixture declaration at {path}. The skeleton runs on declared "
            "synthetic pages only; a run with no input is a failure, not an "
            "empty success"
        )
    with open(path, "rb") as handle:
        fixture = tomllib.load(handle)
    if not fixture.get("page") or not fixture.get("act"):
        raise ContractError(f"{path} declares no pages or no acts")
    if "page_witness_chairs" in fixture:
        raise ContractError(
            f"{path} declares page_witness_chairs, a key retired to the models configuration's "
            "witness_scope. A stale fixture carrying it would be silently ignored rather "
            "than honoured; remove the key so the sealed roster is the only source of scope."
        )
    return fixture


def validate_witness_context_bindings(
    models,
    *,
    witness_context: str,
    witness_context_config_path: str | Path,
    nuda_per_mille: int,
    nuda_approval_ref: str,
    perlector_instrument_per_mille: int,
    perlector_instrument_approval_ref: str,
) -> str:
    """Refuse a bad witness-context binding before a run tree exists, on every path.

    Shared by fixture and real ingress so a real run cannot get as far as the
    Perlector before a defect is caught.  Returns the declaration's sha256.
    """
    if witness_context not in WITNESS_CONTEXT_REGIMES:
        raise ContractError(
            f"witness_context {witness_context!r} is not one of {WITNESS_CONTEXT_REGIMES}"
        )
    _require_sampling_knobs("nuda", nuda_per_mille, MAX_NUDA_PER_MILLE, nuda_approval_ref)
    # A nuda sample needs the project lead's predeclared design, sealed beside
    # the rate so no run can later claim an approval it did not start under.
    if nuda_per_mille and nuda_approval_ref != NUDA_APPROVAL_SUBJECT:
        raise ContractError(
            f"a Lectio nuda rate of {nuda_per_mille}/1000 needs the project lead's predeclared "
            f"sampling design selector {NUDA_APPROVAL_SUBJECT!r} in --nuda-approval-ref; an arbitrary "
            "string is not an approval record"
        )
    _require_sampling_knobs(
        "perlector_instrument",
        perlector_instrument_per_mille,
        MAX_PERLECTOR_INSTRUMENT_PER_MILLE,
        perlector_instrument_approval_ref,
    )
    if (
        perlector_instrument_per_mille
        and perlector_instrument_approval_ref != PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT
    ):
        raise ContractError(
            f"a Perlector prior-draft control rate of {perlector_instrument_per_mille}/1000 "
            "needs the project lead's predeclared sampling design selector "
            f"{PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT!r} in "
            "--perlector-instrument-approval-ref; an arbitrary string is not an approval record"
        )
    validation = validate_witness_context_configuration(
        models,
        witness_context_config_path,
        shipped_config_root=DEFAULT_WITNESS_CONTEXT_CONFIG_PATH.parent,
    )
    return validation.source_sha256


def _require_sampling_knobs(name: str, per_mille: Any, maximum: int, approval_ref: Any) -> None:
    if not is_plain_int(per_mille) or not 0 <= per_mille <= maximum:
        raise ContractError(
            f"{name}_per_mille must be an integer in [0, {maximum}], got {per_mille!r}"
        )
    if not isinstance(approval_ref, str):
        raise ContractError(f"{name}_approval_ref must be a string")


def real_run_policy_digest(
    *,
    witness_context: str,
    witness_context_declaration_sha256: str,
    nuda_per_mille: int,
    nuda_approval_ref: str,
    perlector_instrument_per_mille: int,
    perlector_instrument_approval_ref: str,
    draft_fed: bool,
    mechanics_qualification: bool = False,
) -> str:
    """The digest a real run seals its run-level reading knobs under.

    The real `config_digest` cannot be recomputed downstream, so these argv
    knobs need their own seal or a resume could change them unchecked.  Called
    at creation and at every stage open, so both sides hash the same set.
    """
    if not isinstance(draft_fed, bool):
        raise ContractError(f"draft_fed must be a bool, got {draft_fed!r}")
    if not isinstance(mechanics_qualification, bool):
        raise ContractError(
            f"mechanics_qualification must be a bool, got {mechanics_qualification!r}"
        )
    return digest_of(
        {
            "witness_context_regime": witness_context,
            "witness_context_declaration_sha256": witness_context_declaration_sha256,
            "nuda_per_mille": nuda_per_mille,
            "nuda_approval_ref": nuda_approval_ref,
            "perlector_instrument_per_mille": perlector_instrument_per_mille,
            "perlector_instrument_approval_ref": perlector_instrument_approval_ref,
            "draft_fed": draft_fed,
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
    designator_padding_config_path: str | Path = DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH,
    designator_geometry_config_path: str | Path = DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH,
    designator_grouping_config_path: str | Path = DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH,
    alignment_config_path: str | Path = DEFAULT_ALIGNMENT_CONFIG_PATH,
    pdf_target_dpi: int | None = None,
    armarium_formats_config_path: str | Path = DEFAULT_ARMARIUM_FORMATS_CONFIG_PATH,
    recovery_config_path: str | Path = DEFAULT_RECOVERY_CONFIG_PATH,
    hard_failure_config_path: str | Path = DEFAULT_HARD_FAILURE_CONFIG_PATH,
    witness_context: str = "named",
    witness_context_config_path: str | Path = DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    nuda_per_mille: int = 0,
    nuda_approval_ref: str = "",
    perlector_instrument_per_mille: int = 0,
    perlector_instrument_approval_ref: str = "",
    perlector_protocol_config_path: str | Path = DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH,
    perlector_audit_config_path: str | Path = DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH,
    draft_fed: bool = True,
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
    padding_config_digest = read_sealed_toml(
        designator_padding_config_path, "Designator padding configuration"
    )[1]
    geometry_config_digest = read_sealed_toml(
        designator_geometry_config_path, "Designator geometry configuration"
    )[1]
    # Sealed only: its schema lives in a stage module `common/` may not import,
    # so a malformed file is refused when the Designator loads it.
    grouping_config_digest = read_sealed_toml(
        designator_grouping_config_path, "Designator grouping configuration"
    )[1]
    _, alignment_config_digest = load_alignment_limits(alignment_config_path)
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
    validate_witness_adapter_bindings(models)
    witness_context_config_digest = validate_witness_context_bindings(
        models,
        witness_context=witness_context,
        witness_context_config_path=witness_context_config_path,
        nuda_per_mille=nuda_per_mille,
        nuda_approval_ref=nuda_approval_ref,
        perlector_instrument_per_mille=perlector_instrument_per_mille,
        perlector_instrument_approval_ref=perlector_instrument_approval_ref,
    )
    return {
        "witness_chairs": list(models.witness_chairs),
        "config_digest": digest_of(
            {
                "fixture": fixture,
                "scenario": scenario,
                "models": models.to_record(),
                "pdf_render_config_sha256": pdf_render_config_digest,
                "designator_padding_config_sha256": padding_config_digest,
                "designator_geometry_config_sha256": geometry_config_digest,
                "designator_grouping_config_sha256": grouping_config_digest,
                "alignment_config_sha256": alignment_config_digest,
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
                "witness_context_declaration_sha256": witness_context_config_digest,
                "nuda_per_mille": nuda_per_mille,
                "nuda_approval_ref": nuda_approval_ref,
                "perlector_instrument_per_mille": perlector_instrument_per_mille,
                "perlector_instrument_approval_ref": perlector_instrument_approval_ref,
                "perlector_protocol_config_sha256": perlector_protocol_config_digest,
                "perlector_audit_config_sha256": perlector_audit_config_digest,
                "draft_fed": draft_fed,
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
            "designator-padding": padding_config_digest,
            "designator-geometry": geometry_config_digest,
            "designator-grouping": grouping_config_digest,
            "alignment": alignment_config_digest,
            "corpus-frame-shard": corpus_frame_config_digest,
            "decoding": decoding_config_digest,
            "perlector-protocol": perlector_protocol_config_digest,
            "perlector-audit": perlector_audit_config_digest,
            "pdf-render": pdf_render_config_digest,
            "recovery": recovery_policy["config_sha256"],
            "hard-failure": hard_failure_policy["config_sha256"],
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
    witness_context_declaration_sha256 = validate_witness_context_bindings(
        models,
        witness_context=args.witness_context,
        witness_context_config_path=args.witness_context_config,
        nuda_per_mille=args.nuda_per_mille,
        nuda_approval_ref=args.nuda_approval_ref,
        perlector_instrument_per_mille=args.perlector_instrument_per_mille,
        perlector_instrument_approval_ref=args.perlector_instrument_approval_ref,
    )
    _, alignment_config_digest = load_alignment_limits(args.alignment_config)
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
            "designator-padding": read_sealed_toml(
                args.designator_padding_config, "Designator padding configuration"
            )[1],
            "designator-geometry": read_sealed_toml(
                args.designator_geometry_config, "Designator geometry configuration"
            )[1],
            "designator-grouping": read_sealed_toml(
                args.designator_grouping_config, "Designator grouping configuration"
            )[1],
            "alignment": alignment_config_digest,
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
            "triage-modes": load_triage_modes(DEFAULT_TRIAGE_MODES_CONFIG_PATH),
            "serving-recipes": serving_recipes_config_digest,
            "pod-placement": pod_placement_config_digest,
            "models": models.models_digest,
            "armarium-formats": armarium_formats_digest,
            "run-policy": real_run_policy_digest(
                witness_context=args.witness_context,
                witness_context_declaration_sha256=witness_context_declaration_sha256,
                nuda_per_mille=args.nuda_per_mille,
                nuda_approval_ref=args.nuda_approval_ref,
                perlector_instrument_per_mille=args.perlector_instrument_per_mille,
                perlector_instrument_approval_ref=args.perlector_instrument_approval_ref,
                draft_fed=args.draft_fed,
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
DESIGNATOR_CHAIR = "designator_structure"
PERLECTOR_CHAIR = PERLECTOR

# Absent by default, and named here so the absence is resolved and recorded:
# enabling a real detector must not silently turn every run `partial`.
SECONDARY_PROPOSER_CHAIR = "secondary_proposer"


def unaddressed_chairs(models: ModelsConfig) -> tuple[str, ...]:
    """Configured roles no stage in this pipeline will ever ask for.

    A misspelt role is still valid configuration, and would otherwise be
    silently never asked (principle 2).  Absent chairs, and the base of an
    addressed adapter (recorded in its receipt), count as addressed.
    """
    addressed = set(models.witness_chairs) | {
        DESIGNATOR_CHAIR,
        PERLECTOR_CHAIR,
        SECONDARY_PROPOSER_CHAIR,
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
    # Only the Designator's structure chair records an engine call; anywhere
    # else it claims a serving moment that was not its own (principle 6).
    if provenance.get("engine_call") is not None:
        if producer_stage != DESIGNATOR:
            raise SchemaRefusal(
                "only the Designator's structure pass records an engine call; provenance "
                f"produced by {producer_stage!r} carries one"
            )
        if chair != DESIGNATOR_CHAIR:
            raise SchemaRefusal(
                f"provenance for chair {chair!r} carries a structure-chair engine call; the "
                f"structural pass is served by {DESIGNATOR_CHAIR!r} and by no other chair"
            )
        if state != "configured":
            raise SchemaRefusal(
                f"chair {chair!r} is recorded as {state!r} and still carries an engine call; a "
                "chair that was not configured served nothing"
            )
        if not require_receipt:
            raise SchemaRefusal(
                f"chair {chair!r} carries an engine call but is recorded as not run; a call is a "
                "serving moment, and it owes the receipt that moment was issued under"
            )
        _validate_structure_chair_call(context, provenance["engine_call"])
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


def _validate_structure_chair_call(context: StageContext, call: Any) -> None:
    """The closed record of the posture the structure chair was served under.

    The policy is recorded by name and digest, never by copying the
    temperature, which could then disagree with the sealed bytes.  The digest
    is held to the run's sealed `decoding` entry, so a `config/decoding.toml`
    edited after binding is refused here rather than sealed into a reading.
    """
    if not isinstance(call, Mapping) or set(call) != STRUCTURE_CALL_FIELDS:
        named = sorted(call) if isinstance(call, Mapping) else type(call).__name__
        raise SchemaRefusal(
            "a structure-chair engine call carries exactly its schema, call kind, decoding "
            f"policy name and sealed decoding digest; this one carries {named}"
        )
    if call["schema"] != STRUCTURE_CALL_SCHEMA:
        raise SchemaRefusal(
            f"a structure-chair engine call must declare schema {STRUCTURE_CALL_SCHEMA!r}, not "
            f"{call['schema']!r}"
        )
    if call["call_kind"] != STRUCTURE_CALL_KIND:
        raise SchemaRefusal(
            f"the structure chair is served through {STRUCTURE_CALL_KIND!r}; this call names "
            f"{call['call_kind']!r}"
        )
    if call["decoding_policy"] != STRUCTURE_DECODING_POLICY:
        raise SchemaRefusal(
            f"the structural pass runs under the sealed {STRUCTURE_DECODING_POLICY!r} decoding "
            f"policy; this call names {call['decoding_policy']!r}, which is a posture it did "
            "not run under"
        )
    if not is_sha256(call["decoding_config_sha256"]):
        raise SchemaRefusal(
            "a structure-chair engine call names its sealed decoding digest as a lowercase "
            "SHA-256 value"
        )
    require_sealed_config(
        run_sealed_config_digests(context.run), "decoding", call["decoding_config_sha256"]
    )


def scenario_for(fixture: dict[str, Any], name: str) -> dict[str, Any]:
    """The declared scenario, refused loudly when the fixture does not name it."""
    for scenario in fixture.get("scenario", []):
        if scenario["name"] == name:
            return scenario
    declared = [scenario["name"] for scenario in fixture.get("scenario", [])]
    raise ContractError(f"the fixture declares no scenario {name!r}; declared: {declared}")


_EXPECTED_ACT_NAMES: Final = ("act_id", "act_key", "page_id")
_EXPECTED_ACT_FIELDS: Final = frozenset(
    {*_EXPECTED_ACT_NAMES, "page_ordinal", "has_continuation", "outcome", "evidence"}
)


def expected_acts(context) -> list[dict[str, Any]]:
    """Every act the proposal seal expects, each with a validated Designator outcome.

    The one reader for every consumer; a missing or unknown outcome is fatal,
    never read as marked-out.
    """
    seal = context.tree.read_artifact(
        DESIGNATOR,
        "proposal-seal",
        PROPOSAL_SEAL_ID,
    )
    payload = seal.get("payload")
    if not isinstance(payload, dict) or not verify_self_hash(payload):
        raise FatalAccounting(
            "the Designator proposal seal lacks a valid self-hashed expected-act denominator"
        )
    acts = payload.get("expected_acts")
    count = payload.get("count")
    if not isinstance(acts, list) or not acts:
        raise FatalAccounting("the Designator proposal seal names no expected acts")
    if not is_plain_int(count) or count != len(acts):
        raise FatalAccounting(
            "the Designator proposal seal count does not reconcile with its expected-act rows"
        )
    act_ids: set[str] = set()
    act_keys: set[str] = set()
    for act in acts:
        if not isinstance(act, dict):
            raise FatalAccounting("the Designator proposal seal has a non-object expected-act row")
        if set(act) != _EXPECTED_ACT_FIELDS:
            raise FatalAccounting(
                "the Designator proposal seal expected-act row has fields other than its "
                "closed denominator contract"
            )
        if (
            any(not isinstance(act[name], str) or not act[name] for name in _EXPECTED_ACT_NAMES)
            or not is_plain_int(act["page_ordinal"])
            or not isinstance(act["has_continuation"], bool)
            or not isinstance(act["evidence"], list)
        ):
            raise FatalAccounting("the Designator proposal seal has an invalid expected-act row")
        if act["act_id"] in act_ids or act["act_key"] in act_keys:
            raise FatalAccounting(
                "the Designator proposal seal names an act id or key more than once; "
                "a duplicate is not an additional denominator unit"
            )
        act_ids.add(act["act_id"])
        act_keys.add(act["act_key"])
        classify(DESIGNATOR, act.get("outcome"))
    # A seal from a served structure chair, or any real run, is recomputed from
    # its own evidence; ingress and serving are independent.
    structure_call = _structure_chair_call(context, payload)
    if structure_call is not None or is_real_ingress(context.run):
        by_subject = _proposal_evidence_by_subject(context, act_ids)
        _verify_real_act_denominator(context, acts, by_subject, structure_call=structure_call)
        _verify_proposal_seal_evidence(context, seal, acts, by_subject=by_subject)
    else:
        _verify_synthetic_act_denominator(context, acts)
        _verify_proposal_seal_evidence(context, seal, acts)
    return acts


def _structure_chair_call(context, payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The served structure chair's posture, or `None` when no chair was called.

    Read from the seal's validated provenance because `common/` may not import
    the serving catalogue.  Omitting `engine_call` gains nothing: real runs are
    recomputed anyway, and fixture runs face the stricter fixture floor.
    """
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("engine_call") is None:
        return None
    validate_serving_provenance(
        context, dict(provenance), producer_stage=DESIGNATOR, require_receipt=True
    )
    return dict(provenance["engine_call"])


def is_real_ingress(run: Mapping[str, Any]) -> bool:
    """Whether a run authority names the real route.

    An absent ingress record means synthetic (older test trees lack it).
    """
    return "ingress" in run and parse_ingress_record(run["ingress"]) == REAL_INGRESS


def _verify_real_act_denominator(
    context,
    acts: list[dict[str, Any]],
    by_subject: dict[str, list[dict[str, Any]]],
    *,
    structure_call: Mapping[str, Any] | None = None,
) -> None:
    """Every expected-act row on a real run, proven against its own evidence.

    Each row's class comes from which Designator record exists for it (residual
    hold, page hold, page-fallback, or own-page region), then is recomputed.
    Ambiguous or unevidenced rows are refused; classes are never tried in turn
    until one passes (principle 1).
    """
    fallbacks_by_subject = _designator_records_by_subject(context, "page-fallback")
    # Regions are placed against this run's pages, not the row under test.
    page_ordinals = {page_id: ordinal for ordinal, page_id in exemplar_page_ids(context).items()}
    holds_by_subject: dict[str, dict[str, Any]] = {}
    minted_rows: dict[str, dict[str, Any]] = {}
    verified_structure_attempt_pages: set[str] = set()
    attempts_by_page: dict[str, list[dict[str, Any]]] | None = None
    sealed_decoding: tuple[dict[str, Any], str] | None = None
    for answer in _stage_records(context.tree, DESIGNATOR, STRUCTURE_ANSWER_KIND):
        page_id = answer.get("subject_id")
        payload = answer.get("payload")
        if not isinstance(page_id, str) or not isinstance(payload, Mapping):
            raise FatalAccounting("a terminal structure answer does not bind a page payload")
        if payload.get("schema") not in STRUCTURE_ANSWER_RECORD_SCHEMAS:
            raise FatalAccounting(
                f"page {page_id}'s terminal structure answer has unsupported schema "
                f"{payload.get('schema')!r}"
            )
        if payload.get("schema") != STRUCTURE_ANSWER_RECORD_SCHEMA and attempts_by_page is None:
            attempts_by_page = _structure_attempts_by_page(context)
            sealed_decoding = load_decoding_policy(context.args.decoding_config)
        _verify_structure_attempt_chain(
            context,
            payload,
            page_id,
            attempts_by_page=attempts_by_page,
            sealed_decoding=sealed_decoding,
        )
        verified_structure_attempt_pages.add(page_id)
    observed = {act["act_id"]: act for act in acts}
    for act_id in sorted(observed):
        row = observed[act_id]
        records = by_subject.get(act_id, [])
        holds = [record for record in records if record["kind"] == "hold"]
        if len(holds) > 1:
            raise FatalAccounting(
                f"act {act_id} has {len(holds)} hold records; one act is held once, and "
                "nothing may decide which hold speaks for it"
            )
        hold = holds[0] if holds else None
        hold_payload = _payload_of(hold) if hold is not None else {}
        # Otherwise such a row would fall through as a proposal, its hold unread.
        if hold is not None and not {"residual_bounds", "page_bounds"} & set(hold_payload):
            raise FatalAccounting(
                f"act {act_id} carries a hold record naming neither residual_bounds nor "
                "page_bounds, so the rectangle it was held over cannot be recomputed; a held "
                "act whose hold the denominator cannot read is refused, never reclassified as "
                "a structural proposal"
            )
        proposal_regions = [record for record in records if record["kind"] == "region"]
        for record in proposal_regions:
            # Refused, not filtered: a dropped region could hide a continuation.
            if not isinstance(record["payload"].get("transform"), Mapping):
                raise FatalAccounting(
                    f"act {act_id}'s proposal region {record['artifact_id']!r} carries no "
                    "transform object, so the page it was cut from cannot be read; a region "
                    "the denominator cannot place is not a region it may pass over"
                )
            # A foreign or missing page must not count as a continuation.
            source_page_id = record["payload"]["transform"].get("source_page_id")
            if source_page_id not in page_ordinals:
                raise FatalAccounting(
                    f"act {act_id}'s proposal region {record['artifact_id']!r} names source "
                    f"page {source_page_id!r}, which this run's Exemplar never published; a "
                    "region the denominator cannot place on a page of this run is not a "
                    "region it may pass over"
                )
        regions = [
            record
            for record in proposal_regions
            if record["payload"]["transform"].get("source_page_id") == row["page_id"]
        ]
        far_regions = [record for record in proposal_regions if record not in regions]
        classes = []
        if "residual_bounds" in hold_payload:
            classes.append("residual")
        if "page_bounds" in hold_payload:
            classes.append("page-residual")
        if act_id in fallbacks_by_subject:
            classes.append("page-fallback")
        # Regions alone do not make a proposal: page-fallback acts have regions too.
        if regions and not classes:
            classes.append("proposal")
        if len(classes) > 1:
            raise FatalAccounting(
                f"act {act_id}'s Designator evidence matches more than one act class "
                f"({', '.join(classes)}); a row's class is decided by which evidence record "
                "exists for it, and ambiguous evidence is not a choice to make"
            )
        if not classes:
            raise FatalAccounting(
                f"act {act_id} has no Designator evidence to recompute its identity from: no "
                "proposal region on its page, no hold naming residual or page bounds, and no "
                "page-fallback record; real ingress carries no declaration to admit it on"
            )
        if classes == ["proposal"]:
            _verify_proposal_act_row(
                context,
                act_id,
                row,
                regions,
                far_regions,
                page_ordinals,
                structure_call=structure_call,
                verified_structure_attempt_pages=verified_structure_attempt_pages,
            )
            continue
        minted_rows[act_id] = row
        if hold is not None:
            holds_by_subject[act_id] = hold
    _verify_minted_act_rows(
        context, minted_rows, holds_by_subject, fallbacks_by_subject, beyond="the structural pass"
    )
    _verify_every_conservation_residual_is_accounted(context, observed, holds_by_subject)


def _structure_attempts_by_page(context) -> dict[str, list[dict[str, Any]]]:
    by_page: dict[str, list[dict[str, Any]]] = {}
    for attempt in _stage_records(context.tree, DESIGNATOR, STRUCTURE_ATTEMPT_KIND):
        page_id = attempt.get("subject_id")
        if isinstance(page_id, str):
            by_page.setdefault(page_id, []).append(attempt)
    return by_page


def _verify_structure_attempt_chain(
    context: StageContext,
    payload: Mapping[str, Any],
    page_id: str,
    *,
    attempts_by_page: Mapping[str, list[dict[str, Any]]] | None = None,
    sealed_decoding: tuple[Mapping[str, Any], str] | None = None,
) -> None:
    """Follow and reconcile every versioned terminal structure-attempt reference.

    Whole-run callers pass a shared attempt index and decoding read.
    """
    if payload.get("schema") == STRUCTURE_ANSWER_RECORD_SCHEMA:
        return
    policy = payload.get("attempt_policy")
    references = payload.get("attempts")
    ordinal = payload.get("attempt_ordinal")
    if (
        not isinstance(policy, Mapping)
        or set(policy) != {"max_attempts", "seed_schedule"}
        or policy.get("seed_schedule") not in {"fixed-base", "base-plus-attempt-ordinal-minus-one"}
        or not is_plain_int(policy.get("max_attempts"))
        or not 1 <= policy["max_attempts"] <= 3
        or not is_plain_int(ordinal)
        or not isinstance(references, list)
        or len(references) != ordinal
        or not 1 <= ordinal <= policy.get("max_attempts", 0)
    ):
        raise FatalAccounting(
            f"page {page_id}'s terminal structure answer has no bounded exact attempt ledger"
        )
    if sealed_decoding is None:
        sealed_decoding = load_decoding_policy(context.args.decoding_config)
    decoding_policy, decoding_digest = sealed_decoding
    decoding = payload.get("decoding")
    if (
        dict(policy) != structure_recovery_policy(decoding_policy)
        or not isinstance(decoding, Mapping)
        or decoding.get("decoding_config_sha256") != decoding_digest
    ):
        raise FatalAccounting(
            f"page {page_id}'s terminal structure answer attempt policy is not the one "
            "sealed by its decoding configuration"
        )
    if attempts_by_page is None:
        attempts_by_page = _structure_attempts_by_page(context)
    stored_attempts = list(attempts_by_page.get(page_id, []))
    if len(stored_attempts) != ordinal:
        raise FatalAccounting(
            f"page {page_id}'s terminal structure answer names {ordinal} attempts but its "
            f"stage manifest contains {len(stored_attempts)}; missing or extra history is "
            "not a contiguous ledger"
        )
    attempts: list[Mapping[str, Any]] = []
    for expected_ordinal, reference in enumerate(references, start=1):
        try:
            record = context.tree.read_artifact_reference(
                dict(reference),
                stage=DESIGNATOR,
                kind=STRUCTURE_ATTEMPT_KIND,
                subject_id=page_id,
            )
        except (SchemaRefusal, ContractError, OSError) as error:
            raise FatalAccounting(
                f"page {page_id}'s terminal structure answer names an invalid attempt "
                f"reference at ordinal {expected_ordinal}: {error}"
            ) from error
        attempt = record.get("payload")
        prior = references[: expected_ordinal - 1]
        if (
            record.get("attempt_id") != attempt_id(page_id, "structure", expected_ordinal)
            or not isinstance(attempt, Mapping)
            or attempt.get("schema")
            not in {STRUCTURE_ANSWER_RECORD_SCHEMA_V2, STRUCTURE_ANSWER_RECORD_SCHEMA_V3}
            or attempt.get("page_id") != page_id
            or attempt.get("page_ordinal") != payload.get("page_ordinal")
            or attempt.get("attempt_ordinal") != expected_ordinal
            or attempt.get("attempt_policy") != policy
            or attempt.get("attempts") != prior
            or not is_plain_int(attempt.get("attempt_seed"))
            or attempt.get("decoding") != payload.get("decoding")
        ):
            raise FatalAccounting(
                f"page {page_id}'s structure attempt {expected_ordinal} does not bind its "
                "identity, page, policy, config, and prior history"
            )
        if attempts:
            if (
                attempts[-1].get("schema") == STRUCTURE_ANSWER_RECORD_SCHEMA_V3
                and attempt.get("schema") == STRUCTURE_ANSWER_RECORD_SCHEMA_V2
            ):
                raise FatalAccounting(
                    f"page {page_id}'s structure attempt {expected_ordinal} downgrades its "
                    "native presentation schema"
                )
            expected_seed = (
                attempts[-1]["attempt_seed"]
                if policy["seed_schedule"] == "fixed-base"
                else attempts[-1]["attempt_seed"] + 1
            )
            if attempt["attempt_seed"] != expected_seed:
                raise FatalAccounting(
                    f"page {page_id}'s structure attempt {expected_ordinal} violates its "
                    "sealed seed schedule"
                )
        try:
            validate_serving_provenance(
                context,
                dict(attempt.get("provenance", {})),
                producer_stage=DESIGNATOR,
                require_receipt=True,
            )
        except (SchemaRefusal, ContractError) as error:
            raise FatalAccounting(
                f"page {page_id}'s structure attempt {expected_ordinal} has invalid "
                f"serving provenance: {error}"
            ) from error
        try:
            verify_structure_attempt_call(
                context,
                attempt,
                page_id,
                attempt_inputs=record.get("inputs"),
            )
        except (SchemaRefusal, ContractError) as error:
            raise FatalAccounting(
                f"page {page_id}'s structure attempt {expected_ordinal} has invalid "
                f"call evidence: {error}"
            ) from error
        attempts.append(attempt)
    expected_terminal = dict(attempts[-1])
    expected_terminal["attempts"] = references
    if dict(payload) != expected_terminal:
        raise FatalAccounting(
            f"page {page_id}'s terminal structure answer disagrees with its last attempt"
        )


def _verify_structure_request_image(
    context: StageContext,
    payload: Mapping[str, Any],
    page_id: str,
    attempt_inputs: object,
) -> dict[str, Any]:
    """Replay one v3 Chandra request image from its unchanged sealed page."""
    reference = payload.get("presentation_ref")
    try:
        record = context.tree.read_artifact_reference(
            dict(reference),
            stage=DESIGNATOR,
            kind=STRUCTURE_REQUEST_IMAGE_KIND,
            subject_id=page_id,
        )
    except (TypeError, ValueError, SchemaRefusal, ContractError, OSError) as error:
        raise ContractError(
            f"structure attempt for page {page_id} names an invalid request-image reference: "
            f"{error}"
        ) from error
    evidence = record.get("payload")
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != STRUCTURE_REQUEST_IMAGE_FIELDS
        or evidence.get("schema") != STRUCTURE_REQUEST_IMAGE_SCHEMA
        or evidence.get("page_id") != page_id
        or evidence.get("page_ordinal") != payload.get("page_ordinal")
    ):
        raise ContractError(
            f"structure attempt for page {page_id} has malformed request-image evidence"
        )
    source_ref, page_bytes, page_size = _structure_source_page(context, payload, page_id)
    if (
        evidence.get("source_image_ref") != source_ref
        or record.get("inputs") != [source_ref]
        or attempt_inputs != [source_ref, reference]
    ):
        raise ContractError(
            f"structure attempt for page {page_id} does not retain its exact source and "
            "request-image lineage"
        )
    presented = evidence.get("presented")
    try:
        presented = validate_presented(presented, page_size=page_size)
        validate_presented_page_binding(
            presented,
            page_ordinal=payload["page_ordinal"],
            page_image_path=source_ref["relative_path"],
            page_sha256=source_ref["sha256"],
            page_size=page_size,
            page_bytes=page_bytes,
        )
    except SchemaRefusal as error:
        raise ContractError(
            f"structure attempt for page {page_id} has invalid native image presentation: {error}"
        ) from error
    if presented["image_path"] != context.tree.blob_path(DESIGNATOR, presented["image_sha256"]):
        raise ContractError(
            f"structure attempt for page {page_id} does not retain its native request image "
            "at its Designator content address"
        )
    try:
        presented_bytes = context.tree.read_bytes(presented["image_path"])
    except OSError as error:
        raise ContractError(
            f"structure attempt for page {page_id} cannot read its presented image: {error}"
        ) from error
    if digest_bytes(presented_bytes) != presented["image_sha256"]:
        raise ContractError(
            f"structure attempt for page {page_id} presented-image bytes changed under their "
            "retained digest"
        )
    resize = presented["transform"]["resize"]
    if not _capacity_is_one_image(payload, resize["target_width_px"], resize["target_height_px"]):
        raise ContractError(
            f"structure attempt for page {page_id} capacity was not computed over the native "
            "request image"
        )
    return presented


def _capacity_is_one_image(payload: Mapping[str, Any], width: object, height: object) -> bool:
    capacity = payload.get("capacity")
    images = capacity.get("images") if isinstance(capacity, Mapping) else None
    return (
        isinstance(images, list)
        and len(images) == 1
        and isinstance(images[0], Mapping)
        and images[0].get("width") == width
        and images[0].get("height") == height
    )


def _structure_source_page(
    context: StageContext,
    payload: Mapping[str, Any],
    page_id: str,
) -> tuple[dict[str, str], bytes, tuple[int, int]]:
    """Read and bind the unchanged Exemplar page behind one structure attempt."""
    page = context.tree.read_artifact(
        EXEMPLAR,
        "page",
        artifact_id(EXEMPLAR, "page", page_id),
    )
    page_payload = page.get("payload")
    if not isinstance(page_payload, Mapping):
        raise ContractError(f"structure attempt for page {page_id} has no sealed source page")
    source_ref = {
        "relative_path": page_payload.get("image_path"),
        "sha256": page_payload.get("source_sha256"),
    }
    try:
        page_bytes = context.tree.read_bytes(source_ref["relative_path"])
    except (OSError, TypeError) as error:
        raise ContractError(
            f"structure attempt for page {page_id} cannot read its sealed source page: {error}"
        ) from error
    verify_input_bytes(source_ref, page_bytes)
    page_size = dimensions(page_bytes)
    if page_size != (payload.get("page_w"), payload.get("page_h")):
        raise ContractError(
            f"structure attempt for page {page_id} maps geometry against dimensions other "
            "than its sealed source page"
        )
    return source_ref, page_bytes, page_size


def verify_structure_attempt_call(
    context: StageContext,
    payload: Mapping[str, Any],
    page_id: str,
    *,
    attempt_inputs: object = None,
) -> None:
    """Bind one structure attempt to its retained response or transport call."""
    presented = None
    expected_image_sha256 = None
    if payload.get("schema") == STRUCTURE_ANSWER_RECORD_SCHEMA_V3:
        presented = _verify_structure_request_image(
            context,
            payload,
            page_id,
            attempt_inputs,
        )
        expected_image_sha256 = presented["image_sha256"]
    elif payload.get("schema") == STRUCTURE_ANSWER_RECORD_SCHEMA_V2:
        source_ref, _page_bytes, page_size = _structure_source_page(context, payload, page_id)
        if attempt_inputs != [source_ref]:
            raise ContractError(
                f"legacy v2 structure attempt for page {page_id} does not retain its exact "
                "sealed-page input"
            )
        if not _capacity_is_one_image(payload, *page_size):
            raise ContractError(
                f"legacy v2 structure attempt for page {page_id} capacity was not computed "
                "over its directly presented sealed page"
            )
        expected_image_sha256 = source_ref["sha256"]
    else:
        raise ContractError(
            f"structure attempt for page {page_id} has no supported versioned schema"
        )
    reference = payload.get("call_record_ref")
    if reference is None:
        if (
            payload.get("reason_code") != "structure-request-too-large"
            or payload.get("request_sha256") is not None
            or payload.get("raw_response_ref") is not None
            or payload.get("call_problem") is not None
        ):
            raise ContractError(
                f"structure attempt for page {page_id} has no call record outside a "
                "closed pre-wire capacity refusal"
            )
        return
    call_reference = _serving_evidence_reference(reference, "structure attempt call record")
    try:
        raw = context.tree.read_bytes(call_reference["relative_path"])
    except OSError as error:
        raise ContractError(
            f"structure attempt for page {page_id} names an unreadable call record: {error}"
        ) from error
    verify_input_bytes(call_reference, raw)
    try:
        call = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ContractError(
            f"structure attempt for page {page_id} names a malformed call record"
        ) from error
    if not isinstance(call, Mapping):
        raise ContractError(f"structure attempt for page {page_id} call record is not an object")
    schema = call.get("schema")
    expected_fields = {
        CHAIR_CALL_RECORD_SCHEMA_V1: CHAIR_CALL_RECORD_FIELDS_V1,
        CHAIR_CALL_RECORD_SCHEMA: CHAIR_CALL_RECORD_FIELDS,
        CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA: CHAIR_TRANSPORT_FAILURE_RECORD_FIELDS,
    }.get(schema)
    if expected_fields is None or set(call) != expected_fields:
        raise ContractError(
            f"structure attempt for page {page_id} call record has an unsupported or open schema"
        )
    decoding = payload.get("decoding")
    provenance = payload.get("provenance")
    generation_sent = call.get("generation_sent")
    call_identity = call.get("resolved_identity")
    provenance_revision = (
        provenance.get("resolved_revision") if isinstance(provenance, Mapping) else None
    )
    if (
        call.get("chair") != DESIGNATOR_CHAIR
        or call.get("kind") != "chat-completions"
        or not isinstance(decoding, Mapping)
        or decoding.get("policy") != "structure"
        or not isinstance(provenance, Mapping)
        or call.get("decoding_config_sha256") != decoding.get("decoding_config_sha256")
        or call.get("request_sha256") != payload.get("request_sha256")
        or not isinstance(generation_sent, Mapping)
        or generation_sent.get("seed") != payload.get("attempt_seed")
        or generation_sent.get("temperature") != decoding.get("temperature")
        or call.get("receipt_ref") != payload.get("receipt_ref")
        or call.get("receipt_ref") != provenance.get("receipt_ref")
        or call_identity != provenance.get("resolved_identity")
        or not isinstance(call_identity, Mapping)
        or call.get("serving_recipe") != call_identity.get("serving_recipe")
        or not isinstance(provenance_revision, Mapping)
        or call.get("resolved_revision") != provenance_revision.get("value")
        or call.get("served_model_id") != payload.get("served_model_id")
        or call.get("capacity") != payload.get("capacity")
        or call.get("image_sha256s") != [expected_image_sha256]
    ):
        raise ContractError(
            f"structure attempt for page {page_id} disagrees with its retained call record"
        )
    if schema == CHAIR_TRANSPORT_FAILURE_RECORD_SCHEMA:
        problem = call.get("transport_problem")
        response_fields = (
            "raw_response_ref",
            "response_sha256",
            "response_status",
            "response_model",
            "finish_reason",
            "usage",
            "parse_problem",
        )
        if (
            not isinstance(problem, Mapping)
            or set(problem) != CHAIR_TRANSPORT_PROBLEM_FIELDS
            or problem.get("schema") != CHAIR_TRANSPORT_PROBLEM_SCHEMA
            or problem.get("code") != "ENDPOINT_UNAVAILABLE"
            or not isinstance(problem.get("detail"), str)
            or not isinstance(problem.get("definitively_absent"), bool)
            or problem.get("request_delivery") != "unknown"
            or problem.get("response_completion") != "unknown"
            or any(call.get(field) is not None for field in response_fields)
            or payload.get("raw_response_ref") is not None
            or payload.get("custody_ref") is not None
            or payload.get("custody_problem") is not None
            or payload.get("finish_reason") is not None
            or payload.get("call_problem") != "CHAIR_TRANSPORT_FAILURE"
            or payload.get("reason_code") != "structure-call-unusable"
        ):
            raise ContractError(
                f"structure attempt for page {page_id} has inconsistent transport-failure evidence"
            )
        return
    raw_reference = _serving_evidence_reference(
        call.get("raw_response_ref"), "structure attempt raw response"
    )
    custody_problem = payload.get("custody_problem")
    if custody_problem is None:
        payload_response_matches = payload.get("raw_response_ref") == raw_reference
    else:
        payload_response_matches = (
            isinstance(custody_problem, str)
            and bool(custody_problem)
            and payload.get("raw_response_ref") is None
            and payload.get("custody_ref") is None
            and payload.get("reason_code") == "structure-response-not-retained"
        )
    if (
        not payload_response_matches
        or call.get("response_sha256") != raw_reference["sha256"]
        or call.get("finish_reason") != payload.get("finish_reason")
        or call.get("parse_problem") != payload.get("call_problem")
        or (
            call.get("parse_problem") is not None
            and payload.get("reason_code") != "structure-call-unusable"
        )
        or (
            schema == CHAIR_CALL_RECORD_SCHEMA
            and (
                not is_plain_int(call.get("response_status"))
                or not 100 <= call["response_status"] <= 599
            )
        )
    ):
        raise ContractError(
            f"structure attempt for page {page_id} disagrees with its retained response evidence"
        )
    try:
        response_bytes = context.tree.read_bytes(raw_reference["relative_path"])
    except OSError as error:
        raise ContractError(
            f"structure attempt for page {page_id} names an unreadable raw response: {error}"
        ) from error
    verify_input_bytes(raw_reference, response_bytes)


def _verify_proposal_act_row(
    context,
    act_id: str,
    row: dict[str, Any],
    regions: list[dict[str, Any]],
    far_regions: list[dict[str, Any]],
    page_ordinals: dict[str, int],
    *,
    structure_call: Mapping[str, Any] | None,
    verified_structure_attempt_pages: set[str],
) -> None:
    """A structural act, recomputed and then held to the answer it came from.

    With a served chair, the rectangle must appear exactly in the chair's
    published answer for a scanned page, or the act would carry the chair's
    provenance over ink it never proposed (principles 6, 8).  Exact match only:
    a nearest match would be a selection (principle 1).  Presence, not
    uniqueness: identical rectangles on one page are one act.

    The published act list is checked, not the retained response bytes, so a
    doctored list published beside its status still passes.
    """
    _verify_structural_act_row(act_id, row, regions, far_regions, page_ordinals)
    if structure_call is None:
        # On real ingress, omitting `engine_call` must not skip the answer check.
        if is_real_ingress(context.run):
            raise FatalAccounting(
                f"act {act_id} is a structural proposal on real ingress, but the proposal "
                "seal's own provenance names no engine_call; a real submission's structural "
                "proposal is minted from a served structure chair's answer, and a seal that "
                "omits the call it was served under may not be admitted on a recomputed "
                "rectangle alone"
            )
        return None
    bounds = regions[0]["payload"]["raw_bounds"]
    status = context.tree.read_artifact(
        DESIGNATOR,
        "structure-status",
        artifact_id(DESIGNATOR, "structure-status", row["page_id"]),
    )
    status_payload = _payload_of(status)
    if (
        status_payload.get("state") != "scanned"
        or status_payload.get("page_id") != row["page_id"]
        or status_payload.get("page_ordinal") != row["page_ordinal"]
    ):
        raise FatalAccounting(
            f"act {act_id} is a structural proposal on page {row['page_id']} at page ordinal "
            f"{row['page_ordinal']!r}, but that page's own structure-status records state "
            f"{status_payload.get('state')!r} for page {status_payload.get('page_id')!r} at "
            f"ordinal {status_payload.get('page_ordinal')!r}; a rectangle is not marked out on "
            "a page the structure pass did not scan, nor under an ordinal that page never had"
        )
    reference = status_payload.get("structure_answer_ref")
    if not isinstance(reference, Mapping):
        raise FatalAccounting(
            f"act {act_id}'s page {row['page_id']} was marked out by a served structure chair, "
            "but its structure-status names no retained structure answer; the answer is the "
            "only record of what the chair actually returned, and without it this rectangle "
            "rests on nothing but the producer's own word"
        )
    answer = context.tree.read_artifact_reference(
        dict(reference),
        stage=DESIGNATOR,
        kind=STRUCTURE_ANSWER_KIND,
        subject_id=row["page_id"],
    )
    payload = _payload_of(answer)
    if payload.get("schema") not in STRUCTURE_ANSWER_RECORD_SCHEMAS:
        raise FatalAccounting(
            f"act {act_id}'s page names a structure answer whose schema is "
            f"{payload.get('schema')!r}, not one of {sorted(STRUCTURE_ANSWER_RECORD_SCHEMAS)!r}"
        )
    if row["page_id"] not in verified_structure_attempt_pages:
        _verify_structure_attempt_chain(context, payload, row["page_id"])
        verified_structure_attempt_pages.add(row["page_id"])
    if payload.get("parse_state") != STRUCTURE_ANSWER_PARSED:
        raise FatalAccounting(
            f"act {act_id} was minted from page {row['page_id']}'s structure answer, whose "
            f"parse state is {payload.get('parse_state')!r} with outcome "
            f"{payload.get('parse_outcome')!r}; a page whose answer did not parse is held, and "
            "a held page proposes nothing"
        )
    if (
        payload.get("page_id") != row["page_id"]
        or payload.get("page_ordinal") != row["page_ordinal"]
    ):
        raise FatalAccounting(
            f"act {act_id}'s seal row names page {row['page_id']} at ordinal "
            f"{row['page_ordinal']!r}, but the structure answer it rests on names page "
            f"{payload.get('page_id')!r} at ordinal {payload.get('page_ordinal')!r}"
        )
    answer_provenance = (
        payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
    )
    # Validated in full, not only on `engine_call`.
    validate_serving_provenance(
        context, dict(answer_provenance), producer_stage=DESIGNATOR, require_receipt=True
    )
    if answer_provenance.get("engine_call") != dict(structure_call):
        raise FatalAccounting(
            f"act {act_id}'s structure answer was produced under a different structure-chair "
            "call than the proposal seal records; one run's seal and the answers its acts were "
            "minted from name one serving posture"
        )
    try:
        call_record_reference = _serving_evidence_reference(
            payload.get("call_record_ref"), "structure answer call record"
        )
    except SchemaRefusal as error:
        raise FatalAccounting(
            f"act {act_id}'s structure answer parsed but names no usable call record to have "
            f"parsed: {error}"
        ) from error
    try:
        call_record_bytes = context.tree.read_bytes(call_record_reference["relative_path"])
    except OSError as error:
        raise FatalAccounting(
            f"act {act_id}'s structure answer names a call record that could not be read: {error}"
        ) from error
    try:
        verify_input_bytes(call_record_reference, call_record_bytes)
    except SchemaRefusal as error:
        raise FatalAccounting(
            f"act {act_id}'s structure answer names a call record reference whose bytes have "
            f"changed since it was cited: {error}"
        ) from error
    try:
        call_record = json.loads(call_record_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise FatalAccounting(
            f"act {act_id}'s structure answer names a call record that is not valid JSON: {error}"
        ) from error
    if (
        not isinstance(call_record, Mapping)
        or call_record.get("schema") not in CHAIR_CALL_RECORD_SCHEMAS
        or call_record.get("chair") != DESIGNATOR_CHAIR
        or call_record.get("decoding_config_sha256") != structure_call["decoding_config_sha256"]
    ):
        raise FatalAccounting(
            f"act {act_id}'s structure answer names a call record without a supported "
            f"chair-call-record schema for chair {DESIGNATOR_CHAIR!r} under the "
            "seal's own sealed decoding digest; a parsed answer naming a call record with no "
            "genuine reading behind it is a reading of nothing"
        )
    answered = payload.get("acts")
    if not isinstance(answered, list):
        raise FatalAccounting(
            f"act {act_id}'s structure answer carries no act list to check its rectangle against"
        )
    if payload.get("act_count") != len(answered):
        raise FatalAccounting(
            f"act {act_id}'s structure answer counts {payload.get('act_count')!r} acts over a "
            f"list of {len(answered)}; the record's own denominator does not reconcile"
        )
    if not any(
        isinstance(entry, Mapping) and entry.get("raw_bounds") == bounds for entry in answered
    ):
        raise FatalAccounting(
            f"act {act_id} was minted over rectangle {bounds}, which page {row['page_id']}'s "
            "structure answer does not list at any ordinal; a crop the structure chair never "
            "returned may not be attributed to it"
        )


def _verify_structural_act_row(
    act_id: str,
    row: dict[str, Any],
    regions: list[dict[str, Any]],
    far_regions: list[dict[str, Any]],
    page_ordinals: dict[str, int],
) -> None:
    """A real structural act, recomputed from the rectangle it was minted over.

    Identity binds only page, class and `raw_bounds`, so `act_key`,
    `page_ordinal` and `has_continuation` are recomputed separately: later
    stages join on them.  `has_continuation` is checked both ways, since a
    false negative silently drops a continuation crop (goal 2).  A continuation
    region's bounds enter no identity, so it need only carry readable
    `raw_bounds`.
    """
    if len(regions) != 1:
        raise FatalAccounting(
            f"act {act_id} has {len(regions)} proposal regions on page {row['page_id']}, not "
            "exactly one; a structural act is minted over one rectangle on its own page"
        )
    region_payload = regions[0]["payload"]
    bounds = region_payload.get("raw_bounds")
    if not isinstance(bounds, dict):
        raise FatalAccounting(
            f"act {act_id}'s proposal region carries no raw_bounds to recompute its identity "
            "from; the real structural pass must publish the rectangle the act was minted over"
        )
    try:
        verify_identity(act_id, "act", act_bindings(row["page_id"], "proposal", bounds))
    except IdentityRefusal as error:
        raise FatalAccounting(
            f"act {act_id} does not verify against the proposal class and the raw_bounds its "
            f"own region record names: {error}"
        ) from error
    if region_payload.get("act_key") != row["act_key"]:
        raise FatalAccounting(
            f"act {act_id}'s seal row names act_key {row['act_key']!r}, but its own proposal "
            f"region names act_key {region_payload.get('act_key')!r}; the two must agree"
        )
    region_ordinal = region_payload["transform"].get("source_page_ordinal")
    if region_ordinal != row["page_ordinal"]:
        raise FatalAccounting(
            f"act {act_id}'s seal row names page_ordinal {row['page_ordinal']!r}, but its own "
            f"proposal region names source_page_ordinal {region_ordinal!r}; the two must agree"
        )
    if len(far_regions) > 1:
        raise FatalAccounting(
            f"act {act_id} has {len(far_regions)} proposal regions on pages other than "
            f"{row['page_id']}; a continuation is at most one region on one far page"
        )
    if row["has_continuation"] != bool(far_regions):
        raise FatalAccounting(
            f"act {act_id}'s seal row names has_continuation={row['has_continuation']!r}, but "
            f"its Designator evidence names {len(far_regions)} proposal region(s) on a page "
            "other than its own; the two must agree"
        )
    if not far_regions:
        return
    far_payload = far_regions[0]["payload"]
    if far_payload.get("act_key") != row["act_key"]:
        raise FatalAccounting(
            f"act {act_id}'s continuation region names act_key {far_payload.get('act_key')!r}, "
            f"but its seal row names act_key {row['act_key']!r}; the two must agree"
        )
    far_page_id = far_payload["transform"]["source_page_id"]
    far_ordinal = far_payload["transform"].get("source_page_ordinal")
    if far_ordinal != page_ordinals[far_page_id]:
        raise FatalAccounting(
            f"act {act_id}'s continuation region on page {far_page_id} names "
            f"source_page_ordinal {far_ordinal!r}, but that page is ordinal "
            f"{page_ordinals[far_page_id]} of this submission; the two must agree"
        )
    if not isinstance(far_payload.get("raw_bounds"), dict):
        raise FatalAccounting(
            f"act {act_id}'s continuation region carries no raw_bounds, so the rectangle cut "
            "from the far page cannot be read; the row's has_continuation has already promised "
            "that crop to a reader, and a continuation nothing can open is not one to pass over"
        )


def _verify_synthetic_act_denominator(context, acts: list[dict[str, Any]]) -> None:
    """Bind the skeleton's discovered-act denominator to its sealed fixture input.

    The fixture's acts are a floor: each must appear.  Extra acts (residual,
    page-fallback, page-residual) are not fixture data and are recomputed from
    their own Designator evidence instead.
    """
    fixture_acts = context.fixture.get("act", [])
    expected = {
        fixture_identity.act_identity(context.fixture, row): {
            "act_key": row["key"],
            "page_id": fixture_identity.page_identity(context.fixture, row["page_ordinal"]),
            "page_ordinal": row["page_ordinal"],
            "has_continuation": continuation_for(context.fixture, row["key"]) is not None,
        }
        for row in fixture_acts
    }
    observed = {act["act_id"]: act for act in acts}
    missing = set(expected) - set(observed)
    if missing:
        raise FatalAccounting(
            "the proposal seal expected-act denominator does not reconcile to every synthetic "
            "act bound into this run"
        )
    for act_id, facts in expected.items():
        row = observed[act_id]
        if any(
            row[field] != value for field, value in facts.items() if field != "has_continuation"
        ):
            raise FatalAccounting(
                f"proposal-seal act {act_id} does not match its sealed synthetic act identity"
            )
        if not facts["has_continuation"] and row["has_continuation"]:
            raise FatalAccounting(
                f"proposal-seal act {act_id} claims a continuation not declared in the fixture"
            )
        if row["outcome"] == "proposed" and row["has_continuation"] != facts["has_continuation"]:
            raise FatalAccounting(
                f"proposed act {act_id} does not account for its declared continuation"
            )
    # Sorted so the first refused row is the same on every run.  Holds are read
    # once for both checks: each read walks the whole manifest.
    holds_by_subject = _designator_records_by_subject(context, "hold")
    _verify_minted_act_rows(
        context,
        {act_id: observed[act_id] for act_id in sorted(set(observed) - set(expected))},
        holds_by_subject,
    )
    _verify_every_conservation_residual_is_accounted(context, observed, holds_by_subject)


def _verify_every_conservation_residual_is_accounted(
    context,
    observed: dict[str, dict[str, Any]],
    holds_by_subject: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Every residual a conservation record found must reach the denominator.

    The reverse of `_verify_minted_act_rows`: a residual the seal never named
    leaves no artifact to miss, so without this it vanishes silently
    (principle 2).  A page over the sealed residual bound may withhold its
    list only if it is held as exactly one page-residual item.
    """
    if holds_by_subject is None:
        holds_by_subject = _designator_records_by_subject(context, "hold")
    accounted_pages = _page_residual_holds_by_page(holds_by_subject, observed)
    for page_id, record in _designator_records_by_subject(context, "conservation").items():
        payload = _payload_of(record)
        enumeration = payload.get("residual_enumeration")
        if enumeration == RESIDUAL_ENUMERATION_WITHHELD:
            _verify_withheld_page_is_held_as_one_item(
                page_id, payload, accounted_pages.get(page_id, [])
            )
            continue
        if enumeration not in (RESIDUAL_ENUMERATION_COMPLETE, RESIDUAL_ENUMERATION_AGGREGATED):
            raise FatalAccounting(
                f"the conservation record for page {page_id} records its residual enumeration as "
                f"{enumeration!r}, which is outside the closed set {RESIDUAL_ENUMERATIONS}; a "
                "consumer cannot tell a page with no unclaimed ink from one whose unclaimed ink "
                "was counted and not listed without being told which it is"
            )
        components, aggregate = _verify_residual_component_partition(
            context, page_id, payload, enumeration
        )
        if enumeration == RESIDUAL_ENUMERATION_AGGREGATED:
            _verify_aggregated_page_is_held_as_one_item(
                page_id, payload, accounted_pages.get(page_id, [])
            )
        for index, component in enumerate(components):
            bounds = component.get("bounds") if isinstance(component, Mapping) else None
            if not isinstance(bounds, dict):
                raise FatalAccounting(
                    f"the conservation record for page {page_id} carries a residual at index "
                    f"{index} with no bounds to recompute an act identity from"
                )
            minted = derive_act_id(page_id, "residual", bounds)
            row = observed.get(minted)
            if row is None or row["outcome"] != "held":
                raise FatalAccounting(
                    f"page {page_id}'s conservation record reconciles residual ink at index "
                    f"{index} ({bounds}) that the proposal seal accounts for no held act for; "
                    "ink this stage measured and no crop claimed may not leave the denominator "
                    "silently"
                )


def _verify_aggregated_page_is_held_as_one_item(
    page_id: str, payload: Mapping[str, Any], holds: list[Mapping[str, Any]]
) -> None:
    """An aggregate represents components; it never rebrands them as one act."""
    aggregate = payload.get("aggregated_residual_components")
    if not isinstance(aggregate, list) or not aggregate:
        raise FatalAccounting(
            f"page {page_id}'s aggregate residual enumeration has no retained components"
        )
    if len(holds) != 1:
        raise FatalAccounting(
            f"page {page_id}'s aggregate residual accounting needs exactly one page-residual "
            f"hold, found {len(holds)}"
        )
    declared = holds[0].get("aggregated_component_count")
    if not _is_count(declared) or declared != len(aggregate):
        raise FatalAccounting(
            f"page {page_id}'s page-residual hold does not retain the aggregate component count"
        )


def sealed_residual_presentation_policy(context) -> dict[str, int]:
    """Read the two aggregate floors from the grouping policy this run sealed."""
    path = Path(
        getattr(getattr(context, "args", None), "designator_grouping_config", None)
        or DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH
    )
    try:
        document, observed_digest = read_sealed_toml(path, "Designator grouping configuration")
        table = document["grouping"]["residual_presentation"]
    except (ContractError, KeyError, TypeError) as error:
        raise FatalAccounting(
            "the sealed Designator grouping policy has no readable residual presentation table"
        ) from error
    require_sealed_config(
        run_sealed_config_digests(context.run),
        "designator-grouping",
        observed_digest,
        "this context",
    )
    names = ("residual_aggregate_max_pixel_count", "residual_aggregate_max_area_px")
    if set(table) != set(names) | {"provenance"} or any(
        not is_plain_int(table.get(name)) or table[name] < 0 for name in names
    ):
        raise FatalAccounting(
            "the sealed Designator residual presentation policy is not the closed pair of "
            "non-negative integer pixel and area thresholds"
        )
    return {name: table[name] for name in names}


def _verify_residual_component_partition(
    context,
    page_id: str,
    payload: Mapping[str, Any],
    enumeration: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Verify retained geometry, pixels, policy, and the promoted/aggregate partition."""
    promoted = payload.get("residual_components")
    aggregate = payload.get("aggregated_residual_components", [])
    if not isinstance(promoted, list) or not isinstance(aggregate, list):
        raise FatalAccounting(
            f"the conservation record for page {page_id} carries malformed retained component lists"
        )
    if enumeration == RESIDUAL_ENUMERATION_COMPLETE and aggregate:
        raise FatalAccounting(
            f"the conservation record for page {page_id} calls its residual enumeration "
            "complete while retaining aggregate components"
        )
    if enumeration == RESIDUAL_ENUMERATION_AGGREGATED and not aggregate:
        raise FatalAccounting(
            f"the conservation record for page {page_id} calls its residual enumeration "
            "aggregate-page-held without retaining aggregate components"
        )
    if enumeration == RESIDUAL_ENUMERATION_COMPLETE:
        declared_total = payload.get("residual_component_count")
        if not _is_count(declared_total) or declared_total != len(promoted):
            raise FatalAccounting(
                f"the conservation record for page {page_id} names residual_component_count "
                f"{declared_total!r} but lists {len(promoted)} residual components"
            )
        return promoted, aggregate
    width, height = payload.get("page_width"), payload.get("page_height")
    if not _is_count(width) or not _is_count(height) or width == 0 or height == 0:
        raise FatalAccounting(
            f"the conservation record for page {page_id} has no positive page geometry"
        )
    policy = sealed_residual_presentation_policy(context)
    if any(
        not is_plain_int(payload.get(name)) or payload.get(name) != value
        for name, value in policy.items()
    ):
        raise FatalAccounting(
            f"the conservation record for page {page_id} does not name the exact sealed "
            "residual presentation thresholds"
        )
    declared_promoted = payload.get("residual_promoted_component_count")
    declared_aggregate = payload.get("residual_aggregated_component_count")
    declared_total = payload.get("residual_component_count")
    if (
        not _is_count(declared_promoted)
        or not _is_count(declared_aggregate)
        or not _is_count(declared_total)
        or declared_promoted != len(promoted)
        or declared_aggregate != len(aggregate)
        or declared_total != len(promoted) + len(aggregate)
    ):
        raise FatalAccounting(
            f"the conservation record for page {page_id} does not reconcile its promoted, "
            "aggregate, and total component counts"
        )
    identities: set[tuple[int, int, int, int]] = set()
    for label, rows in (("promoted", promoted), ("aggregate", aggregate)):
        for index, component in enumerate(rows):
            bounds = component.get("bounds") if isinstance(component, Mapping) else None
            pixels = component.get("pixel_count") if isinstance(component, Mapping) else None
            if (
                not isinstance(bounds, Mapping)
                or set(bounds) != {"x", "y", "w", "h"}
                or any(not is_plain_int(bounds[k]) for k in bounds)
                or bounds["x"] < 0
                or bounds["y"] < 0
                or bounds["w"] <= 0
                or bounds["h"] <= 0
                or bounds["x"] + bounds["w"] > width
                or bounds["y"] + bounds["h"] > height
                or not _is_count(pixels)
                or pixels > bounds["w"] * bounds["h"]
            ):
                raise FatalAccounting(
                    f"the conservation record for page {page_id} has malformed {label} "
                    f"component {index}"
                )
            identity = tuple(bounds[name] for name in ("x", "y", "w", "h"))
            if identity in identities:
                raise FatalAccounting(
                    f"the conservation record for page {page_id} repeats residual component "
                    f"identity {identity} across its partition"
                )
            identities.add(identity)
            area = bounds["w"] * bounds["h"]
            significant = (
                pixels >= policy["residual_aggregate_max_pixel_count"]
                or area >= policy["residual_aggregate_max_area_px"]
            )
            if (label == "promoted") != significant:
                raise FatalAccounting(
                    f"the conservation record for page {page_id} classifies {label} component "
                    f"{index} against thresholds other than the sealed presentation policy"
                )
    residual_pixels = payload.get("residual_pixel_count")
    if (
        not _is_count(residual_pixels)
        or sum(component["pixel_count"] for component in [*promoted, *aggregate]) != residual_pixels
    ):
        raise FatalAccounting(
            f"the conservation record for page {page_id} retained component pixels do not "
            "equal residual_pixel_count"
        )
    return promoted, aggregate


def _page_residual_holds_by_page(
    holds_by_subject: dict[str, dict[str, Any]], observed: dict[str, dict[str, Any]]
) -> dict[str, list[Mapping[str, Any]]]:
    """Every page-residual hold in the run, indexed by the page it holds.

    Read from the Designator's artifacts, not the seal, so a hold the seal
    never accounted for is refused.
    """
    by_page: dict[str, list[Mapping[str, Any]]] = {}
    for act_id, hold in holds_by_subject.items():
        payload = _payload_of(hold)
        if "page_bounds" not in payload:
            continue
        row = observed.get(act_id)
        if row is None:
            raise FatalAccounting(
                f"the Designator published a page-residual hold for act {act_id}, which the "
                "proposal seal's expected-act denominator does not account for; a page held in "
                "place of its residuals is a unit this run reports, not evidence beside the "
                "denominator"
            )
        by_page.setdefault(row["page_id"], []).append(payload)
    return by_page


def _verify_withheld_page_is_held_as_one_item(
    page_id: str, payload: Mapping[str, Any], holds: list[Mapping[str, Any]]
) -> None:
    """A record that withheld its components owes exactly one page-residual row.

    That row must name the same bound the record applied.
    """
    if len(holds) != 1:
        raise FatalAccounting(
            f"page {page_id}'s conservation record withheld its residual components, but the run "
            f"carries {len(holds)} page-residual holds for that page rather than exactly one; "
            "unlisted ink is accounted for by the single review item that replaced it, or it is "
            "lost silently"
        )
    bound = payload.get("max_residual_components")
    if not _is_count(bound):
        raise FatalAccounting(
            f"page {page_id}'s conservation record withheld its residual components without "
            "naming the integer bound it was judged against"
        )
    held_bound = holds[0].get("max_residual_components")
    if not _is_count(held_bound) or held_bound != bound:
        raise FatalAccounting(
            f"page {page_id} is held against a bound of {held_bound!r} residual components "
            f"while its own conservation record applied {bound}; the held page and the "
            "reconciliation that held it must name one policy, as one integer"
        )


def fallback_page_act_key(page_ordinal: int) -> str:
    """The human-readable label of the one act a page's fallback crops belong to.

    A label only; identity comes from the ``page-fallback`` act class.
    """
    return f"page-fallback:{page_ordinal}"


# How much of a page's residuals its conservation record lists, so "no
# residual" and "counted but not listed" stay distinguishable.
RESIDUAL_ENUMERATION_COMPLETE: Final = "complete"
RESIDUAL_ENUMERATION_AGGREGATED: Final = "aggregate-page-held"
RESIDUAL_ENUMERATION_WITHHELD: Final = "withheld-page-held"
RESIDUAL_ENUMERATIONS: Final = (
    RESIDUAL_ENUMERATION_COMPLETE,
    RESIDUAL_ENUMERATION_AGGREGATED,
    RESIDUAL_ENUMERATION_WITHHELD,
)

# Page-residual hold causes, shared by the Designator and this verifier.
PAGE_RESIDUAL_REASON_CODE: Final = "residual-components-over-page-bound"
PAGE_RESIDUAL_AGGREGATE_REASON_CODE: Final = "residual-components-below-presentation-threshold"


def page_residual_act_key(page_ordinal: int) -> str:
    """The label of the one act a page held for over-bound residual scatter becomes.

    A label only; identity comes from the ``page-residual`` act class.
    """
    return f"page-residual:{page_ordinal}"


def _verify_minted_act_rows(
    context,
    extra_rows: dict[str, dict[str, Any]],
    holds_by_subject: dict[str, dict[str, Any]] | None = None,
    fallbacks_by_subject: dict[str, dict[str, Any]] | None = None,
    *,
    beyond: str = "the fixture",
) -> None:
    """Every expected-act row beyond the fixture's own denominator.

    `beyond` names the baseline in refusals, so a real run is never told about
    a fixture it does not have.  Three kinds may be added, each recomputed from
    its own Designator record rather than trusted:

    * a conservation residual: `held`, no continuation; its `page_ordinal` and
      `act_key` are checked too, since identity does not bind them;
    * a page-fallback act: `proposed`, premised on the page's `structure-status`
      saying the structure pass found nothing;
    * a page-residual act: `held`, one review item for a page over the residual
      bound.  Holds route by which rectangle they name.

    Indexes are built once: a foxed page can mint tens of thousands of rows.
    """
    if holds_by_subject is None:
        holds_by_subject = _designator_records_by_subject(context, "hold") if extra_rows else {}
    if fallbacks_by_subject is None:
        fallbacks_by_subject = (
            _designator_records_by_subject(context, "page-fallback") if extra_rows else {}
        )
    for act_id, row in extra_rows.items():
        if row["has_continuation"]:
            raise FatalAccounting(
                f"act {act_id} extends the denominator beyond {beyond} but claims a "
                "continuation; a residual has no declared continuation to claim, and neither "
                "has a page-fallback or page-residual act"
            )
        if row["outcome"] == "proposed":
            _verify_page_fallback_act_row(context, act_id, row, fallbacks_by_subject, beyond=beyond)
            continue
        if row["outcome"] != "held":
            raise FatalAccounting(
                f"act {act_id} extends the denominator beyond {beyond} and is neither 'held' "
                f"nor 'proposed'; the only units that may extend the denominator beyond "
                f"{beyond} are a conservation residual, a page-residual hold, and a "
                "page-fallback act"
            )
        hold = holds_by_subject.get(act_id)
        if hold is None:
            raise FatalAccounting(
                f"act {act_id} extends the denominator beyond {beyond} but the Designator "
                "published no hold record for it"
            )
        payload = _payload_of(hold)
        # Presence, not shape, so a malformed page hold is refused as one.
        if "page_bounds" in payload:
            _verify_page_residual_act_row(context, act_id, row, hold)
            continue
        bounds = payload.get("residual_bounds")
        if not isinstance(bounds, dict):
            raise FatalAccounting(
                f"act {act_id}'s hold record carries no residual bounds to recompute its "
                "identity from"
            )
        try:
            verify_identity(act_id, "act", act_bindings(row["page_id"], "residual", bounds))
        except IdentityRefusal as error:
            raise FatalAccounting(
                f"act {act_id} does not verify against the residual class and bounds its own "
                f"hold record names: {error}"
            ) from error
        if payload.get("page_ordinal") != row["page_ordinal"]:
            raise FatalAccounting(
                f"act {act_id}'s seal row names page_ordinal {row['page_ordinal']!r}, but its "
                f"own hold record names page_ordinal {payload.get('page_ordinal')!r}; the two "
                "must agree"
            )
        if payload.get("act_key") != row["act_key"]:
            raise FatalAccounting(
                f"act {act_id}'s seal row names act_key {row['act_key']!r}, but its own hold "
                f"record names act_key {payload.get('act_key')!r}; the two must agree"
            )
        _verify_residual_traces_to_conservation(context, act_id, row["page_id"], hold, bounds)


def _prove_page_wide_act_rectangle(
    context, act_id: str, page_id: str, ordinal: int, bounds: dict, act_class: str
) -> None:
    """The read/re-derive proof both page-wide act rows share, parameterized by class.

    The whole-page rectangle comes from the sealed page bytes, never from the
    record's own claim.
    """
    sources = [
        source
        for source in context.run.get("source_manifest", [])
        if source.get("ordinal") == ordinal
    ]
    if len(sources) != 1:
        raise FatalAccounting(
            f"act {act_id}'s page ordinal {ordinal} does not name exactly one sealed source"
        )
    page = context.tree.read_artifact(EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", page_id))
    verify_sealed_page_pixels(context.tree, context.run, sources[0], page)
    page_bytes = context.tree.read_bytes(page["payload"]["image_path"])
    width, height = dimensions(page_bytes)
    full_page_bounds = {"x": 0, "y": 0, "w": width, "h": height}
    if bounds != full_page_bounds:
        raise FatalAccounting(
            f"act {act_id}'s {act_class} rectangle {bounds} is not the complete sealed page "
            f"rectangle {full_page_bounds}"
        )
    try:
        verify_identity(act_id, "act", act_bindings(page_id, act_class, bounds))
    except IdentityRefusal as error:
        raise FatalAccounting(
            f"act {act_id} does not verify against the reserved {act_class} class and the "
            f"page rectangle its own record names: {error}"
        ) from error


def _binds_page_row(payload: Mapping[str, Any], row: Mapping[str, Any], act_key: str) -> bool:
    """Whether a page-wide record names its seal row's page, ordinal, derived key and a rectangle."""
    return (
        isinstance(payload.get("page_bounds"), dict)
        and payload.get("act_key") == row["act_key"] == act_key
        and payload.get("page_id") == row["page_id"]
        and payload.get("page_ordinal") == row["page_ordinal"]
    )


def _verify_page_fallback_act_row(
    context,
    act_id: str,
    row: dict[str, Any],
    fallbacks_by_subject: dict[str, dict[str, Any]],
    *,
    beyond: str = "the fixture",
) -> None:
    """The one extra row that may be `proposed`, checked against its own evidence.

    Besides identity, its premise is checked: the page's `structure-status`
    must say the structure pass fell back to tiles (principle 8).
    """
    record = fallbacks_by_subject.get(act_id)
    if record is None:
        raise FatalAccounting(
            f"act {act_id} extends the denominator beyond {beyond} as a proposed act but the "
            "Designator published no page-fallback record for it; it is not 'held' either, so it "
            "is not a conservation residual"
        )
    payload = _payload_of(record)
    bounds = payload.get("page_bounds")
    ordinal = row["page_ordinal"]
    if not _binds_page_row(payload, row, fallback_page_act_key(ordinal)):
        raise FatalAccounting(
            f"act {act_id}'s page-fallback record does not carry the page id, page ordinal, "
            "derived fallback key, and page rectangle it must bind"
        )
    _prove_page_wide_act_rectangle(
        context, act_id, row["page_id"], ordinal, bounds, "page-fallback"
    )
    inputs = record.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise FatalAccounting(
            f"act {act_id}'s page-fallback record does not reference exactly one "
            "structure-status artifact to check its premise against"
        )
    status = context.tree.read_artifact_reference(
        inputs[0], stage=DESIGNATOR, kind="structure-status", subject_id=row["page_id"]
    )
    status_payload = status.get("payload")
    evidence = (
        status_payload.get("structure_evidence") if isinstance(status_payload, Mapping) else None
    )
    if evidence != "fallback-tiles":
        raise FatalAccounting(
            f"act {act_id} is a page-fallback act, but page {row['page_id']}'s own "
            f"structure-status records its structural evidence as {evidence!r} rather than "
            "'fallback-tiles'; a predetermined grid may not be minted over a page the "
            "structure pass actually found regions on"
        )


def _verify_page_residual_act_row(
    context, act_id: str, row: dict[str, Any], hold: dict[str, Any]
) -> None:
    """The held row that stands for a whole page, checked against its own evidence.

    Aggregated records keep their components; legacy withheld records keep only
    a count and bound.  Rectangle, identity, premise (the page's conservation
    record), grouping digest (against the run's seal), component count
    (principle 8) and cause are all recomputed; consumers route on the cause
    code.
    """
    payload = _payload_of(hold)
    if "residual_bounds" in payload:
        raise FatalAccounting(
            f"act {act_id}'s hold names both a residual rectangle and a page rectangle; a hold "
            "accounts for one component of unclaimed ink or for a whole page held in place of "
            "its components, and nothing may decide which of the two it meant"
        )
    bounds = payload.get("page_bounds")
    ordinal = row["page_ordinal"]
    if not _binds_page_row(payload, row, page_residual_act_key(ordinal)):
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not carry the page id, page ordinal, "
            "derived page-residual key, and page rectangle it must bind"
        )
    reason_code = payload.get("reason_code")
    if (
        reason_code not in (PAGE_RESIDUAL_REASON_CODE, PAGE_RESIDUAL_AGGREGATE_REASON_CODE)
        or payload.get("blocking_page_ordinal") != ordinal
    ):
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold records its cause as "
            f"{payload.get('reason_code')!r} against page "
            f"{payload.get('blocking_page_ordinal')!r} rather than "
            "a supported page-residual cause against page "
            f"{ordinal}; the hold vocabulary is "
            "closed so that a consumer can branch on the cause without reading prose"
        )
    grouping_digest = payload.get("grouping_config_sha256")
    if not isinstance(grouping_digest, str) or not grouping_digest:
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not name the sealed grouping "
            "configuration digest its residual presentation was judged against"
        )
    sealed_grouping_digest = run_sealed_config_digests(context.run).get("designator-grouping")
    if sealed_grouping_digest is None:
        # Unsealed and mismatched are different faults with different fixes.
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold names grouping configuration digest "
            f"{grouping_digest!r}, but this run sealed no designator-grouping digest at all "
            "for it to be judged against; the bound behind a held page cannot be bound to a "
            "policy the run never named"
        )
    if grouping_digest != sealed_grouping_digest:
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold names grouping configuration digest "
            f"{grouping_digest!r}, which is not the designator-grouping digest "
            f"{sealed_grouping_digest!r} this run sealed at binding time; a Designator free to "
            "invent the grouping policy behind its bound could hold any page it likes"
        )
    _prove_page_wide_act_rectangle(
        context, act_id, row["page_id"], ordinal, bounds, "page-residual"
    )
    inputs = hold.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not reference exactly one conservation "
            "artifact to check its premise against"
        )
    conservation = context.tree.read_artifact_reference(
        inputs[0], stage=DESIGNATOR, kind="conservation", subject_id=row["page_id"]
    )
    _verify_page_residual_premise(act_id, row["page_id"], payload, conservation)


def _verify_page_residual_premise(
    act_id: str, page_id: str, hold_payload: Mapping[str, Any], conservation: dict[str, Any]
) -> None:
    """The conservation record's own account of why this page is held as one item."""
    payload = _payload_of(conservation)
    declared = hold_payload.get("residual_component_count")
    if not _is_count(declared):
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not name an integer residual component count"
        )
    enumeration = payload.get("residual_enumeration")
    if enumeration not in (RESIDUAL_ENUMERATION_WITHHELD, RESIDUAL_ENUMERATION_AGGREGATED):
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} for withheld or aggregated residual enumeration, but that "
            f"page's own conservation record records its enumeration as {enumeration!r} rather "
            f"than {RESIDUAL_ENUMERATION_WITHHELD!r} or {RESIDUAL_ENUMERATION_AGGREGATED!r}; "
            "a page may not be held as one review item over a reconciliation that separately "
            "presents every component"
        )
    # After the enumeration check, so that refusal takes precedence.
    outcome = conservation.get("outcome")
    if outcome != "held":
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} as one review item, but that page's own "
            f"conservation record reports its outcome as {outcome!r} rather than 'held'; a "
            "record standing behind a held page may not still say it was proposed"
        )
    if enumeration == RESIDUAL_ENUMERATION_WITHHELD and "residual_components" in payload:
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} for a withheld enumeration, but that page's "
            "conservation record still carries a residual_components key; the key is omitted "
            "when it is withheld, so that no consumer reads a present list as the complete one"
        )
    measured = payload.get("residual_component_count")
    if not _is_count(measured):
        raise FatalAccounting(
            f"page {page_id}'s conservation record names no integer residual component count "
            f"for act {act_id} to be held against"
        )
    if measured != declared:
        raise FatalAccounting(
            f"act {act_id} reports {declared} residual components while page {page_id}'s own "
            f"conservation record measured {measured}; the count a reviewer is shown is the "
            "count the reconciliation took, never a second figure beside it"
        )
    if enumeration == RESIDUAL_ENUMERATION_WITHHELD:
        bound = hold_payload.get("max_residual_components")
        if not _is_count(bound):
            raise FatalAccounting(
                f"act {act_id}'s legacy withheld page-residual hold does not name the integer "
                "bound it was judged against"
            )
        if hold_payload.get("reason_code") != PAGE_RESIDUAL_REASON_CODE:
            raise FatalAccounting(f"act {act_id}'s legacy withheld page uses the wrong reason code")
    else:
        if hold_payload.get("reason_code") != PAGE_RESIDUAL_AGGREGATE_REASON_CODE:
            raise FatalAccounting(f"act {act_id}'s aggregate page uses the wrong reason code")
        bound = None
    if enumeration == RESIDUAL_ENUMERATION_WITHHELD and measured <= bound:
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} against a bound of {bound} residual components, "
            f"but that page's conservation record measured {measured}, which does not exceed it; "
            "a page whose reconciliation stays within the bound owes one held act per residual, "
            "not one held page"
        )
    if enumeration == RESIDUAL_ENUMERATION_AGGREGATED:
        aggregate = payload.get("aggregated_residual_components")
        aggregated_count = hold_payload.get("aggregated_component_count")
        if (
            not isinstance(aggregate, list)
            or not _is_count(aggregated_count)
            or aggregated_count != len(aggregate)
        ):
            raise FatalAccounting(
                f"act {act_id} holds page {page_id} for aggregate residual accounting without "
                "the retained component count"
            )


def _payload_of(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _is_count(value: Any) -> bool:
    return is_plain_int(value) and value >= 0


def _verify_residual_traces_to_conservation(
    context, act_id: str, page_id: str, hold: dict[str, Any], bounds: dict[str, Any]
) -> None:
    """A residual's declared bounds must exist in the reconciliation that found it.

    Identity alone would pass an invented residual; the hold's one input, its
    conservation record, must carry a component at those bounds.
    """
    inputs = hold.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1:
        raise FatalAccounting(
            f"act {act_id}'s hold record does not reference exactly one conservation "
            "artifact to recompute its residual from"
        )
    conservation = context.tree.read_artifact_reference(
        inputs[0], stage=DESIGNATOR, kind="conservation", subject_id=page_id
    )
    # Bounds are unique within a record (the Designator refuses coincident
    # boxes), so matching by bounds needs no tie-breaker.
    payload = conservation.get("payload")
    components = payload.get("residual_components") if isinstance(payload, Mapping) else None
    if not isinstance(components, list) or not any(
        isinstance(component, Mapping) and component.get("bounds") == bounds
        for component in components
    ):
        raise FatalAccounting(
            f"act {act_id}'s hold declares a residual the conservation record it references "
            "does not carry at those bounds; an extra row must trace to the reconciliation "
            "pass that actually found it, not merely be self-consistent with its own hold"
        )


def _designator_records_by_subject(context, kind: str) -> dict[str, dict[str, Any]]:
    """Every Designator record of one kind, by the act it is evidence for.

    A subject with two records is refused; keeping either would pick by
    manifest order (principle 1).
    """
    records: dict[str, dict[str, Any]] = {}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] != kind:
            continue
        subject = entry["subject_id"]
        if subject in records:
            raise FatalAccounting(
                f"{subject} has more than one Designator {kind} record; one subject is "
                f"evidenced once, and nothing may decide which {kind} record speaks for it"
            )
        records[subject] = context.tree.read_artifact(DESIGNATOR, kind, entry["artifact_id"])
    return records


def _proposal_evidence_by_subject(
    context, expected_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every proposal-origin region and every hold, by the act it is evidence for."""
    by_subject: dict[str, list[dict[str, Any]]] = {act_id: [] for act_id in expected_ids}
    for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]:
        if entry["kind"] not in {"region", "hold"}:
            continue
        record = context.tree.read_artifact(DESIGNATOR, entry["kind"], entry["artifact_id"])
        subject = record["subject_id"]
        if subject not in by_subject:
            raise FatalAccounting(
                f"Designator artifact {record['artifact_id']} names act {subject!r}, which the "
                "proposal denominator does not account for"
            )
        if entry["kind"] == "region" and record["payload"].get("origin") != "proposal":
            continue
        by_subject[subject].append(record)
    return by_subject


def _verify_proposal_seal_evidence(
    context,
    seal: dict[str, Any],
    acts: list[dict[str, Any]],
    *,
    by_subject: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    """Reconcile the immutable expected-act denominator to Designator evidence.

    Recovery regions are later evidence and do not change the denominator.
    """
    if by_subject is None:
        by_subject = _proposal_evidence_by_subject(context, {act["act_id"] for act in acts})

    expected_seal_refs: list[dict[str, str]] = []
    for act in acts:
        records = by_subject[act["act_id"]]
        regions = [record for record in records if record["kind"] == "region"]
        holds = [record for record in records if record["kind"] == "hold"]
        if act["outcome"] == "proposed":
            if not regions or holds:
                raise FatalAccounting(
                    f"proposed act {act['act_id']} does not reconcile to proposal-region evidence"
                )
        elif act["outcome"] == "held":
            if len(holds) != 1:
                raise FatalAccounting(
                    f"held act {act['act_id']} does not reconcile to exactly one hold record"
                )
        else:
            raise FatalAccounting(
                f"proposal seal uses unsupported current Designator outcome {act['outcome']!r}"
            )
        actual_refs = sorted(
            [
                context.artifact_ref(DESIGNATOR, record["kind"], record["artifact_id"])
                for record in records
            ],
            key=lambda reference: reference["relative_path"],
        )
        if act["evidence"] != actual_refs:
            raise FatalAccounting(
                f"proposal-seal row for {act['act_id']} does not name exactly its current "
                "proposal-region and hold evidence"
            )
        expected_seal_refs.extend(actual_refs)
    if sorted(seal["inputs"], key=lambda reference: reference["relative_path"]) != sorted(
        expected_seal_refs, key=lambda reference: reference["relative_path"]
    ):
        raise FatalAccounting(
            "the proposal seal input set does not reconcile to every expected act's evidence"
        )


def open_context(
    args,
    stage: str,
    *,
    registry_factory: Callable[[str], StageChairProtocol] = ChairRegistry.from_toml,
    tree: RunTree | None = None,
    run: Mapping[str, Any] | None = None,
) -> StageContext:
    """Open an existing run for a stage that is not the first to write.

    `tree` and `run` come together or not at all, so the route and the binding
    check use one read of `run.json`.
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
        designator_padding_config_path=args.designator_padding_config,
        designator_geometry_config_path=args.designator_geometry_config,
        designator_grouping_config_path=args.designator_grouping_config,
        alignment_config_path=args.alignment_config,
        pdf_target_dpi=args.pdf_target_dpi,
        armarium_formats_config_path=args.formats_config,
        recovery_config_path=args.recovery_config,
        hard_failure_config_path=args.hard_failure_config,
        witness_context=args.witness_context,
        witness_context_config_path=args.witness_context_config,
        nuda_per_mille=args.nuda_per_mille,
        nuda_approval_ref=args.nuda_approval_ref,
        perlector_instrument_per_mille=args.perlector_instrument_per_mille,
        perlector_instrument_approval_ref=args.perlector_instrument_approval_ref,
        perlector_protocol_config_path=args.perlector_protocol_config,
        perlector_audit_config_path=args.perlector_audit_config,
        draft_fed=args.draft_fed,
        mechanics_qualification=getattr(args, "mechanics_qualification", False),
        serving_recipes_config_path=args.serving_recipes_config,
        decoding_config_path=args.decoding_config,
    )
    if tree is None:
        tree = RunTree(Path(args.run_root), args.run_id)
        run = tree.read_run()
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
    verify_predecessor_seal(tree, stage)
    refuse_halted_run(tree, stage, args.hard_failure_config)
    return _bound_context(tree, run, fixture, args.scenario, stage, args, registry, bindings)


def open_stage_context(
    args,
    stage: str,
    *,
    registry_factory: Callable[[str], StageChairProtocol] = ChairRegistry.from_toml,
) -> StageContext:
    """Open an existing run for any stage after the Door, on either ingress route.

    One read of `run.json` decides the route and is passed down, never re-read.
    """
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    if not is_real_ingress(run):
        return open_context(args, stage, registry_factory=registry_factory, tree=tree, run=run)
    return _open_real_context(args, stage, tree, run, registry_factory)


def _open_real_context(
    args,
    stage: str,
    tree: RunTree,
    run: Mapping[str, Any],
    registry_factory: Callable[[str], StageChairProtocol],
) -> StageContext:
    """Open a real submission's run for a stage after the Door.

    Mirrors `open_context`'s checks in the same order.  The real
    `config_digest` is not recomputed: it binds the Door's inputs and decoder
    versions, and re-binding those would refuse a sound run after a library
    upgrade.  The sealed map is rechecked name by name instead.
    """
    verify_snapshot_is_current(run, args.corpus_register)
    read_snapshot(tree, run)
    registry = _open_registry(args, registry_factory)
    bindings = real_run_bindings(registry.config, args)
    # Before the seal check, so a moved policy is named as one.
    _refuse_incompatible_real_reuse(run, bindings, run_id=args.run_id)
    verify_predecessor_seal(tree, stage)
    refuse_halted_run(tree, stage, args.hard_failure_config)
    return _bound_context(tree, run, None, REAL_SCENARIO, stage, args, registry, bindings)


def _open_registry(args, registry_factory: Callable[..., StageChairProtocol]) -> StageChairProtocol:
    cache_root = getattr(args, "cache_root", None)
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
    hashes = {row.get("ledger_sha256") if isinstance(row, Mapping) else None for row in rows}
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


def exemplar_page_ids(context) -> dict[int, str]:
    """Every Exemplar page by submitted ordinal, sealed and refused alike.

    Read from the Exemplar's `page` artifacts, the only source that carries a
    container page's full identity.  Says which page an ordinal names, not
    that its bytes are sound.
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
    return dict(sorted(pages.items()))


def refuse_halted_run(tree: RunTree, stage: str, hard_failure_config_path: str | Path) -> None:
    """Apply the sealed run-level cap when no orchestrator guards stage entry."""
    run = tree.read_run()
    sealed_digests = run.get(SEALED_CONFIG_DIGESTS_FIELD)
    # Hand-built test trees may lack the policy; a real run never may.
    if not isinstance(sealed_digests, Mapping) or "hard-failure" not in sealed_digests:
        if is_real_ingress(run):
            raise ContractError(
                f"{stage} refuses to start: this real run authority seals no hard-failure "
                "configuration digest, so its run-level cap cannot be proven"
            )
        return
    policy = load_hard_failure_policy(hard_failure_config_path)
    require_sealed_config(run_sealed_config_digests(run), "hard-failure", policy["config_sha256"])
    # Inputs are not verified here, so lineage damage is reported by the
    # boundary that owns it.
    tally = tally_hard_failures(tree, policy, verify_inputs=False)
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

    The one place "current" is derived.  A missing ordinal is fatal, never 0:
    a default 0 lets listing order pick the current record.
    `operation` lets the attempt id be re-derived from subject and ordinal: the
    envelope does not bind the payload's ordinal, so a forged high ordinal would
    otherwise become current.  Ordinals must run 1..N; a gap is a lost attempt
    (principle 2).
    """
    if not records:
        raise FatalAccounting(f"no {what} to derive a current outcome from")
    ordinals: dict[int, str] = {}
    for record in records:
        ordinal = record.get("payload", {}).get("attempt_ordinal")
        if not is_plain_int(ordinal):
            raise FatalAccounting(
                f"a {what} artifact carries no attempt ordinal, so which attempt is "
                "current cannot be derived. A guess here silently picks a stale record"
            )
        if ordinal in ordinals:
            raise FatalAccounting(
                f"{what} carries duplicate attempt ordinal {ordinal} in artifacts "
                f"{ordinals[ordinal]!r} and {record.get('artifact_id')!r}; a tie is not "
                "a latest attempt and may not be selected"
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
        ordinals[ordinal] = record.get("artifact_id", "<unknown>")
    if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
        raise FatalAccounting(
            f"{what} carries attempt ordinals {sorted(ordinals)}, which is not the contiguous "
            "run 1.. that append-only attempts produce; a gap is an attempt that is no longer "
            "here, and nothing is lost silently"
        )
    return max(records, key=lambda record: record["payload"]["attempt_ordinal"])


def current_recovery_request(
    tree: RunTree,
    act_id: str,
    recovery_policy: dict[str, Any],
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Return the exact request named by an act's current Recensor review.

    The review, not the request, makes it current, so request, review,
    Perlectio and policy must form one digest-checked chain.  Shared by the
    dispatcher and the Designator so neither can bypass it.
    """
    recensor_artifacts = tree.build_manifest(RECENSOR)["artifacts"]
    reviews = []
    for entry in recensor_artifacts:
        if entry["kind"] == "review" and entry["subject_id"] == act_id:
            reviews.append(tree.read_artifact(RECENSOR, "review", entry["artifact_id"]))
    review = latest_attempt(reviews, f"Recensor review of {act_id}", operation="recense")
    if review["outcome"] != "recovery-requested":
        raise ContractError(
            f"act {act_id}'s latest Recensor review is {review['outcome']!r}, not an "
            "outstanding recovery request"
        )
    review_payload = review.get("payload")
    if not isinstance(review_payload, dict):
        raise ContractError(f"recovery-requested review of {act_id} has no payload")
    request_ref = review_payload.get("recovery_request_ref")
    reading_ref = review_payload.get("perlectio_ref")
    # Not the review's own `attempt_ordinal`; the two differ.
    ordinal = review_payload.get("recovery_request_ordinal")
    if (
        not isinstance(request_ref, dict)
        or request_ref not in review.get("inputs", [])
        or not isinstance(reading_ref, dict)
        or reading_ref not in review.get("inputs", [])
        or not is_plain_int(ordinal)
        or review_payload.get("recovery_policy") != recovery_policy
    ):
        raise ContractError(
            f"recovery-requested review of {act_id} does not carry its exact request, "
            "Perlectio, ordinal, and run-bound policy"
        )
    request = tree.read_artifact_reference(
        request_ref,
        stage=RECENSOR,
        kind="recovery-request",
        subject_id=act_id,
    )
    if request_id is not None and request["artifact_id"] != request_id:
        raise ContractError(
            f"the supplied recovery request {request_id!r} is not the exact current "
            f"Recensor request for {act_id}"
        )
    request_payload = request.get("payload")
    expected_id = artifact_id(
        RECENSOR,
        "recovery-request",
        act_id,
        attempt_id(act_id, "recover", ordinal),
    )
    if (
        request["artifact_id"] != expected_id
        or request["outcome"] != "recovery-requested"
        or not isinstance(request_payload, dict)
        or request_payload.get("attempt_ordinal") != ordinal
        or request_payload.get("act_key") != review_payload.get("act_key")
        or request_payload.get("perlectio_ref") != reading_ref
        or reading_ref not in request.get("inputs", [])
        or request_payload.get("recovery_policy") != recovery_policy
    ):
        raise ContractError(
            f"recovery-requested review of {act_id} does not match its exact request, "
            "Perlectio, and policy"
        )
    recovery_kind = request_payload.get("recovery_kind")
    if (
        not isinstance(recovery_kind, str)
        or recovery_kind not in RECOVERY_KINDS
        or review_payload.get("recovery_kind") != recovery_kind
    ):
        raise ContractError(
            f"recovery-requested review of {act_id} does not carry one exact recovery kind"
        )
    # Counters rebuilt: a self-hash proves no later edit, not that they ever
    # agreed with earlier requests.
    reconcile_recovery_requests(
        [
            tree.read_artifact(RECENSOR, "recovery-request", entry["artifact_id"])
            for entry in recensor_artifacts
            if entry["kind"] == "recovery-request" and entry["subject_id"] == act_id
        ],
        act_id,
        recovery_policy,
    )
    kind_allowed = recovery_kind_budget(recovery_policy, recovery_kind)
    kind_used = request_payload.get("kind_budget_used")
    if (
        request_payload.get("kind_budget_allowed") != kind_allowed
        or not _is_count(kind_used)
        or kind_used >= kind_allowed
    ):
        raise ContractError(
            f"recovery-requested review of {act_id} does not carry a usable {recovery_kind!r} "
            "budget boundary"
        )
    tree.read_artifact_reference(
        reading_ref,
        stage=PERLECTOR,
        kind="perlectio",
        subject_id=act_id,
    )
    return request


def reading_basis_regions(reading: dict[str, Any], what: str) -> list[dict[str, Any]]:
    """Return a completed Perlectio's regions without trusting an untyped payload."""
    payload = reading.get("payload")
    if not isinstance(payload, dict):
        raise FatalAccounting(f"{what} has no object payload")
    basis = payload.get("basis")
    if not isinstance(basis, dict):
        raise FatalAccounting(f"{what} has no object basis for its completed reading")
    regions = basis.get("regions")
    if not isinstance(regions, list) or not regions:
        raise FatalAccounting(f"{what} has no non-empty region basis for its completed reading")
    for index, region in enumerate(regions):
        if not isinstance(region, dict) or not isinstance(region.get("image_path"), str):
            raise FatalAccounting(
                f"{what} has malformed basis region {index}; a completed reading must name "
                "the crop bytes it read"
            )
    return regions


def recovery_region_count(act_id: str, regions: list[dict[str, Any]]) -> int:
    """How many recovery crops one act carries, refusing an unplaceable origin.

    Shared by three stages so they cannot disagree about an unknown origin.
    """
    count = 0
    for region in regions:
        payload = region.get("payload")
        if not isinstance(payload, dict):
            raise FatalAccounting(f"Designator region of {act_id} has no object payload")
        origin = payload.get("origin")
        # `isinstance` first: an unhashable origin would raise TypeError.
        if not isinstance(origin, str) or origin not in {"proposal", "recovery"}:
            raise FatalAccounting(
                f"Designator region of {act_id} has unrecognized origin {origin!r}; its "
                "place in the recovery denominator is unknown"
            )
        if origin == "recovery":
            count += 1
    return count


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


def require_current_witness_basis(
    act_id: str,
    reading: dict[str, Any],
    testimonia: list[dict[str, Any]],
    what: str,
) -> None:
    """Refuse a reading whose witness basis is no longer each chair's current attempt.

    A Testimonium appended after the reading would otherwise be invisible to
    the export's `complete` (principle 2).  Independent of the Attestatores'
    own guard, for trees assembled some other way.
    """
    basis = reading.get("payload", {}).get("basis")
    cited = basis.get("testimonia") if isinstance(basis, dict) else None
    if not cited:
        return
    if not isinstance(cited, list):
        raise FatalAccounting(f"{what} has a malformed witness basis: testimonia is not a list")
    current = {
        record["payload"]["chair"]: record["artifact_id"]
        for record in latest_per_chair(testimonia, f"testimonium for {act_id}")
    }
    superseded = []
    for item in cited:
        if not isinstance(item, dict):
            raise FatalAccounting(f"{what} has a non-object witness basis entry")
        chair, artifact = item.get("chair"), item.get("artifact_id")
        if not isinstance(chair, str) or not isinstance(artifact, str):
            raise FatalAccounting(f"{what} has an untyped witness basis entry")
        if chair not in current:
            raise FatalAccounting(
                f"{what} cites chair {chair!r}, which has no current Testimonium on this act"
            )
        if current[chair] != artifact:
            superseded.append(chair)
    if superseded:
        raise FatalAccounting(
            f"{what} was established from Testimonium that chair(s) {sorted(superseded)} have "
            "since superseded; the reading has not been reconciled against the current "
            "witness evidence, and a superseded basis may not be carried past this stage as "
            "though it were current"
        )


def act_by_key(fixture: dict[str, Any], key: str) -> dict[str, Any]:
    for act in fixture["act"]:
        if act["key"] == key:
            return act
    raise ContractError(f"the fixture declares no act {key!r}")


def acts_for_page(fixture: dict[str, Any], page_ordinal: int) -> list[dict[str, Any]]:
    return [act for act in fixture["act"] if act["page_ordinal"] == page_ordinal]


def continuation_for(fixture: dict[str, Any], act_key: str) -> dict[str, Any] | None:
    """The continuation region declared for an act, if it has one."""
    for continuation in fixture.get("continuation", []):
        if continuation["act_key"] == act_key:
            return continuation
    return None
