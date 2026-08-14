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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Final, Protocol

from common.chairs.models import AbsentChair, ChairIdentity, ModelsConfig, ServingDetails
from common.chairs.protocol import ChairProtocol
from common.chairs.registry import ChairRegistry
from common.contracts.canonical import digest_bytes, digest_of, verify_self_hash
from common.contracts.envelope import build_envelope
from common.contracts.errors import (
    ContractError,
    FatalAccounting,
    IdentityRefusal,
    IncompatibleReuse,
    SchemaRefusal,
)
from common.contracts.identities import act_bindings, artifact_id, attempt_id
from common.contracts.identities import verify as verify_identity
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

# The Designator's capture padding decides how many pixels a witness is actually
# shown around each act, so two runs under different padding produce different
# crop bytes. Sealing its digest here is what makes reusing one run id across a
# padding change a refusal rather than a silent second geometry — the same
# reason `pdf_render.toml` is sealed.
DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "designator_padding.toml"
)

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
        "sealed_config_digests",
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
    ):
        self.tree = tree
        self.run = run
        self.fixture = fixture
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
        # first read's bytes. Empty for a context built without one (real
        # ingress, which reaches no configuration-driven work).
        self.sealed_config_digests = dict(sealed_config_digests or {})

    @property
    def config_digest(self) -> str:
        return self.run["config_digest"]

    def require_sealed_config(self, name: str, observed_sha256: str) -> None:
        """Refuse a configuration whose bytes changed after this run bound them."""
        sealed = self.sealed_config_digests.get(name)
        if sealed is None:
            raise ContractError(
                f"this context sealed no digest for the {name} configuration, so the bytes "
                "a stage just read cannot be proven to be the ones this run is bound to"
            )
        if sealed != observed_sha256:
            raise ContractError(
                f"the {name} configuration changed between this run's binding check and the "
                f"read that used it: bound {sealed}, read {observed_sha256}. A stage may not "
                "work under a policy the run never sealed"
            )

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
    parser.add_argument(
        "--designator-padding-config", default=str(DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH)
    )
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


