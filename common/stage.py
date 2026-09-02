"""What every stage program needs, and nothing a stage should decide for itself.

Stages are programs, not libraries: the orchestrator invokes them by file path and
they exchange versioned artifacts on disk. So this module holds the plumbing they
all share — argument shape, opening the run tree, publishing an artifact with the
envelope filled in correctly — and deliberately holds no pipeline logic at all. A
stage that found its behaviour here would be importing another stage through a
side door.

The fixture is read as *data*, with tomllib, not imported as code. That keeps the
import boundary honest while still letting the deterministic fakes be driven by a
declared fixture rather than by hard-coded strings scattered through seven files.
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
from common.contracts.approval import REAL_INGRESS, parse_ingress_record
from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, verify_self_hash
from common.contracts.envelope import build_envelope, verify_input_bytes
from common.contracts.errors import (
    ContractError,
    FatalAccounting,
    IdentityRefusal,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import act_bindings, artifact_id, attempt_id
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
    CHAIR_CALL_RECORD_SCHEMA,
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
from common.decoding import DEFAULT_DECODING_CONFIG_PATH, load_decoding_policy
from common.exemplar_boundary import verify_sealed_page_pixels
from common.hard_failure import (
    DEFAULT_HARD_FAILURE_CONFIG_PATH,
    load_hard_failure_policy,
    tally_hard_failures,
)
from common.imaging import dimensions
from common.recovery import (
    DEFAULT_RECOVERY_CONFIG_PATH,
    RECOVERY_KINDS,
    load_recovery_policy,
    reconcile_recovery_requests,
    recovery_kind_budget,
)
from common.runtree.store import PublishResult, RunTree
from common.witness_adapters import validate_witness_adapter_bindings

# Exit codes carry cause, per harvest invariant #11. The old contract worth
# keeping: 0 = complete, 2 = structural or fatal, 3 = accounted but holdable.
# A stage that failed structurally and exited 0 is how a run reports success over
# work it never did.
EXIT_COMPLETE = 0
EXIT_FATAL = 2
EXIT_HELD = 3

# A stage returns this only when it refuses to start a run whose durable failure
# evidence already breaches the cap.  The orchestrator also returns it when a
# checkpoint after a completed member breaches the cap.  Defined beside the three
# ordinary stage exits so direct invocation cannot silently choose a colliding
# fourth value.
EXIT_RUN_HALTED = 4


class RunHalted(ContractError):
    """The run-level hard-failure cap refuses another stage entry."""


DEFAULT_PDF_RENDER_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "pdf_render.toml"
DEFAULT_WITNESS_CONTEXT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "witness_context.toml"
)
DEFAULT_PERLECTOR_PROTOCOL_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "perlector_protocol.toml"
)
DEFAULT_PERLECTOR_AUDIT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "perlector_audit.toml"
)

# Spec 08's run-level blind/named toggle, from Tyrel's 2026-07-30 ruling
# (courtroom_doctrine.md, formalized in spec_08 — not ARCHITECTURE.md, which
# does not define this regime). Named here once, so the CLI flag, the
# config-digest binding, every stage's shared parser and the Perlectio schema
# agree on the closed set: a value added in one place and missed in another
# would let a run start under a regime every Perlectio it produced is then
# refused for. It is provenance, so invariant #42 governs it.
WITNESS_CONTEXT_REGIMES: Final = ("named", "blinded")
MAX_NUDA_PER_MILLE: Final = 1000
MAX_PERLECTOR_INSTRUMENT_PER_MILLE: Final = 1000
# These sealed CLI values identify experiments, not approval evidence. A changed
# condition or selection algorithm needs a new `.v1` subject; a changed rate or
# run configuration needs a new approval targeting the resulting `config_digest`.
# Resolution happens after the run authority exists to avoid circularly including
# an approval record's own content address in the configuration it approves.
NUDA_APPROVAL_SUBJECT: Final = "lectio-nuda-sampling-design.v1"
PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT: Final = "perlector-prior-draft-instrument-design.v1"

# The one scenario name a real submission runs under. A constant, never argv:
# the fixture path seals `--scenario` into `config_digest` and refuses a resumed
# run under another, but the real `config_digest` binds no scenario at all, so
# an argv value there would be the one run-shaping fact nothing ever checked.
# Every real-ingress context carries this and nothing else.
REAL_SCENARIO: Final = "real-submission"

# The real Exemplar Door decodes/renders bytes; it is not the walking skeleton's
# fake adapter. Bumped deliberately whenever source behaviour changes so a real
# run cannot resume under pixels made by a different Door implementation. Named
# here rather than in `pipeline/1_exemplar/door.py`, because the downstream
# open-time recheck (`_refuse_incompatible_real_reuse`) has to compare the run
# authority's Door recipe against it, and `common/` may never import a stage.
# The Door imports it from here.
REAL_DOOR_ADAPTER_REVISION: Final = "exemplar-door-v5"

# The Designator's capture padding decides how many pixels a witness is actually
# shown around each act, so two runs under different padding produce different
# crop bytes. Sealing its digest here is what makes reusing one run id across a
# padding change a refusal rather than a silent second geometry — the same
# reason `pdf_render.toml` is sealed.
DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "designator_padding.toml"
)
DEFAULT_DESIGNATOR_GEOMETRY_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "designator_geometry.toml"
)
# The grouping and reconciliation thresholds the Designator's structure pass runs
# under: which marks join into one act, how far a chain reaches, and how many
# residual components one page's conservation record may enumerate before the page
# is held as a single review item. They decide what is marked out and what is held,
# so two runs under different thresholds produce different acts from identical
# pixels — the same reason padding is sealed, one step earlier in the same stage.
DEFAULT_DESIGNATOR_GROUPING_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "designator_grouping.toml"
)
DEFAULT_CORPUS_FRAME_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "corpus_frame.toml"
)
DEFAULT_SERVING_RECIPES_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "serving_recipes.toml"
)
DEFAULT_POD_PLACEMENT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "pod_placement.toml"
)
DEFAULT_TRIAGE_MODES_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "triage_modes.toml"
)
MAX_TRIAGE_MODES_CONFIG_BYTES: Final = 64 * 1024


def _read_triage_modes_config(path: str | Path) -> bytes:
    """Read one bounded config body through the descriptor that supplied it."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(MAX_TRIAGE_MODES_CONFIG_BYTES + 1)
    except OSError as error:
        raise ContractError(f"triage modes configuration at {path} could not be read") from error
    if len(raw) > MAX_TRIAGE_MODES_CONFIG_BYTES:
        raise ContractError(
            "triage modes configuration at "
            f"{path} exceeds the {MAX_TRIAGE_MODES_CONFIG_BYTES}-byte limit"
        )
    return raw


def _validate_triage_modes_config(raw: bytes, path: str | Path) -> None:
    """The closed triage-mode schema, checked in one place.

    `run_config_bindings` seals the digest of these bytes into `config_digest` and
    `require_triage_modes` rechecks them at the point of use; before this was
    shared, only the second one parsed. A file declaring `[automatic]` therefore
    sealed cleanly into `run.json` and the run walked several stages before the
    first triage recheck refused it — a run tree that looks legitimate and can
    never complete. The same validator on both sides means the binding cannot
    admit a vocabulary the recheck will reject.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError(f"triage modes configuration at {path} is not valid UTF-8") from error
    try:
        record = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ContractError(f"triage modes configuration at {path} is not valid TOML") from error
    if set(record) != set(TRIAGE_MODES) or any(
        not isinstance(policy, dict)
        or set(policy) != {"review_at_or_below_confidence"}
        or not isinstance(policy["review_at_or_below_confidence"], int)
        or isinstance(policy["review_at_or_below_confidence"], bool)
        or not 0 <= policy["review_at_or_below_confidence"] <= 4
        for policy in record.values()
    ):
        raise ContractError("triage modes configuration has the wrong closed schema")


# The witness outcomes that mean a chair actually served, and therefore that a
# serving receipt exists for the reading. Named once, here, because both halves
# of the handoff need it and they must not drift: the Attestatores decides
# whether to write a receipt, and the Perlector decides whether to demand one.
# A producer and a consumer disagreeing about this set would refuse a record
# that is in fact correct — `dead` and `not-run` are unresolved or unattempted,
# and inventing a serving moment for either would be a receipt for nothing.
ATTEMPTED_WITNESS_OUTCOMES = frozenset({"read", "genuinely-empty", "failed"})

# A failed call was attempted but produced no usable reading, so it must not
# certify that its regions were witnessed.  A genuinely-empty Testimonium is
# different: the chair did read the pixels and found no reportable text.  The
# Perlector uses this narrower set when it records region coverage.
#
# Re-exported from the vocabulary module rather than spelled a second time.  R0's
# floor arithmetic (`common/contracts/outcomes.py::witness_coverage`) and this
# module's writers and consumers have to agree on this exact set or an act is
# attached without being read; two identical literals in two files agreed only by
# coincidence.  Found in audit; F-O3.
WITNESS_READING_OUTCOMES = _WITNESS_READING_OUTCOMES

# One closed vocabulary for staged driver selections and the console that
# presents them.  Selection remains an invocation choice, never run-tree bytes.
RUN_MODES: Final = TRIAGE_MODES

# Attestatores can return EXIT_HELD before the driver consults `mode`, after it
# has written its completion seal. Armarium's own terminal report can do the
# same: `run_sequence`'s tail returns `EXIT_HELD` for any non-complete report
# whenever the selection's last member is armarium, with no reference to
# `mode` at all -- found in this unit's own security review, F-R21C1, because
# the very AST scan this comment used to cite for "cannot drift silently" only
# recognises an `if name == ...: return EXIT_HELD` branch and had no shape for
# a bare tail ternary, so armarium had silently drifted out of this set.
# `test_advance_modes.py` derives the branch-shaped half of this set from the
# driver's own syntax and asserts the ternary shape by source literal for the
# other half, so both halves of this cross-module claim stay checked.
ALWAYS_HELD_BOUNDARIES: Final = frozenset({ATTESTATORES, ARMARIUM})


def _named_boundary(name: str, role: str) -> str:
    """Refuse a selection endpoint that owns no stage completion boundary.

    `recovery` is a legal driver member (`--from designator --to recovery`
    parses) and owns no stage program and no seal, so it is refused here by the
    same sentence a typo is: what matters to a person at the console is which
    names *do* carry a boundary, not which list the name failed to be in.
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
    """Return every boundary a selected invocation can stop at, judging no evidence.

    This is a claim about the *driver*, not about the run tree: it says where a
    person can be waiting, never whether one is or whether they should advance.
    """

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
    # Named, not reached by falling through. `RUN_MODES` is `TRIAGE_MODES`, a
    # vocabulary that grows in `common/contracts/stages.py` for the triage
    # manifest's sake; a mode added there would arrive here as a range mode and
    # the operator would be told semi mode needs a range it never had.
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


# Every top-level field a reading's model provenance may carry. A closed set,
# because invariant #42 refuses *wrong-schema* provenance rather than a list of
# fields we already know are wrong: a denylist passes anything a later stage
# invents, and an unvalidated field inside a sealed reading is exactly what #42
# exists to stop. `absence` appears only on an absent chair and the identity
# fields only on a configured one; which combination is legal is decided in
# `validate_serving_provenance`, not here.
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

# The structure chair's serving posture, carried on every artifact the
# structural pass produces. It is the one provenance field that says a model was
# *asked something* rather than that a chair was resolved: `receipt_ref` names
# the serving moment a chair was launched under, and on the fixture path that
# receipt is a declared value over a chair nothing called
# (`fixture_serving_details`). A run whose Designator actually served
# `designator_structure` says so here, once, in a closed shape.
#
# Not a per-page call record. The structure pass makes one call per sealed page
# and each page's own `structure-answer` record names that call
# (`call_record_ref`, SPEC_D §1.3); what travels on provenance is the posture
# every one of those calls ran under, which is the part a consumer can check
# without opening any of them.
STRUCTURE_CALL_SCHEMA: Final = "structure-chair-call.v1"
STRUCTURE_CALL_FIELDS: Final = frozenset(
    {"schema", "call_kind", "decoding_policy", "decoding_config_sha256"}
)
# The one wire kind a vision chair is served through
# (`operations/serving/client.py` refuses anything else by name). Recorded so a
# later reader can tell what the chair was asked through without inferring it
# from the shape of what came back.
STRUCTURE_CALL_KIND: Final = "chat-completions"
# The `config/decoding.toml` section the structure pass runs under. Tyrel ruled
# on 2026-09-02 that the Designator's structure pass may run at a variable
# temperature, sealed and recorded per run, so its re-run variance is a clue
# beside the witnesses, while every Attestator keeps `reading_of_record`. The
# two postures are different sealed sections, so naming the wrong one is a
# posture reported rather than executed.
STRUCTURE_DECODING_POLICY: Final = "structure"

# The Designator's per-page record of what the structure chair answered, and the
# two words a consumer needs to reach one: the artifact kind and the payload
# schema. Named here, beside the verifier that recomputes against them, for the
# reason `fallback_page_act_key` is — the producer writes them and this module
# reads them back, so the two may not spell them differently.
STRUCTURE_ANSWER_KIND: Final = "structure-answer"
STRUCTURE_ANSWER_RECORD_SCHEMA: Final = "designator-structure-answer.v1"
STRUCTURE_ANSWER_PARSED: Final = "parsed"


class StageChairProtocol(ChairProtocol, Protocol):
    """The small additional config surface a calling stage needs.

    A stage receives this explicitly rather than knowing which registry
    implementation made it.  The production default is ``ChairRegistry``; the
    deterministic fake beside the tests is a separate implementation of this
    protocol, not a subclass or import of it.
    """

    config: ModelsConfig


# The name every run authority records its point-of-use recheck digests under.
# Recorded rather than only folded into `config_digest`, because a digest that
# exists only inside a hash can be *verified* against a candidate file and never
# *named*: a later reader holding the run tree alone could not say which
# data-handling policy governed admission (CodeRabbit CF01), which
# `config/README.md` stated as a limitation of the run authority rather than of
# the reader. The map is small, immutable, self-hashed with the rest of the
# authority, and every entry in it is already bound into `config_digest`, so it
# adds a readable name for a fact the run was already sealed to.
SEALED_CONFIG_DIGESTS_FIELD: Final = "sealed_config_digests"


