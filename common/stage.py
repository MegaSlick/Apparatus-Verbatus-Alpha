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
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable, Final, Protocol

from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig, ServingDetails
from common.chairs.protocol import ChairProtocol
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes, digest_of, verify_self_hash
from common.contracts.envelope import build_envelope
from common.contracts.errors import ContractError, FatalAccounting, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import artifact_id, attempt_id
from common.contracts.outcomes import classify
from common.contracts.stages import DESIGNATOR, PERLECTOR, RECENSOR
from common.hard_failure import DEFAULT_HARD_FAILURE_CONFIG_PATH, load_hard_failure_policy
from common.recovery import (
    DEFAULT_RECOVERY_CONFIG_PATH,
    RECOVERY_KINDS,
    load_recovery_policy,
    reconcile_recovery_requests,
    recovery_kind_budget,
)
from common.runtree.store import PublishResult, RunTree

# Exit codes carry cause, per harvest invariant #11. The old contract worth
# keeping: 0 = complete, 2 = structural or fatal, 3 = accounted but holdable.
# A stage that failed structurally and exited 0 is how a run reports success over
# work it never did.
EXIT_COMPLETE = 0
EXIT_FATAL = 2
EXIT_HELD = 3

# Never a stage's own exit code, only the orchestrator's: only that process
# decides whether to invoke another stage, so only it can halt a run for the
# run-level hard-failure cap. Defined beside the three above, not in the
# orchestrator module, so nothing can silently pick a fourth value that
# collides with one of these.
EXIT_RUN_HALTED = 4

DEFAULT_PDF_RENDER_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "pdf_render.toml"
DEFAULT_WITNESS_CONTEXT_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "witness_context.toml"
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
WITNESS_READING_OUTCOMES = frozenset({"read", "genuinely-empty"})

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
    }
)


class StageChairProtocol(ChairProtocol, Protocol):
    """The small additional config surface a calling stage needs.

    A stage receives this explicitly rather than knowing which registry
    implementation made it.  The production default is ``ChairRegistry``; the
    deterministic fake beside the tests is a separate implementation of this
    protocol, not a subclass or import of it.
    """

    config: ModelsConfig


class StageContext:
    """One stage's view of the run it is part of."""

    __slots__ = (
        "tree",
        "run",
        "fixture",
        "scenario",
        "stage",
        "adapter_revision",
        "args",
        "registry",
    )

    def __init__(self, tree, run, fixture, scenario, stage, adapter_revision, args, registry):
        self.tree = tree
        self.run = run
        self.fixture = fixture
        self.scenario = scenario
        self.stage = stage
        self.adapter_revision = adapter_revision
        self.args = args
        self.registry = registry

    @property
    def config_digest(self) -> str:
        return self.run["config_digest"]

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
        """Tyrel's reference for the sampling design this run draws nuda under.

        Empty when nothing is sampled. `run_config_bindings` refuses a non-zero
        rate that carries none, so a populated rate always has one.
        """
        return self.args.nuda_approval_ref

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