def validate_witness_context_bindings(
    models,
    *,
    witness_context: str,
    witness_context_config_path: str | Path,
    nuda_per_mille: int,
    nuda_approval_ref: str,
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
    if nuda_per_mille and not nuda_approval_ref.strip():
        raise ContractError(
            f"a Lectio nuda rate of {nuda_per_mille}/1000 needs Tyrel's predeclared sampling "
            "design reference in --nuda-approval-ref; an unapproved instrument sample is a "
            "decision this pipeline does not get to make for him"
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


def run_config_bindings(
    models: ModelsConfig,
    fixture: dict[str, Any],
    scenario: str,
    *,
    pdf_render_config_path: str | Path = DEFAULT_PDF_RENDER_CONFIG_PATH,
    designator_padding_config_path: str | Path = DEFAULT_DESIGNATOR_PADDING_CONFIG_PATH,
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
    try:
        padding_config_digest = digest_bytes(Path(designator_padding_config_path).read_bytes())
    except OSError as error:
        raise ContractError(
            "the Designator padding configuration binding at "
            f"{designator_padding_config_path} could not be read"
        ) from error
    recovery_policy = load_recovery_policy(recovery_config_path)
    hard_failure_policy = load_hard_failure_policy(hard_failure_config_path)
    witness_context_config_digest = validate_witness_context_bindings(
        models,
        witness_context=witness_context,
        witness_context_config_path=witness_context_config_path,
        nuda_per_mille=nuda_per_mille,
        nuda_approval_ref=nuda_approval_ref,
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
        # Not a run.json binding — every caller writing a run takes the three
        # above by name. This is the record of which bytes each digest above was
        # taken over, so a stage that re-reads one of these files for its values
        # can prove it read what was bound (`StageContext.require_sealed_config`).
        # Only `designator-padding` has such a point-of-use re-read today: the
        # Door's own second read of the PDF-render policy (`door.py`) neither
        # takes a sealed digest nor calls `require_sealed_config`, so sealing a
        # `pdf-render` entry here would read as a closed TOCTOU window that is
        # not actually wired shut. Not this stage's window to close.
        "sealed_config_digests": {
            "designator-padding": padding_config_digest,
        },
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

    The fixture's own acts are a *floor*, never a ceiling: every one must
    appear, and the seal may also carry acts the fixture never declared. Two
    kinds exist, and neither is fixture data, so neither can be checked against
    it — `_verify_minted_act_rows` checks each against the one thing it *can*
    be checked against: its own Designator evidence record, recomputed rather
    than trusted. A **residual** act (`_publish_residual_holds`) is ink
    conservation found that structural grouping never claimed, and is `held`. A
    **page-fallback** act (`_publish_page_fallback`) is the predetermined crop
    grid cut over a page the structure pass found nothing on, and is `proposed`,
    because the whole point of cutting it is that it goes downstream to be read.
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
    _verify_minted_act_rows(
        context, {act_id: observed[act_id] for act_id in sorted(set(observed) - set(expected))}
    )


# An act identity is `act_bindings(page_id, ordinal, bounds)`, so two acts minted
# on one page must never share an ordinal. Three classes of ordinal exist on a
# page and they are kept disjoint here, in one place, rather than by each minter
# knowing what the others do:
#
#   >= 0                          the nth region a structure pass proposed
#   RESIDUAL_FLOOR .. -1          one conservation residual (`residual_act_ordinal`)
#   FALLBACK_PAGE_ACT_ORDINAL     the one page-fallback act, below that floor
#
# The residual space used to be the *whole* negative range, which left nowhere
# for a second minted class to live without an argument about which values were
# "unlikely" to be reached. Bounding it below makes the fallback ordinal
# unreachable from it by construction, and `residual_act_ordinal` refuses an
# index that would cross the floor rather than silently minting a colliding
# identity. The floor is far past any reachable residual count -- this stage's
# own worst measured page reconciles to about 60,000 residual components
# (`pipeline/2_designator/HANDOFF.md`, "Cost, and where it is unbounded") --
# so no existing ordinal moves and none ever will in practice; the point of the
# bound is that the disjointness is proven rather than assumed.
RESIDUAL_ACT_ORDINAL_FLOOR: int = -(2**31)
FALLBACK_PAGE_ACT_ORDINAL: int = RESIDUAL_ACT_ORDINAL_FLOOR - 1


def residual_act_ordinal(index: int) -> int:
    """The disjoint ordinal namespace a conservation-residual act's identity uses.

    `identities.act_id`'s own `proposal_ordinal` is always the non-negative "nth
    region a structure pass proposed on a page" — a residual was never such a
    proposal, so it cannot borrow that ordinal space without risking collision
    with a real one, present or future. `-(index + 1)` is disjoint from every
    non-negative ordinal by construction rather than by convention, so no
    structure pass — this fixture's or a real one's — can ever mint a genuine
    proposal whose identity collides with a residual's. `index` is the
    residual's position in `conservation.reconcile`'s own `residual_components`
    list, which is already deterministically ordered (top, then left) by the
    shared `structure.label_components`, so the same page reconciles to the
    same ordinal on every run.

    The minting code and this module's verification of what was minted call the
    same function, so the two cannot drift on what `-(index + 1)` means.

    Bounded below by `RESIDUAL_ACT_ORDINAL_FLOOR` so the page-fallback act's own
    reserved ordinal cannot be reached from here. An index that would cross the
    floor is refused rather than allowed to mint an identity that could collide
    with a fallback act on the same page.
    """
    if index < 0:
        raise ContractError(f"a residual index {index} is negative and names no residual")
    ordinal = -(index + 1)
    if ordinal < RESIDUAL_ACT_ORDINAL_FLOOR:
        raise ContractError(
            f"residual index {index} is past the residual ordinal floor "
            f"{RESIDUAL_ACT_ORDINAL_FLOOR}; a page this speckled has to be refused as a page "
            "rather than accounted for with an ordinal that collides with another minted act"
        )
    return ordinal


def fallback_page_act_key(page_ordinal: int) -> str:
    """The human-readable label of the one act a page's fallback crops belong to.

    For a reviewer's eye and for `expected_acts`'s duplicate-key refusal; what
    keeps this act's *identity* from colliding with anything is
    `FALLBACK_PAGE_ACT_ORDINAL`, not this string. Named here beside the ordinal
    rather than in the Designator so the producer and this module's verification
    of what was produced cannot spell it differently.
    """
    return f"page-fallback:{page_ordinal}"


def _verify_minted_act_rows(context, extra_rows: dict[str, dict[str, Any]]) -> None:
    """Every expected-act row beyond the fixture's own denominator.

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

    The evidence index is built once for the whole set rather than per row.
    Every residual component on a page mints one of these rows, and a speckled
    or foxed page reconciles to tens of thousands of them, so a per-row walk of
    the stage's whole artifact tree makes ordinary input quadratic in itself.
    """
    holds_by_subject = _designator_records_by_subject(context, "hold") if extra_rows else {}
    fallbacks_by_subject = (
        _designator_records_by_subject(context, "page-fallback") if extra_rows else {}
    )
    for act_id, row in extra_rows.items():
        if row["has_continuation"]:
            raise FatalAccounting(
                f"act {act_id} extends the denominator beyond the fixture but claims a "
                "continuation; a residual has no declared continuation to claim, and neither "
                "has a page-fallback act"
            )
        if row["outcome"] == "proposed":
            _verify_page_fallback_act_row(context, act_id, row, fallbacks_by_subject)
            continue
        if row["outcome"] != "held":
            raise FatalAccounting(
                f"act {act_id} is not declared in the sealed fixture and is neither 'held' nor "
                "'proposed'; the only units that may extend the denominator beyond the fixture "
                "are a conservation residual and a page-fallback act"
            )
        hold = holds_by_subject.get(act_id)
        if hold is None:
            raise FatalAccounting(
                f"act {act_id} extends the denominator beyond the fixture but the Designator "
                "published no hold record for it"
            )
        payload = hold.get("payload") if isinstance(hold.get("payload"), dict) else {}
        ordinal, bounds = payload.get("residual_ordinal"), payload.get("residual_bounds")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal >= 0
            or not isinstance(bounds, dict)
        ):
            raise FatalAccounting(
                f"act {act_id}'s hold record carries no valid residual ordinal and bounds to "
                "recompute its identity from"
            )
        try:
            verify_identity(act_id, "act", act_bindings(row["page_id"], ordinal, bounds))
        except IdentityRefusal as error:
            raise FatalAccounting(
                f"act {act_id} does not verify against the residual ordinal and bounds its own "
                f"hold record names: {error}"
            ) from error
        _verify_residual_traces_to_conservation(
            context, act_id, row["page_id"], hold, ordinal, bounds
        )


def _verify_page_fallback_act_row(
    context, act_id: str, row: dict[str, Any], fallbacks_by_subject: dict[str, dict[str, Any]]
) -> None:
    """The one extra row that may be `proposed`, checked against its own evidence.

    A page-fallback act is the only unit outside the fixture that reaches the
    witnesses and the Perlector, so it is the one whose provenance most needs to
    be recomputed rather than believed. Two independent things are checked, and
    the second is what stops a fabricated one: the identity must derive from
    this page and the one reserved fallback ordinal over the page rectangle its
    own record declares, and that record's single input must be the page's
    `structure-status` — read through the digest-checked hop, not by address —
    saying the structure pass genuinely fell back to tiles on that page. A
    fallback act minted over a page whose structure pass *did* detect something
    therefore refuses here, which is exactly the claim-about-what-was-measured
    GOVERNANCE 10 forbids.
    """
    record = fallbacks_by_subject.get(act_id)
    if record is None:
        raise FatalAccounting(
            f"act {act_id} extends the denominator beyond the fixture as a proposed act but the "
            "Designator published no page-fallback record for it; it is not 'held' either, so it "
            "is not a conservation residual"
        )
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    bounds = payload.get("page_bounds")
    if not isinstance(bounds, dict) or payload.get("act_key") != row["act_key"]:
        raise FatalAccounting(
            f"act {act_id}'s page-fallback record carries no page rectangle and act key to "
            "recompute its identity from"
        )
    try:
        verify_identity(
            act_id, "act", act_bindings(row["page_id"], FALLBACK_PAGE_ACT_ORDINAL, bounds)
        )
    except IdentityRefusal as error:
        raise FatalAccounting(
            f"act {act_id} does not verify against the reserved page-fallback ordinal and the "
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


def _verify_residual_traces_to_conservation(
    context, act_id: str, page_id: str, hold: dict[str, Any], ordinal: int, bounds: dict[str, Any]
) -> None:
    """A residual's declared bounds must exist in the reconciliation that found it.

    The check above only proves the hold is *internally* self-consistent — its
    own `residual_ordinal` and `residual_bounds` recompute the act id they sit
    beside. That alone would pass a residual invented from nothing, provided
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
    # Every step of this lookup is checked before it is taken, and all of it lands
    # on the one named refusal below. Read straight through, a conservation record
    # whose payload was not a mapping or whose indexed row was not a mapping raised
    # `AttributeError` out of an accounting check — a traceback where invariant #10
    # promises a named fatal.
    #
    # **The lower bound is for a corrupted ordinal, not a short list**, and the
    # distinction cost a vacuous test before it was understood. `residual_act_ordinal`
    # is `-(index + 1)` over a non-negative index, so `index = -ordinal - 1` recovers
    # a non-negative index for every *well-formed* record and `index >= len(components)`
    # bounds it correctly. A hold whose sealed `ordinal` has been corrupted to a
    # positive value is the reachable case: it makes `index` negative, which
    # `index >= len(...)` never catches, and `components[-3]` on a short list is an
    # `IndexError`. Reaching it in a test needs the ordinal itself corrupted after
    # minting — the minting helper cannot produce one, because it calls
    # `residual_act_ordinal`, which refuses. Left uncovered and named rather than
    # covered by a test that passes against the unfixed code, which is what a first
    # attempt at one did. Found by CodeRabbit; the reachability corrected here.
    payload = conservation.get("payload")
    components = payload.get("residual_components") if isinstance(payload, Mapping) else None
    index = -ordinal - 1
    if (
        not isinstance(components, list)
        or not -len(components) <= index < len(components)
        or not isinstance(components[index], Mapping)
        or components[index].get("bounds") != bounds
    ):
        raise FatalAccounting(
            f"act {act_id}'s hold declares a residual the conservation record it references "
            "does not carry at that ordinal; an extra row must trace to the reconciliation "
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
        designator_padding_config_path=args.designator_padding_config,
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
        sealed_config_digests=bindings["sealed_config_digests"],
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