def require_sealed_config(
    sealed_config_digests: Mapping[str, str],
    name: str,
    observed_sha256: str,
    owner: str = "this run",
) -> None:
    """Refuse a configuration whose bytes changed after this run bound them.

    The one comparison behind every point of use in the sealing family. A stage
    holds it through `StageContext.require_sealed_config`; the orchestrator, which
    is not a stage and has no context, holds it through the digests `run.json`
    itself recorded (`run_sealed_config_digests`).

    An absent name is a different fault from a changed file and says so: one means
    the binding step never sealed this policy, the other means the file moved
    under a run that did.
    """
    sealed = sealed_config_digests.get(name)
    if sealed is None:
        raise ContractError(
            f"{owner} sealed no digest for the {name} configuration, so the bytes "
            "a stage just read cannot be proven to be the ones this run is bound to"
        )
    if sealed != observed_sha256:
        raise ContractError(
            f"the {name} configuration changed between this run's binding check and the "
            f"read that used it: bound {sealed}, read {observed_sha256}. A stage may not "
            "work under a policy the run never sealed"
        )


def run_sealed_config_digests(run: Mapping[str, Any]) -> dict[str, str]:
    """The point-of-use recheck digests a run authority recorded for itself.

    Refused rather than defaulted to an empty map: "this run sealed nothing" and
    "this run sealed something I have not read" must not resolve the same way,
    and an empty map would turn every `require_sealed_config` below it into the
    absent-name refusal with a message pointing at the wrong step.
    """
    recorded = run.get(SEALED_CONFIG_DIGESTS_FIELD)
    if not isinstance(recorded, dict) or not recorded:
        raise ContractError(
            "this run authority records no sealed configuration digests, so nothing here "
            "can prove which policy bytes governed it; a run created before the sealing "
            "family landed cannot be continued under a point-of-use recheck"
        )
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
        # `None` on a real submission, behind the refusing `fixture` property
        # below; a loaded declaration on the fixture path. The parameter keeps
        # its name so every existing construction is untouched.
        self._fixture = fixture
        self.scenario = scenario
        self.stage = stage
        self.adapter_revision = adapter_revision
        self.args = args
        self.registry = registry
        # The digest of each configuration file's bytes *as they were when this
        # context checked them against `run.json`*. A stage that later re-reads
        # one of those files to get its values reads it a second time, and the
        # two reads are not the same act: between them the file can change, and
        # the stage would then work under a policy the run never sealed while
        # every other check still passed. `require_sealed_config` is the
        # point-of-use comparison that makes the second read prove it saw the
        # first read's bytes. Both ingress routes carry it: `open_context` on a
        # fixture run and `_open_real_context` on a real one, where five stages
        # require these names before their first line of work.
        self.sealed_config_digests = dict(sealed_config_digests or {})
        # A stage that opens a fixture run receives the already-parsed values
        # from the exact bytes that participated in its sealed config digest.
        # In particular Armarium must not reopen formats.toml after this point.
        self.armarium_formats = armarium_formats
        self.serving_config_inputs = (
            MappingProxyType(_serving_config_inputs(serving_config_inputs, "StageContext"))
            if serving_config_inputs is not None
            else None
        )
        # The bounded recovery policy, already parsed from the exact bytes whose
        # digest went into this run's `config_digest`. Carried rather than re-read
        # for the reason the whole sealing family exists: the Recensor and the
        # Designator recovery pass used to open `config/recovery.toml` a second
        # time for the budget they published, and a rewrite landing between
        # `open_context`'s binding read and theirs sealed reviews and requests
        # under an allowance the run never bound (audit S3). One read, one
        # policy, and `require_sealed_config("recovery", ...)` at each point of
        # use so a reintroduced second read cannot pass silently.
        self._recovery_policy = dict(recovery_policy) if recovery_policy is not None else None
        self.sealed = False

    @property
    def fixture(self) -> dict[str, Any]:
        """The declared synthetic fixture, or a named refusal on a real submission.

        `None`, not `{}` and not a Mapping-shaped sentinel: the failure this
        guards against is `context.fixture.get("act", [])` returning `[]` on a
        real run and a fixture-shaped check *passing* over it. Anything that
        supports `.get` reproduces exactly that. Refusing at first touch turns
        every unconverted fixture reader in the pipeline into a refusal that
        names the stage, and makes the audit mechanical: `rg 'context\\.fixture'`
        is the complete list of readers a real-mode branch had to decide about.
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

        A context built without one refuses rather than handing back a `None` a
        caller would index into: a missing budget must not read as a zero budget,
        which is exactly the value the S3 policy swap produced in published
        reviews before the file was read only once.
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

        Read straight off this process's own parsed CLI flag rather than off
        `run.json`: the flag is what `run_config_bindings` folded into
        `config_digest`, and `open_context`'s existing IncompatibleReuse check
        already refuses a resumed run supplied a different value, so there is
        no separate run-authority copy for this property to disagree with.
        """
        return self.args.witness_context

    @property
    def witness_context_config_path(self) -> str:
        return self.args.witness_context_config

    @property
    def nuda_per_mille(self) -> int:
        """The sealed Lectio nuda sampling rate, in thousandths. See `witness_context`."""
        return self.args.nuda_per_mille

    @property
    def nuda_approval_ref(self) -> str:
        """The sealed selector for the sampling design this run draws nuda under.

        Empty when nothing is sampled. `run_config_bindings` requires the exact
        recognized selector for a non-zero rate; the Perlector later resolves
        that selector to Tyrel's typed approval-record reference in the run tree.
        """
        return self.args.nuda_approval_ref

    @property
    def perlector_instrument_per_mille(self) -> int:
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

        The manifest is deliberately only an input to the calculation: the
        stored seal is the evidence, and a missing stored seal named by a prior
        manifest is a refusal rather than an invitation to recreate history.
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
        # A restart that did not change any stage-owned evidence reuses the
        # previous witnessed statement instead of manufacturing a new attempt.
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
        # Decode environment is excluded from the deterministic inventory, so
        # it is safe to publish before the seal and does not create a fixpoint.
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

        A receipt is deliberately outside stage artifacts because it contains the
        serving endpoint and start moment. The stage receives only this immutable
        reference plus the resolved identity it already holds. Reverification and
        receipt construction come first on every reading. The run-tree writer
        reuses only byte-identical receipts; it never looks up an older receipt by
        model identity, because a restarted endpoint can have different serving
        facts even when the model pin is unchanged.
        """
        self.registry.ensure(identity)
        receipt = self.registry.receipt(identity, serving)
        reference, _ = self.tree.write_run_receipt(receipt)
        return reference.to_record()

    def write_serving_launch_audit(self, audit: dict[str, Any]) -> dict[str, str]:
        """Store serving-manager operational evidence as a run-local blob.

        ``chair-serving-receipt.v1`` is a deliberately closed record. The
        serving manager therefore writes PID/profile/package/readiness/adapter
        facts separately, under the current stage's content-addressed blob area,
        and passes only this immutable reference beside the receipt.
        """

        if not isinstance(audit, dict) or not audit:
            raise SchemaRefusal("serving launch audit must be a non-empty object")
        if self.serving_config_inputs is None:
            raise SchemaRefusal(
                "StageContext has no run-sealed serving configuration inputs; "
                "construct serving through open_context"
            )
        observed_inputs = _serving_config_inputs(
            audit.get("configuration_inputs"), "serving launch audit"
        )
        if observed_inputs != dict(self.serving_config_inputs):
            raise SchemaRefusal(
                "serving launch audit configuration inputs differ from the run-sealed inputs"
            )
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
        observed_inputs = _serving_config_inputs(
            audit.get("configuration_inputs"), "serving launch audit"
        )
        if observed_inputs != dict(self.serving_config_inputs or {}):
            raise SchemaRefusal(
                "serving launch audit configuration inputs differ from the run-sealed inputs"
            )
        return audit

    def _write_serving_blob(self, value: dict[str, Any], label: str) -> dict[str, str]:
        """Canonical content-addressed storage shared by serving evidence records.

        Guarded after the seal for the same reason `publish` is, and it is the
        same directory at stake: this writes through `tree.put_blob` into the
        stage's own blob directory, which `_stage_blob_inventory` walks and whose
        digest the seal payload carries. One serving-evidence write afterwards
        changes the inventory the seal witnessed — and the symptom lands on the
        wrong stage, because the *next* consumer refuses with "its named inventory
        no longer matches disk". A producer that did honest work would be reported
        as a tree whose evidence was altered, indistinguishable from real
        tampering. Ordering discipline was already judged insufficient for
        artifacts; blobs are no different.

        Serving *receipts* need no such guard: they are run receipts written under
        `receipts/`, outside any stage's inventory.
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
        from common.contracts.canonical import digest_bytes

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
# Every stage that decodes or transforms image bytes in its own pass must seal
# ``produced_pixels: true``; DAI makes Attestatores such a stage.
_PIXEL_STAGES: Final = frozenset(
    {"door", "exemplar", "ink-map", "designator", "attestatores", "perlector", "recensor"}
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

    ``build_manifest`` records a content digest, but a later consumer used to
    discard it and reopen the path by identity.  A replacement between those two
    reads was therefore valid evidence in isolation yet not the evidence the
    manifest and completion seal had checked.  Compare the bytes represented by
    the validated record to the snapshot digest before returning the record.
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

    Compared as the SET of seals the stored inventory names against the set on
    disk, never as "some seal is still there". Attempts are the contiguous run
    1..N, so removing the *latest* of several leaves a prefix `latest_attempt`
    reads as whole: the earlier statement then answers for a boundary it never
    witnessed, and the ordinal the deletion vacated is minted a second time over
    a different inventory. Refusing only total deletion misses every case where
    an earlier seal remains to answer in the deleted seal's place.

    `present` is passed in rather than walked here: both callers have already
    built the seal records they are about to reason about, and a second walk of
    the same directory is the per-boundary cost this check does not need to add.
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
    # Stages that only pass evidence through record `none`; readers/croppers
    # record the closed route family they use. The Door can take both library
    # routes, which is deliberately visible rather than guessed from suffixes.
    paths = {
        "door": {"pillow", "pdfium"},
        "exemplar": {"project-png"},
        # `grayscale_rows` owns its Pillow fallback inside the project-PNG route.
        # Naming that fallback as a second route would manufacture decoder drift
        # at both boundaries around the Ink Map.
        "ink-map": {"project-png"},
        "designator": {"project-png"},
        # Decode paths name the project-owned deterministic codec route; the
        # executable presentation transform separately names its resampler.
        "attestatores": {"project-png"},
        "perlector": {"project-png"},
        "recensor": {"project-png"},
    }.get(stage, {"none"})
    if not paths <= _DECODE_PATHS:
        raise SchemaRefusal(f"{stage} records an unknown decode path")
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
    """Require the consult's closed decode-environment record before comparing it."""
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
    # A completion boundary is the last point at which this producer can prove
    # every link it records still reaches the bytes it consumed.  Skipping input
    # verification here let a changed upstream blob be sealed into a locally
    # self-consistent inventory: the artifact bytes had not changed, only the
    # evidence their input digest named had.
    #
    # `manifest` is the caller's already-built inventory, passed so one reader
    # does not walk the same stage twice. A supplied manifest carries whatever
    # verification its builder asked for; `verify_inputs` governs the one built
    # here, and its default stays the verifying one.
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
    # Read the authority once, and refuse a missing binding by name. `read_run`
    # proves a run authority's self-hash, schema, and run id; it does not require
    # any particular field, so an authority not written by `RunTree.create` can
    # reach here whole and still be missing one. Subscripting it raised a bare
    # KeyError, which is neither a ContractError nor a RunHalted — so `run_stage`
    # did not turn it into one of the four honest exit codes, and the operator was
    # handed a traceback naming a dict key instead of a refusal naming the run.
    # The verifier at `_verify_stage_seal` already reads both fields with `.get`
    # and tolerates their absence; this side now agrees with it.
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

    Blob names are claims about their content.  The inventory therefore refuses
    an unexpected spelling, a symlink or hard link, a name/content mismatch, and
    a file whose identity or metadata changes while it is hashed.  Hashing the
    descriptor in chunks also keeps a large but legitimate image from being
    copied wholesale into memory merely to witness it.
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
        if not stat.S_ISDIR(opened_directory.st_mode) or (
            opened_directory.st_dev,
            opened_directory.st_ino,
        ) != (named_directory.st_dev, named_directory.st_ino):
            raise SchemaRefusal(f"{stage} blob inventory is not one contained directory")
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        inventory = []
        folded_names: dict[str, str] = {}
        for name in names:
            # Case-fold collisions are judged before anything else: two names that
            # differ only by case give one tree two different inventories on a
            # case-sensitive and a case-insensitive filesystem, and neither name can
            # be trusted once both are present.
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
                    # `RunTree.put_blob` writes `.<digest>.tmp-<unique>` and then
                    # hard-links it to its evidence name. SIGKILL between the two
                    # leaves the private name behind. That orphan is interrupted
                    # writer state, not published evidence for a completion seal to
                    # witness -- but it is skipped only after it is proven to be a
                    # plain regular file, so the exception is never a way to smuggle
                    # a link past the no-follow rule.
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
        named_directory = os.stat(blobs_root, follow_symlinks=False)
        if (opened_directory.st_dev, opened_directory.st_ino) != (
            named_directory.st_dev,
            named_directory.st_ino,
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
    """True only for the store's private, same-directory blob-write name.

    ``RunTree.put_blob`` publishes a sha256-named file through
    ``.<digest>.tmp-<unique>`` and an atomic hard link.  SIGKILL can leave the
    private name behind before the link gives those bytes their evidence name.
    Every other unexpected name stays a refusal, so this exception can never be
    the route by which something unexplained sits in the evidence directory.
    """
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

    `RunTree._atomic_create` publishes by hard-linking `.<digest>.tmp-<unique>`
    onto the digest name and then unlinking the temporary. SIGKILL between those
    two steps leaves the published blob with a second link, and the only other
    name holding it is that temporary. The bytes are complete, the digest matches
    the name, and the inventory already skips the temporary itself — so refusing
    the blob for its link count meant a run killed at the wrong microsecond could
    never seal again, with the message accusing evidence that was in fact intact.

    Each same-inode temporary bearing this blob's own digest explains exactly one
    link. Every other link is still unexplained and still a refusal: this widens
    the rule by precisely the state the publisher can leave and by nothing else.
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
        if (sibling.st_dev, sibling.st_ino) == identity:
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
            identity = (opened.st_dev, opened.st_ino)
            # One link for the evidence name, plus any the interrupted publisher
            # left behind under its own private temporary name.
            allowed_links = 1 + _publisher_link_allowance(directory_fd, siblings, name, identity)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink > allowed_links
                or identity != (named_before.st_dev, named_before.st_ino)
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
        identity != (closed_over.st_dev, closed_over.st_ino)
        or identity != (named_after.st_dev, named_after.st_ino)
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
    """Prove the Armarium boundary, whose lack of a successor leaves no stage reader.

    ``SEAL_PREDECESSORS`` is consumer-keyed, so asking it about Armarium would
    re-prove the Archetypus boundary instead of reading Armarium's own seal.
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
    # The producer refuses a seal its own stored inventory names and disk no
    # longer holds; so must the consumer, which is the side an attacker with the
    # tree reaches without ever invoking the producer again. Without this,
    # deleting the latest of several seals and reverting the change it witnessed
    # leaves the earlier seal answering for a boundary it never saw.
    _refuse_deleted_seal(tree, producer, {record["artifact_id"] for record in seals})
    seal = latest_attempt(seals, f"{producer} stage seal", operation="seal")
    payload = seal["payload"]
    expected_id = artifact_id(producer, "decode-environment", producer, seal["attempt_id"])
    if payload.get("decode_environment_artifact_id") != expected_id:
        raise SchemaRefusal(
            f"{reader} refuses {producer} stage-seal: wrong decode environment name"
        )
    # One read, both comparisons. Two reads can straddle a rewrite, and a seal
    # that matched no single run authority would pass this boundary -- the same
    # two-read fault this file already names for `pdf_render.toml` and
    # `recovery.toml`.
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
    # Reconstruct the producer's environment on the consumer's machine.  The
    # stage argument describes which decode work the record speaks for; using
    # the reader here compares two different jobs by construction (for example,
    # Attestatores passes pixels through while Perlector decodes them).
    current_environment = _validate_decode_environment(
        _decode_environment(producer), f"{reader} current for {producer}"
    )
    differences = _decode_difference(previous_environment, current_environment)
    if differences:
        # This is intentionally an observation only. Unit 17 decides when a
        # decoder difference becomes fatal; silently omitting it is not allowed.
        print(
            f"decode environment differs by name from {producer}: {differences}",
            file=sys.stderr,
        )


def _decode_difference(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Every field difference the binding consult requires reported by name.

    Both values describe the producer's work, observed at production and again
    by its consumer.  Role fields remain in the comparison: changing either one
    is a real change to the producer environment, not a reason to weaken the
    field-by-field contract. Reporting is not refusal; Unit 17 alone decides
    whether any valid difference becomes fatal.
    """
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
    """Validate a content-addressed reference before sealing it into evidence.

    The traversal check below is a shape check on the reference string, not
    the run tree's own containment guard: nothing here reads a file with this
    path, and every reader that eventually does must still go through
    ``RunTree.resolve()`` (``common/runtree/store.py``), which additionally
    resolves the path and checks it against the tree root — catching, for
    example, a symlink component this string-only check would miss.
    """

    if not isinstance(value, Mapping) or set(value) != {"relative_path", "sha256"}:
        raise SchemaRefusal(f"serving evidence {label} reference has unknown or missing fields")
    relative_path = value["relative_path"]
    digest = value["sha256"]
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path.startswith("/")
        or ".." in relative_path.split("/")
        or not isinstance(digest, str)
        or not is_sha256(digest)
    ):
        raise SchemaRefusal(f"serving evidence {label} reference is malformed")
    return {"relative_path": relative_path, "sha256": digest}


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

    ``--chair`` remains in the common argv vocabulary so orchestration can pass
    one stable shape, but only the Attestatores opts into implementing it. A
    stage that does not opt in must refuse the value before opening or writing a
    run, rather than silently succeeding at its ordinary operation.
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
    # No `choices` here: the fixture declares which scenarios exist, and a
    # hard-coded list in a second place is a drift surface. `scenario_for`
    # refuses an undeclared name after the fixture is loaded.
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--fixture-root", default="proof")
    parser.add_argument(
        "--corpus-register",
        default=None,
        help="append-only corpus register to snapshot at ingress and verify at later stages",
    )
    parser.add_argument("--models-config", default="config/models.toml")
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
        help="Tyrel's reference for the predeclared prior-draft instrument design",
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
            "Tyrel's reference for the predeclared Lectio nuda sampling design; "
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
            "run's argv. GOVERNANCE 6 — 'the record itself protects the past' — is "
            "why the receipt records the caps that actually bound the serving "
            "moment (the launch audit's profile.tier) rather than folding this into "
            "the reproducibility contract config_digest exists to protect."
        ),
    )
    return parser