def stage_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    # No `choices` here: the fixture declares which scenarios exist, and a
    # hard-coded list in a second place is a drift surface. `scenario_for`
    # refuses an undeclared name after the fixture is loaded.
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--fixture-root", default="proof")
    parser.add_argument("--models-config", default="config/models.toml")
    parser.add_argument("--pdf-render-config", default=str(DEFAULT_PDF_RENDER_CONFIG_PATH))
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
    parser.add_argument("--act", default=None, help="one act id, for a recovery operation")
    parser.add_argument(
        "--recovery-request",
        default=None,
        help="the exact Recensor recovery-request artifact a Designator recrop answers",
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
    return fixture


def run_config_bindings(
    models: ModelsConfig,
    fixture: dict[str, Any],
    scenario: str,
    *,
    pdf_render_config_path: str | Path = DEFAULT_PDF_RENDER_CONFIG_PATH,
    pdf_target_dpi: int | None = None,
    recovery_config_path: str | Path = DEFAULT_RECOVERY_CONFIG_PATH,
    hard_failure_config_path: str | Path = DEFAULT_HARD_FAILURE_CONFIG_PATH,
    witness_context: str = "named",
    witness_context_config_path: str | Path = DEFAULT_WITNESS_CONTEXT_CONFIG_PATH,
    nuda_per_mille: int = 0,
    nuda_approval_ref: str = "",
) -> dict[str, Any]:
    """The three `run.json` bindings, and everything that shapes them.

    Since spec 02 `config/models.toml` owns the roster, the witness floor and
    the adapter recipes, so two of the three come straight off it. The third,
    `config_digest`, is the digest of *everything* that shapes this run's
    behaviour — the model configuration, fixture, scenario, PDF-render settings,
    recovery policy, and the run-level hard-failure policy. The synthetic fixture
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
    try:
        pdf_render_config_digest = digest_bytes(Path(pdf_render_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            f"the PDF render configuration binding at {pdf_render_config_path} could not be read"
        ) from error
    recovery_policy = load_recovery_policy(recovery_config_path)
    hard_failure_policy = load_hard_failure_policy(hard_failure_config_path)
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
    if nuda_per_mille and not nuda_approval_ref:
        raise ContractError(
            f"a Lectio nuda rate of {nuda_per_mille}/1000 needs Tyrel's predeclared sampling "
            "design reference in --nuda-approval-ref; an unapproved instrument sample is a "
            "decision this pipeline does not get to make for him"
        )
    try:
        witness_context_config_digest = digest_bytes(Path(witness_context_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            f"the witness-context declaration at {witness_context_config_path} could not be read"
        ) from error
    return {
        "witness_chairs": list(models.witness_chairs),
        "config_digest": digest_of(
            {
                "fixture": fixture,
                "scenario": scenario,
                "models": models.to_record(),
                "pdf_render_config_sha256": pdf_render_config_digest,
                "pdf_target_dpi_override": pdf_target_dpi,
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
            }
        ),
        "adapter_recipes": dict(sorted(models.adapter_recipes.items())),
    }


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
    addressed = set(models.witness_chairs) | {DESIGNATOR_CHAIR, PERLECTOR_CHAIR}
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

    Two consequences worth knowing before spec 04 replaces this with a real
    serving manager. Endpoint and start time are confined to the run receipt, so
    a stage payload carries only the content-addressed reference to one. And
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
        endpoint="fixture://offline-seat-runner",
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
    _verify_synthetic_act_denominator(context, acts)
    _verify_proposal_seal_evidence(context, seal, acts)
    return acts


def _verify_synthetic_act_denominator(context, acts: list[dict[str, Any]]) -> None:
    """Bind the skeleton's discovered-act denominator to its sealed fixture input.

    This check belongs only to the declared synthetic walking skeleton: its fake
    Designator derives every act from fixture data, and the run configuration
    seals those fixture bytes.  Real ingress intentionally stops before any
    proposal seal exists, so no unbuilt structural model is being prescribed.
    """
    fixture_acts = context.fixture.get("act", [])
    if not fixture_acts:
        return
    expected = {
        act_identity(context.fixture, row): {
            "act_key": row["key"],
            "page_id": page_identity(context.fixture, row["page_ordinal"]),
            "page_ordinal": row["page_ordinal"],
            "has_continuation": continuation_for(context.fixture, row["key"]) is not None,
        }
        for row in fixture_acts
    }
    observed = {act["act_id"]: act for act in acts}
    if set(observed) != set(expected):
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


def _verify_proposal_seal_evidence(
    context, seal: dict[str, Any], acts: list[dict[str, Any]]
) -> None:
    """Reconcile the immutable expected-act denominator to Designator evidence.

    The proposal seal is the sole downstream denominator, so it cannot be a
    shorter producer-authored list than the regions and holds actually published.
    Only original proposal regions belong to it; recovery regions are later,
    append-only evidence and must not rewrite the denominator.
    """
    expected_ids = {act["act_id"] for act in acts}
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
) -> StageContext:
    """Open an existing run for a stage that is not the first to write."""
    fixture = load_fixture(args.fixture_root)
    scenario_for(fixture, args.scenario)
    registry = registry_factory(args.models_config)
    bindings = run_config_bindings(
        registry.config,
        fixture,
        args.scenario,
        pdf_render_config_path=args.pdf_render_config,
        pdf_target_dpi=args.pdf_target_dpi,
        recovery_config_path=args.recovery_config,
        hard_failure_config_path=args.hard_failure_config,
        witness_context=args.witness_context,
        witness_context_config_path=args.witness_context_config,
        nuda_per_mille=args.nuda_per_mille,
        nuda_approval_ref=args.nuda_approval_ref,
    )
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    differing = [
        field
        for field in ("config_digest", "adapter_recipes", "witness_chairs")
        if run.get(field) != bindings[field]
    ]
    if differing:
        raise IncompatibleReuse(
            f"run {args.run_id!r} is bound to different {', '.join(differing)} than "
            "the currently loaded models config and fixture scenario; direct stages "
            "may not run against an unsealed configuration"
        )
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
    )


def run_stage(main) -> int:
    """Run a stage's main and turn a contract refusal into an honest exit code.

    A stage that crashed with a traceback and a zero exit would be the vacuous
    green this project exists to notice, so the only paths out of here are an
    explicit code or a non-zero one.
    """
    try:
        return int(main() or EXIT_COMPLETE)
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


def page_for(fixture: dict[str, Any], ordinal: int) -> dict[str, Any]:
    for page in fixture["page"]:
        if page["ordinal"] == ordinal:
            return page
    raise ContractError(f"the fixture declares no page {ordinal}")


def page_identity(fixture: dict[str, Any], ordinal: int) -> str:
    """Every stage derives this the same way rather than looking it up.

    Identity derivation lives in one place so six stage programs cannot drift on
    how a page is named. The fixture supplies the source digest; the contract
    supplies the derivation.
    """
    from common.contracts.identities import page_id

    return page_id(page_for(fixture, ordinal)["sha256"], ordinal)


def act_bounds(act: dict[str, Any]) -> dict[str, int]:
    """The act's original proposal bounds — what act identity is bound to."""
    return {"x": act["x"], "y": act["y"], "w": act["w"], "h": act["h"]}


def act_identity(fixture: dict[str, Any], act: dict[str, Any]) -> str:
    from common.contracts.identities import act_id

    return act_id(
        page_identity(fixture, act["page_ordinal"]),
        act["proposal_ordinal"],
        act_bounds(act),
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