def load_fixture(fixture_root: str) -> dict[str, Any]:
    """Read the declared fixture as data.

    Refused loudly when absent: harvest invariant #3 in spirit — an empty or
    missing input is a loud failure, never a green run with no output.
    """
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
    """Refuse a bad spec-08 binding before a run tree exists, on every path.

    One function on purpose: the fixture path (`run_config_bindings`) and the
    real-submission path (`door._real_bindings`) must refuse the same things,
    or a defect the fixture path catches at run creation costs a real corpus
    the whole pre-Perlector leg before the Perlector finally refuses it.
    Returns the declaration's sha256, which both paths seal.
    """
    if witness_context not in WITNESS_CONTEXT_REGIMES:
        raise ContractError(
            f"witness_context {witness_context!r} is not one of {WITNESS_CONTEXT_REGIMES}"
        )
    if (
        not isinstance(nuda_per_mille, int)
        or isinstance(nuda_per_mille, bool)
        or not (0 <= nuda_per_mille <= MAX_NUDA_PER_MILLE)
    ):
        raise ContractError(
            f"nuda_per_mille must be an integer in [0, {MAX_NUDA_PER_MILLE}], got {nuda_per_mille!r}"
        )
    if not isinstance(nuda_approval_ref, str):
        raise ContractError("nuda_approval_ref must be a string")
    # Spec 08: Lectio nuda "runs on a predeclared, Tyrel-approved sampling
    # design... fixed before the run". Hard rule 1 is what makes that a refusal
    # rather than a note: the sampling design is his to approve, and a run that
    # draws an unapproved sample has decided something nobody asked it to. The
    # reference is sealed beside the rate, so a run cannot later claim an
    # approval it was not started under.
    if nuda_per_mille and nuda_approval_ref != NUDA_APPROVAL_SUBJECT:
        raise ContractError(
            f"a Lectio nuda rate of {nuda_per_mille}/1000 needs Tyrel's predeclared sampling "
            f"design selector {NUDA_APPROVAL_SUBJECT!r} in --nuda-approval-ref; an arbitrary "
            "string is not an approval record"
        )
    if (
        not isinstance(perlector_instrument_per_mille, int)
        or isinstance(perlector_instrument_per_mille, bool)
        or not (0 <= perlector_instrument_per_mille <= MAX_PERLECTOR_INSTRUMENT_PER_MILLE)
    ):
        raise ContractError(
            "perlector_instrument_per_mille must be an integer in [0, 1000], got "
            f"{perlector_instrument_per_mille!r}"
        )
    if not isinstance(perlector_instrument_approval_ref, str):
        raise ContractError("perlector_instrument_approval_ref must be a string")
    if (
        perlector_instrument_per_mille
        and perlector_instrument_approval_ref != PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT
    ):
        raise ContractError(
            f"a Perlector prior-draft control rate of {perlector_instrument_per_mille}/1000 "
            "needs Tyrel's predeclared sampling design selector "
            f"{PERLECTOR_INSTRUMENT_APPROVAL_SUBJECT!r} in "
            "--perlector-instrument-approval-ref; an arbitrary string is not an approval record"
        )
    try:
        witness_context_config_bytes = Path(witness_context_config_path).read_bytes()
    except OSError as error:
        raise ContractError(
            f"the witness-context declaration at {witness_context_config_path} could not be read"
        ) from error
    witness_context_config_digest = digest_bytes(witness_context_config_bytes)
    # Coverage, not just readability, checked here rather than left to the
    # Perlector: this function already holds `models.witness_chairs` and
    # already reads this file's bytes for the digest above, so a chair with no
    # declared entry can refuse before the run tree exists rather than after
    # the Exemplar, Designator and the entire Attestatores leg have already
    # run against every witness model on every act — the expensive part of a
    # live pod run, spent on what is usually a config typo. The Perlector's own
    # `dossier.load_witness_context` still does the full per-entry schema
    # validation when it actually loads this file to build a dossier; this is
    # only the cheap presence check that can run this early.
    try:
        witness_context_table = tomllib.loads(witness_context_config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the witness-context declaration at {witness_context_config_path} could not be "
            f"parsed: {error}"
        ) from error
    missing = [chair for chair in models.witness_chairs if chair not in witness_context_table]
    if missing:
        raise ContractError(
            f"chair {missing[0]!r} has no declared entry in {witness_context_config_path}; "
            "every configured witness must carry a factual dossier context, or none is described"
        )
    for chair, entry in sorted(witness_context_table.items()):
        # Shape, not just presence: `attestator_1 = "typed by mistake"` passed
        # the presence check and then cost the whole pre-Perlector leg before
        # `dossier.load_witness_context` refused it.
        if (
            not isinstance(entry, dict)
            or set(entry) != {"training_domain"}
            or not isinstance(entry.get("training_domain"), str)
            or not entry["training_domain"].strip()
        ):
            raise ContractError(
                f"the witness-context entry for {chair!r} in {witness_context_config_path} "
                "is not a closed table with only a non-blank training_domain"
            )
    unaddressed = [
        chair for chair in witness_context_table if chair not in set(models.witness_chairs)
    ]
    if unaddressed:
        raise ContractError(
            f"{witness_context_config_path} declares {unaddressed[0]!r}, which is not a "
            "configured witness chair; a misspelt chair here would silently lose its witness"
        )
    return witness_context_config_digest


def real_run_policy_digest(
    *,
    witness_context: str,
    witness_context_declaration_sha256: str,
    nuda_per_mille: int,
    nuda_approval_ref: str,
    perlector_instrument_per_mille: int,
    perlector_instrument_approval_ref: str,
    draft_fed: bool,
) -> str:
    """The digest a real run seals its run-level reading knobs under.

    On the fixture path these seven facts live inside `config_digest`, and
    `open_context`'s reuse check refuses a resumed run that supplies a different
    value. The real `config_digest` cannot be recomputed downstream
    (`_open_real_context` says why), so on that path they need a *named* seal of
    their own, or `--witness-context blinded` on a resume would reach the
    Perlector unchecked while every other refusal stayed green. One function,
    called by `door._real_bindings` when the run is created and by
    `real_run_bindings` at every later stage's open, so the two sides cannot
    drift on which fields the name covers. The field set is closed: a knob added
    to one caller and not the other is a run-policy digest that never moves.

    Values are validated by `validate_witness_context_bindings` on both paths
    before this is called; this only closes the set and hashes it.
    """
    if not isinstance(draft_fed, bool):
        raise ContractError(f"draft_fed must be a bool, got {draft_fed!r}")
    return digest_of(
        {
            "witness_context_regime": witness_context,
            "witness_context_declaration_sha256": witness_context_declaration_sha256,
            "nuda_per_mille": nuda_per_mille,
            "nuda_approval_ref": nuda_approval_ref,
            "perlector_instrument_per_mille": perlector_instrument_per_mille,
            "perlector_instrument_approval_ref": perlector_instrument_approval_ref,
            "draft_fed": draft_fed,
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
    serving_recipes_config_path: str | Path = DEFAULT_SERVING_RECIPES_CONFIG_PATH,
    pod_placement_config_path: str | Path = DEFAULT_POD_PLACEMENT_CONFIG_PATH,
    corpus_frame_config_path: str | Path = DEFAULT_CORPUS_FRAME_CONFIG_PATH,
    decoding_config_path: str | Path = DEFAULT_DECODING_CONFIG_PATH,
    triage_modes_config_path: str | Path = DEFAULT_TRIAGE_MODES_CONFIG_PATH,
) -> dict[str, Any]:
    """The three `run.json` bindings, and everything that shapes them.

    Since spec 02 `config/models.toml` owns the roster, the witness floor and
    the adapter recipes, so two of the three come straight off it. The third,
    `config_digest`, is the digest of *everything* that shapes this run's
    behaviour — the model configuration, fixture, scenario, PDF-render settings,
    Designator padding, geometry and grouping policy, Armarium projection configuration,
    recovery policy, decoding policy, the run-level hard-failure policy,
    serving-recipe catalogue, and pod-placement catalogue. The synthetic fixture
    declares byte-backed pages only, so
    it does not claim to bind the real Door's PDFium/Pillow/libheif execution
    recipe; ``door._real_bindings`` binds that recipe on actual ingress.

    All three parts are load-bearing, and the scenario is the one easiest to
    drop by accident. Spec 01's third acceptance test reuses one run id under a
    second scenario and requires the refusal *before any write*; the two
    scenarios declare identical source pages, so with the scenario out of this
    digest nothing in `run.json` distinguishes them and the run gets four
    stages in before artifact immutability catches it. A late incidental
    refusal is not the sealed-tree guarantee spec 01 landed.
    """
    # The Door parses `PdfRenderSettings` out of this file and then needed its
    # digest; reading it here a second time is what let a rewrite between the two
    # reads produce a run whose `render_settings` recorded one target DPI while
    # `config_digest` bound the bytes of another (audit S6). The Door now reads
    # once (`render_config.load_pdf_render_binding`) and hands the digest of the
    # exact bytes it parsed down here. A stage that only needs the binding — every
    # `open_context` caller — has nothing parsed to carry and reads the file
    # itself; that read is proven against the run by the `config_digest`
    # comparison in `open_context`.
    if pdf_render_config_sha256 is not None:
        if not is_sha256(pdf_render_config_sha256):
            raise ContractError(
                "the supplied PDF render configuration digest is not a sha256; a binding "
                "may not be sealed under a value nothing could have hashed"
            )
        pdf_render_config_digest = pdf_render_config_sha256
    else:
        try:
            pdf_render_config_digest = digest_bytes(Path(pdf_render_config_path).read_bytes())
        except OSError as error:
            raise ContractError(
                "the PDF render configuration binding at "
                f"{pdf_render_config_path} could not be read"
            ) from error
    try:
        perlector_protocol_config_digest = digest_bytes(
            Path(perlector_protocol_config_path).read_bytes()
        )
    except OSError as error:
        raise ContractError(
            "the Perlector protocol configuration binding at "
            f"{perlector_protocol_config_path} could not be read"
        ) from error
    try:
        perlector_audit_config_digest = digest_bytes(Path(perlector_audit_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the Perlector audit configuration binding at "
            f"{perlector_audit_config_path} could not be read"
        ) from error
    try:
        padding_config_digest = digest_bytes(Path(designator_padding_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the Designator padding configuration binding at "
            f"{designator_padding_config_path} could not be read"
        ) from error
    try:
        geometry_config_digest = digest_bytes(Path(designator_geometry_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the Designator geometry configuration binding at "
            f"{designator_geometry_config_path} could not be read"
        ) from error
    # Hashed here and parsed at the point of use — geometry's shape exactly, and
    # deliberately not `triage_modes`', which is validated here as well.
    #
    # The schema this file has to satisfy lives in
    # `pipeline/2_designator/grouping_config.py::load_grouping_config`, and
    # `common/` may never import a stage module: `common/README.md` says "it never
    # imports back" and `common/chairs/test_chairs_import_boundary.py` reads every
    # file under `common/` through `ast` to enforce it. Reaching that loader from
    # here — or from the Door, which `pipeline/test_stage_import_boundaries.py`
    # holds to the same rule across stage directories — would buy a bind-time
    # refusal by breaking two live guards, so the refusal for a *malformed* file
    # is meant to sit where the loader already is: the Designator's structure
    # pass, loading it in `initial_pass` and proving it against this digest
    # through `require_sealed_config("designator-grouping", ...)`, with
    # `load_grouping_config` refusing loudly there, before the stage marks
    # anything out. That call has not landed yet — see the note beside
    # `sealed_config_digests` below — so today a malformed file is only hashed,
    # never parsed. An *unreadable* file still refuses right here, at run
    # creation, exactly as geometry's does.
    try:
        grouping_config_digest = digest_bytes(Path(designator_grouping_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the Designator grouping configuration binding at "
            f"{designator_grouping_config_path} could not be read"
        ) from error
    _, alignment_config_digest = load_alignment_limits(alignment_config_path)
    corpus_frame_policy, corpus_frame_config_digest = load_corpus_frame_policy(
        corpus_frame_config_path
    )
    _decoding_policy, decoding_config_digest = load_decoding_policy(decoding_config_path)
    # The parameter, not the module default: every other sealed configuration here
    # binds the path its caller named, and `require_triage_modes` already accepts
    # one at the point of use. Sealing the default while the recheck read a caller's
    # file would have reported drift on two files that had each never changed.
    # Validated, not merely hashed. Sealing bytes the point-of-use recheck will
    # refuse produces a run whose `run.json` is well formed and which cannot reach
    # triage; the refusal belongs at run creation, where nothing has been written.
    triage_modes_raw = _read_triage_modes_config(triage_modes_config_path)
    _validate_triage_modes_config(triage_modes_raw, triage_modes_config_path)
    triage_modes_config_digest = digest_bytes(triage_modes_raw)
    armarium_formats_digest, armarium_formats = bind_armarium_formats(armarium_formats_config_path)
    try:
        serving_recipes_config_digest = digest_bytes(Path(serving_recipes_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the serving recipes configuration binding at "
            f"{serving_recipes_config_path} could not be read"
        ) from error
    try:
        pod_placement_config_digest = digest_bytes(Path(pod_placement_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the pod placement configuration binding at "
            f"{pod_placement_config_path} could not be read"
        ) from error
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
                # Spec 08's run-level toggle and its sampling design. Sealed
                # here exactly like `pdf_target_dpi_override` above: a stage
                # never stores its own copy of "what regime did this run use",
                # it re-derives the same config_digest from its own CLI flags
                # and `open_context`'s existing IncompatibleReuse check refuses
                # a resumed run that supplies a different value than the one
                # the tree was sealed under.
                "witness_context_regime": witness_context,
                "witness_context_declaration_sha256": witness_context_config_digest,
                "nuda_per_mille": nuda_per_mille,
                "nuda_approval_ref": nuda_approval_ref,
                "perlector_instrument_per_mille": perlector_instrument_per_mille,
                "perlector_instrument_approval_ref": perlector_instrument_approval_ref,
                "perlector_protocol_config_sha256": perlector_protocol_config_digest,
                "perlector_audit_config_sha256": perlector_audit_config_digest,
                "draft_fed": draft_fed,
                "serving_config_inputs": serving_config_inputs,
            }
        ),
        "adapter_recipes": dict(sorted(models.adapter_recipes.items())),
        "serving_config_inputs": serving_config_inputs,
        # The record of which bytes each digest above was taken over, so a stage
        # that re-reads one of these files for its values can prove it read what
        # was bound (`StageContext.require_sealed_config`). Every caller writing a
        # run records this map in the run authority as well, so a later reader
        # holding only the tree can name the policies that governed it rather than
        # merely re-derive them.
        #
        # Every name here is bound into `config_digest` above, and every name here
        # has a point of use that requires it: padding and geometry at the
        # Designator's crop, alignment at the Attestatores, the shard limit at run
        # creation, the two Perlector policies at the reading, `recovery` at the
        # Recensor, the Designator recovery pass and the orchestrator's dispatch,
        # `pdf-render` at the Door that parsed it, and `hard-failure` at the
        # orchestrator's own checkpoint. A name sealed with no point of use would
        # read as a closed window that nothing actually shuts.
        #
        # `grouping` is the one entry ahead of its point of use today: it is
        # bound and sealed here so the digest exists to prove against, but
        # `pipeline/2_designator/run.py::initial_pass` does not yet call
        # `require_sealed_config("designator-grouping", ...)` — that call, and the
        # malformed-policy refusal it guards, arrive with the Designator's
        # structure pass, not with this binding.
        #
        # `hard-failure` is the family's fourth member and the last to be sealed.
        # It is the one the orchestrator reads BEFORE the run exists — the tally
        # threshold has to be known to decide whether a resumed run may re-enter a
        # stage at all — and then holds for the whole run, so its point of use is
        # the first moment a run authority exists to prove it against, not the
        # read itself.
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
        # Parsed from the bytes `recovery_policy["config_sha256"]` names, and
        # carried into `StageContext` so the Recensor and the Designator recovery
        # pass never open the file a second time (audit S3).
        "recovery_policy": recovery_policy,
    }


def _read_config_digest(path: str | Path, description: str) -> str:
    """Digest one configuration file's bytes, refusing an unreadable one by name.

    The same read-and-name shape `run_config_bindings` spells out inline for
    each file; shared here by the real path so its refusals say the same thing
    about the same file. `run_config_bindings` is deliberately left as written:
    the fixture path's bytes are pinned by the acceptance digests, and a
    behaviour-neutral rewrite of it buys nothing those pins can measure.
    """
    try:
        return digest_bytes(Path(path).read_bytes())
    except OSError as error:
        raise ContractError(f"the {description} binding at {path} could not be read") from error


# The names a real run seals that no stage after the Door can recompute. The
# data-handling policy gated admission and is named by the Door alone
# (`--data-gate-policy` is a Door-only flag the orchestrator forwards to no
# other stage), so a later stage can require the name to be *present* — the
# run was gated — but cannot say which bytes it should carry.
_REAL_DOOR_ONLY_SEALED_NAMES: Final = ("data-handling",)


def real_run_bindings(models: ModelsConfig, args) -> dict[str, Any]:
    """The downstream-relevant subset of a real run's bindings, recomputed at open.

    Reads the same files `run_config_bindings` reads, under the same names, and
    returns what a stage after the Door needs: the sealed digest map to recheck
    `run.json` against and to carry as `StageContext.sealed_config_digests`, the
    parsed Armarium formats and recovery policy, the serving configuration
    inputs, and the roster facts. It computes no `config_digest`: the real one
    binds the submission ledger and the Door machine's decoder versions, which
    this stage does not hold and must not bind to (`_open_real_context`).

    Three names here exist only on the real path — `models`, `armarium-formats`
    and `run-policy` — because on the fixture path the same facts are inside
    `config_digest` and already rechecked whole. Here they are what stands in
    for that check, name by name.
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
    triage_modes_raw = _read_triage_modes_config(DEFAULT_TRIAGE_MODES_CONFIG_PATH)
    _validate_triage_modes_config(triage_modes_raw, DEFAULT_TRIAGE_MODES_CONFIG_PATH)
    armarium_formats_digest, armarium_formats = bind_armarium_formats(args.formats_config)
    serving_recipes_config_digest = _read_config_digest(
        args.serving_recipes_config, "serving recipes configuration"
    )
    pod_placement_config_digest = _read_config_digest(
        DEFAULT_POD_PLACEMENT_CONFIG_PATH, "pod placement configuration"
    )
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
            "designator-padding": _read_config_digest(
                args.designator_padding_config, "Designator padding configuration"
            ),
            "designator-geometry": _read_config_digest(
                args.designator_geometry_config, "Designator geometry configuration"
            ),
            "designator-grouping": _read_config_digest(
                args.designator_grouping_config, "Designator grouping configuration"
            ),
            "alignment": alignment_config_digest,
            "corpus-frame-shard": corpus_frame_config_digest,
            "decoding": decoding_config_digest,
            "perlector-protocol": _read_config_digest(
                args.perlector_protocol_config, "Perlector protocol configuration"
            ),
            "perlector-audit": _read_config_digest(
                args.perlector_audit_config, "Perlector audit configuration"
            ),
            "pdf-render": _read_config_digest(args.pdf_render_config, "PDF render configuration"),
            "recovery": recovery_policy["config_sha256"],
            "hard-failure": hard_failure_policy["config_sha256"],
            "triage-modes": digest_bytes(triage_modes_raw),
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
            ),
        },
        "armarium_formats": armarium_formats,
        "recovery_policy": recovery_policy,
    }


def load_corpus_frame_policy(path: str | Path) -> tuple[dict[str, int], str]:
    """Read R0's bounded corpus-frame policy from the bytes a run seals."""
    try:
        raw = Path(path).read_bytes()
        record = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"corpus-frame shard configuration at {path} could not be read"
        ) from error
    if set(record) != {"max_pages_per_shard"}:
        raise ContractError("corpus-frame shard configuration has the wrong closed schema")
    limit = record["max_pages_per_shard"]
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ContractError("corpus-frame max_pages_per_shard must be an integer in [1, 1000]")
    return {"max_pages_per_shard": limit}, digest_bytes(raw)


def require_corpus_frame_shard(
    page_count: int,
    sealed_config_digests: Mapping[str, str],
    path: str | Path = DEFAULT_CORPUS_FRAME_CONFIG_PATH,
) -> None:
    """Point-of-use recheck for the sealed ≤1,000-page shard boundary."""
    policy, observed = load_corpus_frame_policy(path)
    bound = sealed_config_digests.get("corpus-frame-shard")
    if bound is None:
        # A run that sealed no shard digest at all is a different fault from one
        # whose config changed after binding; naming them apart tells an operator
        # whether to look at the binding step or at the file (CodeRabbit
        # chain-end review; host disposition: fixed).
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
    """Refuse mode schema or bytes that differ from the run's sealed vocabulary."""
    if path is None:
        path = DEFAULT_TRIAGE_MODES_CONFIG_PATH
    raw = _read_triage_modes_config(path)
    bound = sealed_config_digests.get("triage-modes")
    if bound is None:
        raise ContractError("this run sealed no digest for the triage modes configuration")
    observed = digest_bytes(raw)
    if bound != observed:
        # Check the binding before parsing. A malformed replacement is still
        # first and foremost bytes this run never sealed, and must not mask that
        # security refusal behind a TOML diagnostic.
        raise ContractError(
            "the triage modes configuration changed between run binding and its "
            f"point-of-use check: this run sealed {bound}, and {path} now hashes to {observed}"
        )
    _validate_triage_modes_config(raw, path)


# The roles the pipeline addresses by name, beside the Attestator witnesses.
# Here rather than in the stage modules because `unaddressed_chairs` below has to
# know the whole set of roles the pipeline ever asks for, and a second spelling in
# `2_designator/run.py` or `4_perlector/run.py` would be a set that could drift
# from the check that depends on it.
#
# `PERLECTOR_CHAIR` exists even though its value equals the stage name, because a
# stage and a chair are two vocabularies: `config/models.toml` says roles "are
# configuration keys, not concepts". The Perlector reading through the stage
# constant worked only because the two words happen to coincide, and repinning the
# chair to a differently named role would have quietly broken it.
DESIGNATOR_CHAIR = "designator_structure"
PERLECTOR_CHAIR = PERLECTOR

# `config/models.toml` already carries `secondary_proposer`, absent by default
# ("no secondary proposer is configured for the offline walking skeleton") —
# but an absence is only a recorded decision if something actually resolves the
# role and writes that decision down. Naming it here, in the one set
# `unaddressed_chairs` checks against, is what stops the day someone flips the
# roster to a real detector from silently turning every run `partial`: the
# resolution path has to exist *before* that flip, not be discovered by it.
SECONDARY_PROPOSER_CHAIR = "secondary_proposer"


def unaddressed_chairs(models: ModelsConfig) -> tuple[str, ...]:
    """Configured roles no stage in this pipeline will ever ask for.

    `models.toml` accepts a new role without a code change, which is the point —
    and the cost is that a misspelt one is still a perfectly valid configured
    chair. `attestor_4` for `attestator_4` fails the `attestator_` prefix, so it
    never enters `witness_chairs`, no stage resolves it, and no artifact anywhere
    names it: a configured model was silently never asked for anything and the run
    still reported `complete`. GOVERNANCE 2 refuses complete unless everything
    reconciles, and a chair in the roster is something to reconcile.

    Absences count as addressed: an absent chair is a decision already recorded.

    So does the configured base of an addressed adapter. No stage resolves a base by
    name — the adapter is what a stage asks for — but the base artifact genuinely
    participates in the reading and its identity travels in the serving receipt as
    `adapter_identity`. Reporting it unaddressed forced a perfectly valid adapter
    roster to `partial` for a chair that *is* accounted for, one indirection away.
    Found by CodeRabbit on pull request 16.
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

    Declared, not observed: nothing here served anything, so these are fixture
    values in the same sense as the synthetic pages, and they say so —
    `fixture://` for an endpoint, `fixture` for a dtype. Reading them as a
    measurement of a real serving moment would be exactly the confusion
    GOVERNANCE 10 forbids.

    Two consequences worth knowing until the pipeline adopts spec 04's real
    serving-manager callback. Endpoint and start time are confined to the run
    receipt, so a stage payload carries only the content-addressed reference to one. And
    `started_at` is a constant *because* the skeleton's receipts sit inside the
    tree the determinism tests hash; a real receipt is honestly
    non-deterministic, and the run at which that becomes true is the run at which
    `receipts/` has to leave those snapshots. Neither test is loosened for it
    now, while every receipt in the tree is still a declared fixture value.
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

    The receipt holds serving-time facts, so endpoint and timestamp must never be
    copied into a stage artifact. A configured identity must still agree exactly
    with the named role in the sealed models config, its explicit revision, and
    the digest-checked receipt reference. This validates evidence; it never asks
    the registry for a neighbouring role, revision, recipe, or cache.
    """
    if not isinstance(provenance, dict):
        raise SchemaRefusal("model provenance is not an object")
    leaked = sorted({"endpoint", "started_at"} & set(provenance))
    if leaked:
        raise SchemaRefusal(
            f"model provenance leaks serving-only field(s) {leaked}; use the run receipt reference"
        )
    # **An allowlist, because #42 refuses wrong-schema provenance rather than
    # known-bad provenance.** Naming `endpoint` and `started_at` above catches the
    # two leaks we have already made and nothing else: any field a later stage
    # invents travels into a sealed reading unexamined, which is precisely the
    # tampering the invariant is about. The check above stays because it names the
    # two by name and says why; this one closes the rest.
    unexpected = sorted(set(provenance) - _PROVENANCE_FIELDS)
    if unexpected:
        raise SchemaRefusal(
            f"model provenance carries unknown field(s) {unexpected}; a reading's provenance "
            "is a closed schema, and a field nothing validates is a field nothing can trust"
        )
    # **Required of the Perlector, forbidden of everyone else.** Checking the value
    # only when it happened to be present left the clause it cites unenforced from
    # both directions: a Perlectio that stopped recording its regime validated all
    # the way into the export, and a stage that does not own the field could attach
    # one and nothing objected. Which producer owns a field is part of the closed
    # schema, not a separate question.
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
    # **Owned by one producer and one chair, checked from both directions.** The
    # same reasoning `witness_regime` above is held to: a field validated only
    # when it happens to be present is a field any stage may attach, and an
    # unvalidated field inside a sealed reading is exactly what invariant #42
    # exists to stop. Only the Designator's structure pass calls a chair to mark
    # out structure (ARCHITECTURE: the Designator marks out; the Attestatores
    # bear witness), and only `designator_structure` sits in that chair
    # (`unaddressed_chairs` names the whole role set). A witness Testimonium
    # carrying a structure-chair call, or a structure call attributed to a
    # witness role, would be a reading claiming a serving moment that was not
    # its own — GOVERNANCE 6, and the same fabricated-moment refusal spec D §7
    # names.
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
    # **The allowlist above says which fields may exist; only here is it known which
    # may exist *together*.** `absence` is legal provenance — on an absent chair — so
    # the closed schema admits it, and the absent branch returned before this line.
    # Without this check a configured chair carried an unread `absence` record beside
    # a full identity: two contradictory claims about the same chair, sealed into a
    # reading, and the reading still verified. Found by the Terra review seat, which
    # reproduced it with a fabricated absence for a chair that never existed.
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

    Two facts, and only the two a consumer can check without opening a per-page
    call record: the wire kind the chair was asked through, and the decoding
    policy this run sealed for the structural pass.

    `decoding_policy` is a *name*, not a value. The Designator's structure pass
    runs under `config/decoding.toml`'s `[structure]` section while every
    Attestator reads at `reading_of_record` (Tyrel, 2026-09-02), so provenance
    naming the wrong section would report a posture the pass did not run under —
    GOVERNANCE 10's confusion of a claim with a measurement, and the reason the
    temperature itself is deliberately *not* copied here: a number beside the
    name could disagree with the sealed bytes, and then two artifacts in one run
    would say different things about one run's decoding.

    `decoding_config_sha256` binds that name to the exact bytes. It is checked
    through `require_sealed_config` against the digest this run bound at
    `open_context`, so a `config/decoding.toml` edited between the binding read
    and the structure call refuses here rather than travelling into a sealed
    reading unnoticed — the point-of-use recheck the whole sealing family
    exists for, applied to the one policy the structural pass is free to vary.
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
    """The declared scenario, refused loudly when the fixture does not name it.

    The fixture is the authority on which scenarios exist; a misspelt scenario
    that fell through to `happy` behaviour would be a run wearing the wrong
    configuration with a green exit code.
    """
    for scenario in fixture.get("scenario", []):
        if scenario["name"] == name:
            return scenario
    declared = [scenario["name"] for scenario in fixture.get("scenario", [])]
    raise ContractError(f"the fixture declares no scenario {name!r}; declared: {declared}")


def expected_acts(context) -> list[dict[str, Any]]:
    """Every act the proposal seal expects, each with a validated Designator outcome.

    One reader for all five consumers, because the seal is the downstream
    expected-act authority and this is the handoff contract: every entry carries
    the Designator's outcome for that act, and an entry whose outcome is missing
    or outside the closed vocabulary is invariant #10's imbalance — fatal at the
    first consumer, never a `.get` that quietly reads as marked-out.
    """
    seal = context.tree.read_artifact(
        DESIGNATOR,
        "proposal-seal",
        artifact_id(DESIGNATOR, "proposal-seal", "proposal-seal", None),
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
    if not isinstance(count, int) or isinstance(count, bool) or count != len(acts):
        raise FatalAccounting(
            "the Designator proposal seal count does not reconcile with its expected-act rows"
        )
    act_ids: set[str] = set()
    act_keys: set[str] = set()
    for act in acts:
        if not isinstance(act, dict):
            raise FatalAccounting("the Designator proposal seal has a non-object expected-act row")
        required = {
            "act_id",
            "act_key",
            "page_id",
            "page_ordinal",
            "has_continuation",
            "outcome",
            "evidence",
        }
        if set(act) != required:
            raise FatalAccounting(
                "the Designator proposal seal expected-act row has fields other than its "
                "closed denominator contract"
            )
        if (
            not isinstance(act["act_id"], str)
            or not act["act_id"]
            or not isinstance(act["act_key"], str)
            or not act["act_key"]
            or not isinstance(act["page_id"], str)
            or not act["page_id"]
            or not isinstance(act["page_ordinal"], int)
            or isinstance(act["page_ordinal"], bool)
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
    # Gated on the run authority's ingress record by name, never on the shape of
    # the fixture. With `fixture={}` on a real run the synthetic floor did not
    # skip: it read `[]`, found nothing missing, and sent every row to the
    # minted-row check, which admits only a residual hold and a page-fallback --
    # so a real Designator's ordinary structural act was refused with a sentence
    # about a fixture the run never had. Real mode classifies each row from its
    # own Designator evidence instead (`_verify_real_act_denominator`).
    #
    # Ingress is no longer the only thing that opens that route. Which pass
    # produced this seal is a fact about the sealed serving catalogue, not about
    # where the pages came from: the offline end-to-end run drives a *served*
    # structure chair over fixture pages, and a real submission may be marked out
    # by a fixture chair over no chair at all. The two questions are independent,
    # so the seal's own provenance is asked as well as the run's ingress, and a
    # seal produced by a served structure chair takes the recomputing route
    # whichever ingress it ran under.
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

    **Why the artifact and not the catalogue.** `common/` may never import
    `operations/` (`common/contracts/serving.py`'s own docstring says why: both
    sides import the shared shapes, and neither may depend on the other), so
    this module cannot call `serving_mode_for` over the bound recipes the way
    the Designator's own `main` does when it chooses which pass to run. What it
    reads instead is the decision that choice already wrote down: on the live
    path the structure chair's provenance carries an `engine_call`, and
    `validate_serving_provenance` binds that record to the run's registry, its
    sealed adapter recipe, its sealed decoding digest and a digest-checked
    serving receipt before a single row is admitted on the strength of it. The
    catalogue decides; this reads what the catalogue decided, and refuses a
    claim the run cannot support.

    **What a seal cannot escape by staying silent.** Omitting `engine_call` does
    not buy a forged seal an easier check. On real ingress the route is taken
    anyway. On fixture ingress the fixture floor is the *stronger* check —
    every act the sealed fixture declares must appear, with the identity the
    fixture derives — so a seal that drops the field is measured against a
    declaration instead of against its own evidence. The one seal both routes
    would admit is one whose rectangles are exactly the fixture's own, which is
    the same act at the same identity by either name.

    The refusal is `validate_serving_provenance`'s own, deliberately not
    rewrapped: it names which field of the provenance is wrong, and a
    `FatalAccounting` about the denominator would hide that behind a sentence
    about act counts.
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

    An absent ingress record reads as the synthetic walking skeleton, exactly as
    `refuse_halted_run` reads it: the hand-built trees in this module's own unit
    tests predate the record, and a present one must still parse or it refuses.

    The single reader behind this check: each stage's own `real_ingress(context)`
    calls straight through to this function rather than restating the read, so a
    change to how the ingress record is parsed lands once.
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

    There is no declaration to check a real seal against, so the fixture floor
    does not run. What replaces it is not trust: each row is classified by which
    Designator record exists for it -- a `hold` naming `residual_bounds`, a
    `hold` naming `page_bounds`, a `page-fallback` record, or a proposal-origin
    `region` on the row's own page -- and then recomputed against that record
    exactly as the fixture path recomputes its minted rows. The three minted
    classes go through `_verify_minted_act_rows` unchanged. The fourth, the
    structural proposal, is the one class the fixture path never had to
    recompute (its declaration was the stronger check) and here is checked
    against the producer's own crop rectangle (`_verify_proposal_act_row`,
    which adds the hop to the retained structure answer when a chair was
    actually served).

    A row matching more than one minted class is refused as ambiguous, and a
    row matching none and carrying no region is refused as unevidenced. Nothing
    tries the classes in turn until one verifies: the evidence decides the
    class, or nothing does (GOVERNANCE 3, hard rule 8).
    """
    fallbacks_by_subject = _designator_records_by_subject(context, "page-fallback")
    holds_by_subject: dict[str, dict[str, Any]] = {}
    minted_rows: dict[str, dict[str, Any]] = {}
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
        hold_payload = (
            hold.get("payload")
            if hold is not None and isinstance(hold.get("payload"), dict)
            else {}
        )
        proposal_regions = [
            record
            for record in records
            if record["kind"] == "region"
            and isinstance(record["payload"].get("transform"), Mapping)
        ]
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
        # A structural proposal is the row with regions and no minted-class
        # record at all. Regions alone do not name a class: a page-fallback act's
        # predetermined crops are proposal regions too (`_publish_page_fallback`
        # cuts them through `cut_minted_region`), and what says which unit they
        # belong to is the `page-fallback` record beside them, whose own
        # rectangle and premise `_verify_page_fallback_act_row` then recomputes.
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
                context, act_id, row, regions, far_regions, structure_call=structure_call
            )
            continue
        minted_rows[act_id] = row
        if hold is not None:
            holds_by_subject[act_id] = hold
    _verify_minted_act_rows(
        context, minted_rows, holds_by_subject, fallbacks_by_subject, beyond="the structural pass"
    )
    _verify_every_conservation_residual_is_accounted(context, observed, holds_by_subject)


def _verify_proposal_act_row(
    context,
    act_id: str,
    row: dict[str, Any],
    regions: list[dict[str, Any]],
    far_regions: list[dict[str, Any]],
    *,
    structure_call: Mapping[str, Any] | None,
) -> None:
    """A structural act, recomputed and then held to the answer it came from.

    `_verify_structural_act_row` proves the row against the rectangle its own
    region record was cut over: identity, act key, page ordinal, continuation.
    That is everything a consumer can check when the rectangle has no earlier
    author — the walking skeleton's crops come from the sealed fixture, and the
    region record is the first place they exist as evidence.

    When a chair was actually served, the rectangle *does* have an earlier
    author, and one more thing becomes checkable: the answer that chair returned
    was retained, parsed and published per page (`structure-answer`, SPEC_D
    §1.3), so a rectangle no answer names is a crop this run cannot attribute to
    the model it says marked it out. Without this hop a Designator — or a tree
    edited after the fact — could mint a perfectly self-consistent act over any
    rectangle at all: identity recomputes, the act key agrees with the region
    that carries it, and every downstream stage reads, witnesses and establishes
    text over ink no model ever proposed. GOVERNANCE 10 is the rule that
    forbids it (the claim is "the structure chair marked this out"), and
    GOVERNANCE 6 is the rule it would break next, since the reading would carry
    the chair's provenance for a rectangle the chair never returned.

    Three claims, and each is refused separately so the refusal says which one
    failed. The **page** must have been scanned: the page's own
    `structure-status` is read at its identity-derived address — a page id is
    not a producer's choice of string but this act's own binding, already
    recomputed above — and it must record `state="scanned"` for this row's page
    and page **ordinal**. The **answer** is then reached through that record's
    `structure_answer_ref` on the digest-checked hop rather than by address, the
    same "prove its premise" step `_verify_page_fallback_act_row` makes, so a
    status pointing at bytes that have since changed refuses instead of
    resolving. And the **rectangle** must appear in that answer's own act list,
    exactly — no nearest match, no containment, no tolerance (hard rule 8: a
    "nearest" rectangle is a selection among candidates dressed as arithmetic).

    Two smaller bindings sit alongside. The answer's own `engine_call` must be
    the seal's, so a page answered under one sealed decoding posture cannot
    supply rectangles for a seal that claims another. And the answer must name
    the per-page `call_record_ref` it was derived from: a `parsed` answer citing
    no call is a record of a reading with no reading behind it.

    A rectangle may legitimately appear more than once. Two identical rectangles
    on one page are one crop and mint one act (SPEC_D §2.2 — the class-and-bounds
    identity has no ordinal namespace), with the second recorded as a
    `duplicate-rectangle` finding rather than refused. So this asks that the
    rectangle be *present*, never that it be unique; requiring uniqueness would
    turn a recorded, deliberate merge into a fatal accounting error.
    """
    _verify_structural_act_row(act_id, row, regions, far_regions)
    if structure_call is None:
        # A structural proposal with no served chair is only reachable here on
        # real ingress (`expected_acts` takes this route on a fixture run only
        # when the seal itself names a call; without one, a fixture run falls
        # to the stronger fixture floor instead). Omitting `engine_call` must
        # not buy a real proposal an easier check than the one its own
        # docstring above promises: staying silent skips not just the answer
        # hop but the geometry recompute's only counterparty, so a forger's
        # self-consistent rectangle would be admitted on its own say-so.
        if _is_real_ingress(context.run):
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
    status_payload = status.get("payload") if isinstance(status.get("payload"), Mapping) else {}
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
    payload = answer.get("payload") if isinstance(answer.get("payload"), Mapping) else {}
    if payload.get("schema") != STRUCTURE_ANSWER_RECORD_SCHEMA:
        raise FatalAccounting(
            f"act {act_id}'s page names a structure answer whose schema is "
            f"{payload.get('schema')!r}, not {STRUCTURE_ANSWER_RECORD_SCHEMA!r}"
        )
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
    # The answer's own provenance is the premise the minted act rests on, and it
    # is held to the same closed schema, registry, sealed recipe and
    # digest-checked receipt the seal's provenance is -- not compared only on
    # the one field (`engine_call`) this hop happens to need next.
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
        or call_record.get("schema") != CHAIR_CALL_RECORD_SCHEMA
        or call_record.get("chair") != DESIGNATOR_CHAIR
        or call_record.get("decoding_config_sha256") != structure_call["decoding_config_sha256"]
    ):
        raise FatalAccounting(
            f"act {act_id}'s structure answer names a call record that is not a "
            f"{CHAIR_CALL_RECORD_SCHEMA!r} record for chair {DESIGNATOR_CHAIR!r} under the "
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
) -> None:
    """A real structural act, recomputed from the rectangle it was minted over.

    `cut_minted_region` publishes `raw_bounds` beside every proposal region: the
    structural rectangle the act identity was bound to, before capture padding.
    That is the one producer-independent fact a consumer can recompute the
    identity against, so a real run's denominator is *verified* rather than
    trusted. Exactly one proposal-origin region may sit on the row's own page; a
    continuation is a second region on a far page and is not counted here, but
    it is not ignored either.

    Act identity binds only page, class and bounds (`act_bindings`), so the
    identity check above proves none of `act_key`, `page_ordinal` or
    `has_continuation` -- a seal row is free to *name* a different page ordinal
    or a foreign act key for the very region whose bounds it verified against,
    and stages 3-7 index and join by those named fields, not by identity.
    `cut_minted_region` publishes `act_key` and `transform.source_page_ordinal`
    beside every region, so both are recomputable and are recomputed here.

    `has_continuation` is the same kind of belief otherwise: nothing before this
    reconciled it against the regions the Designator actually cut. A row
    claiming a continuation with no far-page region names one that was never
    cut; a row denying one while a far-page region exists would drop a
    published continuation crop silently downstream (the Attestatores append
    the far page only when the flag is set) -- exactly the loss GOALS 1 calls
    worse than a poorly read act. So the far-page count is checked against the
    flag in both directions.
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


def _verify_synthetic_act_denominator(context, acts: list[dict[str, Any]]) -> None:
    """Bind the skeleton's discovered-act denominator to its sealed fixture input.

    This check belongs only to the declared synthetic walking skeleton: its fake
    Designator derives every act from fixture data, and the run configuration
    seals those fixture bytes.  Two other routes exist now and neither reaches
    here — a real submission, whose rows are classified from their own Designator
    evidence (`_verify_real_act_denominator`), and a seal produced by a served
    structure chair, whose rows are additionally held to the answers that chair
    returned (`_verify_proposal_act_row`). What is prescribed below is therefore
    the fixture path alone, and a run arriving here has a fixture to be measured
    against.

    The fixture's own acts are a *floor*, never a ceiling: every one must
    appear, and the seal may also carry acts the fixture never declared. Three
    kinds exist, and none is fixture data, so none can be checked against
    it — `_verify_minted_act_rows` checks each against the one thing it *can*
    be checked against: its own Designator evidence record, recomputed rather
    than trusted. A **residual** act (`_publish_residual_holds`) is ink
    conservation found that structural grouping never claimed, and is `held`. A
    **page-fallback** act (`_publish_page_fallback`) is the predetermined crop
    grid cut over a page the structure pass found nothing on, and is `proposed`,
    because the whole point of cutting it is that it goes downstream to be read.
    A **page-residual** act is the whole page held in place of more residuals
    than the sealed bound allows to be minted separately, and is `held`.
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
    # Sorted, because `_verify_residual_act_rows` raises on the *first* row that
    # fails and a set of strings has no stable order: CPython randomises string
    # hashing per process, so a seal carrying more than one bad extra row named a
    # different act in the refusal on every run. The refusal was always correct
    # and always fired; which act it accused was a coin flip, which is the kind of
    # evidence nobody can act on twice. Found by the Opus read of this branch,
    # which demonstrated five different orders over six keys in five runs.
    # One read of the hold artifacts for both directions. Each
    # `_designator_records_by_subject` call walks the stage's whole manifest and
    # opens every record of its kind, and a page held for over-bound residuals is
    # by construction a run with many holds — the case where reading them twice
    # costs most is the one the second direction was added for.
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

    `_verify_minted_act_rows` reads seal row -> conservation record: an extra row
    must prove itself against the reconciliation that found it. That direction
    alone still trusts the producer completely for the rows it did *not* write.
    A `conservation` record may declare unclaimed ink on a page while the seal
    names no act for it, and nothing then disagrees: `_verify_proposal_seal_evidence`
    only reconciles `region` and `hold` artifacts, so a residual that never became
    a hold leaves no artifact to be unaccounted for, every consumer reconciles
    perfectly, and the Designator's own `EXIT_COMPLETE` — which reads the seal's
    rows, not the reconciliation — reports 0 over ink the stage itself measured
    and no crop claimed.

    GOVERNANCE 2 is a rule about the missing row as much as the forged one, and
    the Designator's own docstring already promises the stronger reading: a run
    that "found ink no crop claimed has not completed". This is that promise
    checked at the first consumer rather than asserted by the producer.

    Position within `residual_components` orders evidence only; identity binds
    the residual class and its bounds, so a new component cannot rename one.

    A record may also decline to enumerate, and that is the one case where the
    list's absence is not a gap: a page whose reconciliation exceeded the sealed
    residual bound is held as a single `page-residual` review item instead of as
    that many held acts, and the components are then recomputable from the sealed
    page bytes rather than carried. The tolerance is exactly as wide as the thing
    that earns it — one page-residual hold, naming this page, judged against the
    same bound this record recorded. A withheld record with no such row is the
    silent loss this check exists to prevent, wearing a policy's name.
    """
    if holds_by_subject is None:
        holds_by_subject = _designator_records_by_subject(context, "hold")
    accounted_pages = _page_residual_holds_by_page(holds_by_subject, observed)
    for page_id, record in _designator_records_by_subject(context, "conservation").items():
        payload = record.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        enumeration = payload.get("residual_enumeration")
        if enumeration == RESIDUAL_ENUMERATION_WITHHELD:
            _verify_withheld_page_is_held_as_one_item(
                page_id, payload, accounted_pages.get(page_id, [])
            )
            continue
        if enumeration != RESIDUAL_ENUMERATION_COMPLETE:
            raise FatalAccounting(
                f"the conservation record for page {page_id} records its residual enumeration as "
                f"{enumeration!r}, which is outside the closed set {RESIDUAL_ENUMERATIONS}; a "
                "consumer cannot tell a page with no unclaimed ink from one whose unclaimed ink "
                "was counted and not listed without being told which it is"
            )
        components = payload.get("residual_components")
        if not isinstance(components, list):
            raise FatalAccounting(
                f"the conservation record for page {page_id} carries no residual-component list "
                "to reconcile the denominator against"
            )
        declared_count = payload.get("residual_component_count")
        if not _is_count(declared_count) or declared_count != len(components):
            raise FatalAccounting(
                f"the conservation record for page {page_id} names residual_component_count "
                f"{declared_count!r} but its residual_components list carries {len(components)} "
                "entries; on an enumerated page the count a reviewer is told is the count the "
                "list itself supports, recomputed rather than believed"
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


def _page_residual_holds_by_page(
    holds_by_subject: dict[str, dict[str, Any]], observed: dict[str, dict[str, Any]]
) -> dict[str, list[Mapping[str, Any]]]:
    """Every page-residual hold in the run, indexed by the page it holds.

    Read from the Designator's own artifacts rather than from the seal rows, so
    that a hold the denominator never accounted for is a refusal here instead of
    an absence nothing notices. `_verify_minted_act_rows` has already proven each
    of these against the sealed page and its conservation record; what this index
    is for is the other direction — finding the page that has no hold at all.
    """
    by_page: dict[str, list[Mapping[str, Any]]] = {}
    for act_id, hold in holds_by_subject.items():
        payload = hold.get("payload") if isinstance(hold.get("payload"), Mapping) else {}
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

    And that row must have been judged against the bound this record itself
    names. A hold carrying a laxer bound than the reconciliation applied would
    describe a decision the run never took, which is the same class of untruth
    as the missing row: both leave a reviewer reading a policy that was not the
    one in force.
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
    if holds[0].get("max_residual_components") != bound:
        raise FatalAccounting(
            f"page {page_id} is held against a bound of {holds[0].get('max_residual_components')!r} "
            f"residual components while its own conservation record applied {bound}; the held "
            "page and the reconciliation that held it must name one policy"
        )


def fallback_page_act_key(page_ordinal: int) -> str:
    """The human-readable label of the one act a page's fallback crops belong to.

    For a reviewer's eye and for `expected_acts`'s duplicate-key refusal; what
    keeps this act's *identity* from colliding with anything is
    the closed ``page-fallback`` act class, not this string. Named here so the
    producer and verifier cannot spell it differently.
    """
    return f"page-fallback:{page_ordinal}"


# How much of a page's residual reconciliation its conservation record actually
# enumerates. A closed pair, named once, because the whole point of the withheld
# value is that a consumer must be able to tell "this page had no residual" from
# "this page's residuals were counted and not listed" — and a third spelling
# appearing on one side of that distinction is how the two become one again.
RESIDUAL_ENUMERATION_COMPLETE: Final = "complete"
RESIDUAL_ENUMERATION_WITHHELD: Final = "withheld-page-held"
RESIDUAL_ENUMERATIONS: Final = (RESIDUAL_ENUMERATION_COMPLETE, RESIDUAL_ENUMERATION_WITHHELD)


def page_residual_act_key(page_ordinal: int) -> str:
    """The label of the one act a page held for over-bound residual scatter becomes.

    The same presentation-only role `fallback_page_act_key` has, and named here
    for the same reason: the Designator writes it and this module recomputes it,
    so the two may not spell it differently. Identity is carried by the closed
    ``page-residual`` act class over the page rectangle, never by this string.
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

    `beyond` names what a row is being checked against in refusal sentences.
    The synthetic caller has a fixture denominator to name; the real caller has
    none, so it passes "the structural pass" instead -- a malformed real-mode
    row must never be told it "extends the denominator beyond the fixture",
    the exact sentence a real run never had a fixture to be measured against.

    Two units the Designator may add beyond what the fixture declares, and no
    others. Both exist for GOALS 1's "a missed act is worse than a poorly read
    act", and neither may be trusted merely because the seal's own producer
    wrote it down — that is the same reasoning `expected_acts` already applies
    to every fixture-derived row above.

    A **conservation residual** is ink no structural pass claimed at all. It is
    `held` from the moment it exists, never `proposed`: nothing witnessed it and
    nothing read it, so it may not carry a continuation either, and its identity
    must recompute from facts a reviewer can check against the conservation
    record that found it.

    A **page-fallback** act is the predetermined crop grid cut over a page the
    structure pass found no ink on (Tyrel, 2026-08-11: "If the designator sees
    no text it should default to predetermined crops ... and send the crops down
    stream to be read by everything"). It is `proposed`, because cutting crops
    nothing will read would be the pointless half of that ruling, and its
    identity must recompute against the page's own `structure-status` record,
    which is what independently says the structure pass found nothing there.

    A **page-residual** act is the third, and it is a *held* row like the first:
    a page whose conservation reconciled more unclaimed components than the
    sealed bound allows becomes one review item instead of that many. Both held
    kinds arrive here as a `hold` record, so the routing between them is the
    rectangle the hold names — `residual_bounds` for one component, `page_bounds`
    for the whole page — and a hold naming both is refused by name rather than
    resolved in either direction.

    The evidence index is built once for the whole set rather than per row, and
    the caller may hand in the hold index it already holds. Every residual
    component on a page mints one of these rows, and a speckled or foxed page
    reconciles to tens of thousands of them, so a per-row walk of the stage's
    whole artifact tree makes ordinary input quadratic in itself.
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
                f"act {act_id} is not declared in the sealed fixture and is neither 'held' nor "
                f"'proposed'; the only units that may extend the denominator beyond {beyond} "
                "are a conservation residual, a page-residual hold, and a page-fallback act"
            )
        hold = holds_by_subject.get(act_id)
        if hold is None:
            raise FatalAccounting(
                f"act {act_id} extends the denominator beyond {beyond} but the Designator "
                "published no hold record for it"
            )
        payload = hold.get("payload") if isinstance(hold.get("payload"), dict) else {}
        # Presence, not shape: a hold whose `page_bounds` is malformed is still a
        # page-residual hold making a malformed claim, and must be refused as one
        # rather than fall through to the component path and be accused of
        # carrying no residual bounds.
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
        _verify_residual_traces_to_conservation(context, act_id, row["page_id"], hold, bounds)


def _verify_page_fallback_act_row(
    context,
    act_id: str,
    row: dict[str, Any],
    fallbacks_by_subject: dict[str, dict[str, Any]],
    *,
    beyond: str = "the fixture",
) -> None:
    """The one extra row that may be `proposed`, checked against its own evidence.

    A page-fallback act is the only unit outside the fixture that reaches the
    witnesses and the Perlector, so it is the one whose provenance most needs to
    be recomputed rather than believed. Two independent things are checked, and
    the second is what stops a fabricated one: the identity must derive from
    this page and the one reserved `page-fallback` act class over the page
    rectangle its own record declares, and that record's single input must be the page's
    `structure-status` — read through the digest-checked hop, not by address —
    saying the structure pass genuinely fell back to tiles on that page. A
    fallback act minted over a page whose structure pass *did* detect something
    therefore refuses here, which is exactly the claim-about-what-was-measured
    GOVERNANCE 10 forbids.
    """
    record = fallbacks_by_subject.get(act_id)
    if record is None:
        raise FatalAccounting(
            f"act {act_id} extends the denominator beyond {beyond} as a proposed act but the "
            "Designator published no page-fallback record for it; it is not 'held' either, so it "
            "is not a conservation residual"
        )
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    bounds = payload.get("page_bounds")
    ordinal = row["page_ordinal"]
    expected_key = fallback_page_act_key(ordinal)
    if (
        not isinstance(bounds, dict)
        or payload.get("act_key") != row["act_key"]
        or payload.get("act_key") != expected_key
        or payload.get("page_id") != row["page_id"]
        or payload.get("page_ordinal") != ordinal
    ):
        raise FatalAccounting(
            f"act {act_id}'s page-fallback record does not carry the page id, page ordinal, "
            "derived fallback key, and page rectangle it must bind"
        )
    sources = [
        source
        for source in context.run.get("source_manifest", [])
        if source.get("ordinal") == ordinal
    ]
    if len(sources) != 1:
        raise FatalAccounting(
            f"act {act_id}'s page ordinal {ordinal} does not name exactly one sealed source"
        )
    page = context.tree.read_artifact(
        EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", row["page_id"])
    )
    verify_sealed_page_pixels(context.tree, context.run, sources[0], page)
    page_bytes = context.tree.read_bytes(page["payload"]["image_path"])
    width, height = dimensions(page_bytes)
    full_page_bounds = {"x": 0, "y": 0, "w": width, "h": height}
    if bounds != full_page_bounds:
        raise FatalAccounting(
            f"act {act_id}'s page-fallback rectangle {bounds} is not the complete sealed page "
            f"rectangle {full_page_bounds}"
        )
    try:
        verify_identity(act_id, "act", act_bindings(row["page_id"], "page-fallback", bounds))
    except IdentityRefusal as error:
        raise FatalAccounting(
            f"act {act_id} does not verify against the reserved page-fallback class and the "
            f"page rectangle its own record names: {error}"
        ) from error
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

    A page-residual act says something no other minted row says: *this page's
    reconciliation found more unclaimed components than the sealed bound allows,
    so the page is one review item and the components are not listed*. That makes
    it the one row whose evidence a reader cannot open and count for themselves,
    which is exactly why none of it may be believed here.

    Five things are recomputed rather than read. The rectangle comes from the
    sealed page bytes, so a hold naming a rectangle that is not the whole page —
    or naming a page it was not minted over — refuses however self-consistent its
    own identity is. The identity is re-derived against the reserved
    ``page-residual`` class and that rectangle. The premise is followed to the
    page's own `conservation` record through the digest-checked hop, never by
    address, and that record has to *itself* say the count exceeded the bound the
    hold names and that its enumeration was withheld. And the record must carry
    no `residual_components` key at all: an empty list would mean a page with no
    unclaimed ink, which is the opposite claim, and the key's absence is what
    makes every existing consumer fail loudly instead of reading absence as none.

    The bound itself is not merely internally consistent, it is bound to the run.
    `max_residual_components` is a Designator grouping-policy parameter (SPEC_C
    1), and this run sealed a `designator-grouping` digest for exactly that
    policy at `open_context`. A hold naming its own bound and never naming which
    grouping configuration it was judged against would let a Designator invent
    any bound it liked; the hold's `grouping_config_sha256` is checked against
    `run_sealed_config_digests(context.run)["designator-grouping"]` so the bound
    is bound to the policy this run actually sealed, not merely to itself.

    The hold's own `residual_component_count` is held to the record's. It is the
    number a reviewer reads off the review item, and a hold free to name a
    different one would put a figure in front of a person that no artifact in the
    run supports. GOVERNANCE 10: the count is a measurement, so it is checked
    against the thing that measured it.
    """
    payload = hold.get("payload") if isinstance(hold.get("payload"), dict) else {}
    if "residual_bounds" in payload:
        raise FatalAccounting(
            f"act {act_id}'s hold names both a residual rectangle and a page rectangle; a hold "
            "accounts for one component of unclaimed ink or for a whole page held in place of "
            "its components, and nothing may decide which of the two it meant"
        )
    bounds = payload.get("page_bounds")
    ordinal = row["page_ordinal"]
    expected_key = page_residual_act_key(ordinal)
    if (
        not isinstance(bounds, dict)
        or payload.get("act_key") != row["act_key"]
        or payload.get("act_key") != expected_key
        or payload.get("page_id") != row["page_id"]
        or payload.get("page_ordinal") != ordinal
    ):
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not carry the page id, page ordinal, "
            "derived page-residual key, and page rectangle it must bind"
        )
    grouping_digest = payload.get("grouping_config_sha256")
    if not isinstance(grouping_digest, str) or not grouping_digest:
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not name the sealed grouping "
            "configuration digest its residual bound was judged against"
        )
    sealed_grouping_digest = run_sealed_config_digests(context.run).get("designator-grouping")
    if grouping_digest != sealed_grouping_digest:
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold names grouping configuration digest "
            f"{grouping_digest!r}, which is not the designator-grouping digest "
            f"{sealed_grouping_digest!r} this run sealed at binding time; a Designator free to "
            "invent the grouping policy behind its bound could hold any page it likes"
        )
    sources = [
        source
        for source in context.run.get("source_manifest", [])
        if source.get("ordinal") == ordinal
    ]
    if len(sources) != 1:
        raise FatalAccounting(
            f"act {act_id}'s page ordinal {ordinal} does not name exactly one sealed source"
        )
    page = context.tree.read_artifact(
        EXEMPLAR, "page", artifact_id(EXEMPLAR, "page", row["page_id"])
    )
    verify_sealed_page_pixels(context.tree, context.run, sources[0], page)
    page_bytes = context.tree.read_bytes(page["payload"]["image_path"])
    width, height = dimensions(page_bytes)
    full_page_bounds = {"x": 0, "y": 0, "w": width, "h": height}
    if bounds != full_page_bounds:
        raise FatalAccounting(
            f"act {act_id}'s page-residual rectangle {bounds} is not the complete sealed page "
            f"rectangle {full_page_bounds}"
        )
    try:
        verify_identity(act_id, "act", act_bindings(row["page_id"], "page-residual", bounds))
    except IdentityRefusal as error:
        raise FatalAccounting(
            f"act {act_id} does not verify against the reserved page-residual class and the "
            f"page rectangle its own record names: {error}"
        ) from error
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
    payload = conservation.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    bound = hold_payload.get("max_residual_components")
    declared = hold_payload.get("residual_component_count")
    if not _is_count(bound) or not _is_count(declared):
        raise FatalAccounting(
            f"act {act_id}'s page-residual hold does not name an integer residual component "
            "count and the integer bound it was judged against"
        )
    enumeration = payload.get("residual_enumeration")
    if enumeration != RESIDUAL_ENUMERATION_WITHHELD:
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} for withheld residual enumeration, but that "
            f"page's own conservation record records its enumeration as {enumeration!r} rather "
            f"than {RESIDUAL_ENUMERATION_WITHHELD!r}; a page may not be held as one review item "
            "over a reconciliation that enumerated its components"
        )
    # Checked only once the record has already proven it means to withhold: a
    # record that enumerated its components is refused above for that alone,
    # whatever its outcome says, and folding this check in ahead of that one
    # would report the wrong reason for the same wrong record.
    outcome = conservation.get("outcome")
    if outcome != "held":
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} as one review item, but that page's own "
            f"conservation record reports its outcome as {outcome!r} rather than 'held'; a "
            "record standing behind a held page may not still say it was proposed"
        )
    if "residual_components" in payload:
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
    if measured <= bound:
        raise FatalAccounting(
            f"act {act_id} holds page {page_id} against a bound of {bound} residual components, "
            f"but that page's conservation record measured {measured}, which does not exceed it; "
            "a page whose reconciliation stays within the bound owes one held act per residual, "
            "not one held page"
        )


def _is_count(value: Any) -> bool:
    """A plain non-negative integer. `bool` is an `int` and is not a count."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _verify_residual_traces_to_conservation(
    context, act_id: str, page_id: str, hold: dict[str, Any], bounds: dict[str, Any]
) -> None:
    """A residual's declared bounds must exist in the reconciliation that found it.

    The check above only proves the hold is *internally* self-consistent — its
    own `residual_bounds` recompute the act id they sit beside. That alone
    would pass a residual invented from nothing, provided
    whoever invented it also recomputed the identity correctly: nothing yet
    opens the `conservation` artifact the hold's own `inputs` already
    reference and confirms a residual component with those bounds is actually
    in it. `hold_residual_act` publishes every residual hold with exactly one
    input, the conservation record it was minted from; reading through that
    reference — not by address, but through the digest-checked hop
    `RunTree.read_artifact_reference` provides — is what makes "checked
    against the conservation record that found it" true rather than aspirational.
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
    # Malformed payloads and rows must reach the named refusal below rather than
    # escape as attribute errors. Bounds are the residual's identity within its
    # class, so the Designator refuses coincident component boxes before minting;
    # this lookup can therefore match by bounds without an ordinal tie-breaker.
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

    Read the same way `_verify_proposal_seal_evidence` reads every act's
    evidence below, but ahead of it: the denominator check runs first, so an
    extra row must already name its own real evidence record before that later,
    more general evidence check ever sees it.
    """
    return {
        entry["subject_id"]: context.tree.read_artifact(DESIGNATOR, kind, entry["artifact_id"])
        for entry in context.tree.build_manifest(DESIGNATOR)["artifacts"]
        if entry["kind"] == kind
    }


def _proposal_evidence_by_subject(
    context, expected_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every proposal-origin region and every hold, by the act it is evidence for.

    The one walk of the Designator's region and hold records. It is the walk
    `_verify_proposal_seal_evidence` reconciles the seal against, and on a real
    run it is also what `_verify_real_act_denominator` classifies each row from,
    so the two share it rather than reading the stage twice.
    """
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

    The proposal seal is the sole downstream denominator, so it cannot be a
    shorter producer-authored list than the regions and holds actually published.
    Only original proposal regions belong to it; recovery regions are later,
    append-only evidence and must not rewrite the denominator.
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

    The synthetic-ingress branch of `open_stage_context`, which passes in the
    tree and the run authority it already read to decide the branch. Supplied
    together or not at all: a branch decided on one read and a binding checked
    on another would be the two-read fault `_verify_stage_seal` names, with a
    mode switch in it.
    """
    if (tree is None) != (run is None):
        raise ContractError(
            "open_context takes the run tree and its read authority together or neither; "
            "a binding checked against a second read of run.json can straddle a rewrite"
        )
    fixture = load_fixture(args.fixture_root)
    scenario_for(fixture, args.scenario)
    registry = registry_factory(args.models_config)
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
        serving_recipes_config_path=args.serving_recipes_config,
        decoding_config_path=args.decoding_config,
    )
    if tree is None:
        tree = RunTree(Path(args.run_root), args.run_id)
        run = tree.read_run()
    verify_snapshot_is_current(run, args.corpus_register)
    read_snapshot(tree, run)
    # `sealed_config_digests` is compared as a field of its own, not left to be
    # implied by `config_digest`. Every digest in it is inside `config_digest`, so
    # an equal digest already proves the *bytes*; what it does not prove is that
    # this build files those bytes under the same names the run recorded. A name
    # wired to the wrong digest — the F-S5 shape, where the real Door's map was
    # missing an entry the fixture path had — would otherwise be invisible until a
    # stage reached the point of use and refused with "sealed no digest".
    fields = ("config_digest", "adapter_recipes", "witness_chairs", SEALED_CONFIG_DIGESTS_FIELD)
    differing = [
        field
        for field in fields
        # A run authority written before this map existed is not "differing" and
        # is not rejected here: `StageContext.require_sealed_config` also
        # tolerates it per-name only in the sense that an absent name refuses
        # with "sealed no digest" at the point of use, and the orchestrator's
        # `run_sealed_config_digests` is the one reader that refuses such an
        # authority outright. Rejecting it here instead would name a
        # configuration nobody changed.
        if (field != SEALED_CONFIG_DIGESTS_FIELD or field in run)
        and run.get(field) != bindings[field]
    ]
    if differing:
        # Name the sealed policies that actually moved, not only the field that
        # holds them. "different config_digest, sealed_config_digests" is true of
        # every one of the family's members and sends an operator to read ten
        # files to find the one that changed; a resume refused because
        # `config/decoding.toml` was edited should say `decoding`. Unit 2's
        # definition of done asks for exactly this by name, and the sentence it
        # asks for is the same sentence every other sealed policy needs.
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
    return StageContext(
        tree=tree,
        run=run,
        fixture=fixture,
        scenario=args.scenario,
        stage=stage,
        # No stage names its own recipe any more: once a run exists, the sealed
        # `run.json` is the only source for the producer revision, so a stage
        # program and the run authority cannot drift on what answered.
        adapter_revision=adapter_recipe_for(run, stage),
        args=args,
        registry=registry,
        sealed_config_digests=bindings["sealed_config_digests"],
        armarium_formats=bindings["armarium_formats"],
        serving_config_inputs=bindings["serving_config_inputs"],
        recovery_policy=bindings["recovery_policy"],
    )


def open_stage_context(
    args,
    stage: str,
    *,
    registry_factory: Callable[[str], StageChairProtocol] = ChairRegistry.from_toml,
) -> StageContext:
    """Open an existing run for any stage after the Door, on either ingress route.

    One read of the run authority decides the route and is then passed down,
    never re-read: the ingress record is inside `run.json`'s own self-hash, so
    which of the two routes created a run cannot be quietly switched, and a
    branch decided on one read with a binding checked on another would be the
    "two reads can straddle a rewrite" fault with a mode switch in it. The
    synthetic route is `open_context`, unchanged; the real route is
    `_open_real_context`.
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

    Mirrors `open_context` step for step, so the two routes refuse the same
    things in the same order: the register snapshot, the roster, the binding
    recheck, the predecessor seal, the run-level cap, then construction.

    **The real `config_digest` is not recomputed here, and must not be.** The
    Door composes it over the submission ledger's file list and self-hash, the
    format policy, the data-handling policy that gated admission, the triage
    documents, and `_door_execution_recipe` -- the PDFium and Pillow versions of
    the machine that ran the Door. A later stage holds none of those inputs, and
    for the execution recipe it must not: recomputing it would bind the
    Armarium to the Door's decoder build and refuse a sound run after a library
    upgrade. So `open_context`'s whole-digest comparison has no counterpart on
    this route, and what stands in for it is the name-by-name recheck of the
    sealed map (`_refuse_incompatible_real_reuse`), which is why the Door seals
    `models`, `armarium-formats` and `run-policy` on the real path alone: they
    are the facts stages 3-7 act on that the whole digest would otherwise have
    been the only thing covering.

    The context carries `fixture=None` (behind the refusing accessor) and
    `REAL_SCENARIO`, never `args.scenario`: the real digest binds no scenario,
    so an argv value there would be unchecked.
    """
    verify_snapshot_is_current(run, args.corpus_register)
    read_snapshot(tree, run)
    registry = registry_factory(args.models_config)
    bindings = real_run_bindings(registry.config, args)
    # Before any write and before the seal check, so a moved policy is named as
    # a policy and not as a missing boundary.
    _refuse_incompatible_real_reuse(run, bindings, run_id=args.run_id)
    verify_predecessor_seal(tree, stage)
    refuse_halted_run(tree, stage, args.hard_failure_config)
    return StageContext(
        tree=tree,
        run=run,
        fixture=None,
        scenario=REAL_SCENARIO,
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

    The same class `open_context` raises, so operators and the operator console
    see one shape; the same closing sentence, because on this route the promise
    that nothing was written is the whole value of checking at open time. Every
    fact that moved is named in one sentence, and an absent name is named apart
    from a changed one -- `require_sealed_config`'s distinction -- because they
    need different operator actions: restore the file, versus create the run
    again on a build that seals the name. A real run created before the three
    real-only names existed therefore cannot be resumed under this build.
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
    named = list(differing)
    if moved:
        named.append(f"sealed configuration {', '.join(moved)} moved")
    if absent:
        named.append(
            f"this run sealed no digest for the {', '.join(absent)} configuration, so a "
            "stage cannot prove which bytes it is bound to"
        )
    raise IncompatibleReuse(
        f"run {run_id!r} is bound to different {'; '.join(named)} than the currently loaded "
        "run inputs. No stage work was written. Resume with the original sealed inputs, or "
        "start a new run for the changed inputs"
    )


def submission_identity(run: Mapping[str, Any]) -> str | None:
    """The one identity a real submission carries: its filename ledger's self-hash.

    Every real source row carries `ledger_sha256`, it is one value across the
    run, and `pipeline/1_exemplar/run.py::_verify_source_ledger` already proves
    it reproduces the self-hashed ledger that admitted the submission. `None` on
    the fixture route, which has a fixture id instead; the two are different
    concepts and are never written under one name.
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

    The one index of "which page is ordinal N" for both ingress routes: the
    Exemplar's own `page` artifacts are the only correct source, because a
    fanned container page binds its container digest, page index and render
    contract into its identity, which neither the fixture declaration nor the
    run authority's manifest row carries. A refused page keeps its ordinal so a
    reader can tell "refused" from "never submitted". This is the Ink Map's
    census generalised, minus the pixel verification that belongs to the Ink
    Map: it says which page an ordinal names, not that the page's bytes are
    sound.

    A sealed page citing more than one submitted row would leave the other
    rows' ordinals with no entry, so it refuses by name rather than dropping
    them; the Door refuses such a submission before the Exemplar ever runs.
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
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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
    # Hand-built stage-seal fixtures predate named policies and carry no ingress;
    # synthetic-ingress fixtures may also construct one narrow boundary by hand.
    # A real run always names this policy: losing that name is a failed proof,
    # never permission to continue without the cap.
    if not isinstance(sealed_digests, Mapping) or "hard-failure" not in sealed_digests:
        if "ingress" in run and parse_ingress_record(run["ingress"]) == REAL_INGRESS:
            raise ContractError(
                f"{stage} refuses to start: this real run authority seals no hard-failure "
                "configuration digest, so its run-level cap cannot be proven"
            )
        return
    policy = load_hard_failure_policy(hard_failure_config_path)
    require_sealed_config(run_sealed_config_digests(run), "hard-failure", policy["config_sha256"])
    # An unreadable tally record is a failed measurement, not permission to
    # enter. Input-byte consistency belongs to each stage's consumer boundary,
    # though: recursively checking every tally artifact here can intercept
    # unrelated lineage damage before the owning boundary names it. The tally
    # still validates every record whose outcome and subject it measures.
    tally = tally_hard_failures(tree, policy, verify_inputs=False)
    if tally["breached"]:
        raise RunHalted(
            f"{stage} refuses to start: {tally['count']} hard failure(s) exceed the run-level "
            f"cap of {tally['threshold']}; no stage writes after a halted run"
        )


def run_stage(main) -> int:
    """Run a stage's main and turn a contract refusal into an honest exit code.

    A stage that crashed with a traceback and a zero exit would be the vacuous
    green this project exists to notice, so the only paths out of here are an
    explicit code or a non-zero one.
    """
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

    "Current" is derived, never stored as a pointer — Tyrel's retention ruling of
    2026-07-30 — so this is the one place that derivation happens and the only
    place it can be got wrong.

    A record with no attempt ordinal is FATAL rather than treated as ordinal zero.
    That defaulting cost this build an hour: three readers each defaulted a missing
    ordinal to 0, `>` never fired, and each picked whichever record the filesystem
    listed first. The orchestrator read a stale "recovery-requested" that way and
    dispatched a second recrop over the top of the first. An evidence channel that
    cannot be read makes the answer unknown; it never resolves in the run's favour.

    **`operation` is what binds the ordinal to the sealed identity, and it is why
    this function cannot be called without naming one.** The envelope proves that
    `artifact_id` derives from `attempt_id`, and stops there: it takes the attempt
    token as an opaque well-formed string and never re-derives it from the
    subject/operation/ordinal it is supposed to bind. `attempt_ordinal` lives in the
    payload, outside that derivation, and this function used to select on it alone.
    So the field that decides which reading is current was the one field in the
    chain nothing recomputed. Demonstrated on a real run tree before this change: a
    second `perlectio` for one act, carrying different text and `attempt_ordinal:
    99`, validated as an envelope and became the current reading over the record
    that had actually read the ink.

    The caller knows its own operation — the Perlector reads `perlegere`, the
    Recensor recenses, a chair reads `read:<chair>` — so the identity can be
    recomputed here without the envelope growing a field, and `skeleton.v1` does not
    have to become `skeleton.v2` to close it. A richer envelope carrying the
    operation and the ordinal would be the more general answer, and it stays open;
    this is the reader-side half of it, which needs no migration.

    Ordinals must also be the contiguous run 1..N. Attempts are append-only and
    never reused, so a gap means an attempt that existed is no longer here — which
    is the one thing GOVERNANCE 2 does not allow to pass quietly — and it is also
    what stops an honestly-derived attempt 99 from being manufactured beside
    attempt 1 and outranking it.
    """
    if not records:
        raise FatalAccounting(f"no {what} to derive a current outcome from")
    ordinals: dict[int, str] = {}
    for record in records:
        ordinal = record.get("payload", {}).get("attempt_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
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

    A recovery request is not self-authorizing merely because its own envelope is
    well formed.  The Recensor review is the decision that makes it current; the
    request, review, Perlectio, and run-bound policy must therefore form one
    closed, digest-checked chain.  Both the dispatcher and the sole crop author
    use this one check so a direct Designator invocation cannot bypass the
    orchestrator's view of the recovery loop.
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
    ordinal = review_payload.get("attempt_ordinal")
    if (
        not isinstance(request_ref, dict)
        or request_ref not in review.get("inputs", [])
        or not isinstance(reading_ref, dict)
        or reading_ref not in review.get("inputs", [])
        or not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
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
    # The Recensor performs the fuller request/recrop/reread reconciliation, but a
    # non-Recensor consumer reads a current request directly, so its counters are
    # rebuilt here too: the self-hash proves the payload was not edited after
    # publication, not that its numbers ever agreed with the requests before it.
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
        or not isinstance(kind_used, int)
        or isinstance(kind_used, bool)
        or kind_used < 0
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
    """Return a completed Perlectio's regions without trusting an untyped payload.

    An artifact envelope closes transport shape, not every stage payload.  The
    three consumers of a completed Perlectio must therefore refuse a resealed
    `basis=[]` (or malformed region) as accounting evidence, rather than indexing
    into it and escaping through an accidental traceback.
    """
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

    One accounting rule with three consumers, so it lives here beside
    `latest_attempt`, `reading_basis_regions` and `expected_acts` for the reason
    those do: the three had drifted. `pipeline/5_recensor/run.py::recovery_state`
    refuses a region whose `origin` is outside `{"proposal", "recovery"}`, while
    the Archetypus and Armarium copies asked only whether it equalled `"recovery"`
    and counted anything else as zero. The same tree was therefore fatal at the
    Recensor and reconciled at the Archetypus — a region with an unrecognized
    origin silently left the recovery denominator at exactly the two stages that
    decide whether a recrop was reread before its text is established.
    """
    count = 0
    for region in regions:
        payload = region.get("payload")
        if not isinstance(payload, dict):
            raise FatalAccounting(f"Designator region of {act_id} has no object payload")
        origin = payload.get("origin")
        # `isinstance` first: `origin not in {...}` raises `TypeError` on an
        # unhashable value, so a resealed region carrying a list or an object where
        # its origin belongs escaped as a traceback out of the very check written to
        # name it. All three copies of this rule had that hole.
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

    Attestatores attempts are append-only per (act, chair) — a failed re-read shows
    as `failed` with the earlier success intact as history (GOVERNANCE 4) — so any
    consumer of a flat list of testimonium records for one act has to collapse each
    chair's own history down to its current attempt before treating the group as
    evidence. `pipeline/5_recensor/run.py` did this collapsing inline;
    `pipeline/4_perlector/run.py` read the same artifacts unfiltered, so once a
    chair gained a second attempt the two consumers of one upstream contract would
    disagree about what "current" means — one dissent row per attempt instead of
    per chair, and a since-superseded `read` still marking a region witness-covered.
    One derivation, reused by both, is what keeps that from recurring.
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

    The counterpart, on the testimony side, of the newer-Perlectio check the
    Archetypus and Armarium already make on the reading side. Both stages derive
    everything they say about an act from the *latest* Recensor review and from
    the reading's own basis references, and neither route passes back through
    `latest_per_chair`. So a Testimonium appended after the reading was
    established is structurally invisible at the point where the export decides
    whether to say `complete`, and the sealed export keeps saying it (audit
    Opus-F2, 2d).

    GOVERNANCE 2 is the rule this serves and it is unconditional: "'complete' is
    refused unless everything reconciles." A basis citing an attempt that has
    since been superseded has not reconciled, whatever the reading itself says.
    `pipeline/3_attestatores/run.py::require_open_witness_layer` closes the door
    that makes this state reachable through the stage programs at all; this is the
    structural refusal for a folder assembled, resumed or resealed some other way,
    and it is deliberately independent of that one.

    Held and absent-chair readings cite no testimony and are passed over: their
    bytes do not depend on any Testimonium, so nothing about them can be
    superseded.
    """
    basis = reading.get("payload", {}).get("basis")
    cited = basis.get("testimonia") if isinstance(basis, dict) else None
    if not cited:
        return
    if not isinstance(cited, list):
        # A truthy non-list would either iterate its fragments into the
        # per-entry refusal below or crash as a bare TypeError; a malformed
        # basis is refused as itself instead.
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
