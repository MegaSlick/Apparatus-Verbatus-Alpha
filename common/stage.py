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
from typing import Any, Callable, Protocol

from common.contracts.canonical import digest_of
from common.contracts.envelope import build_envelope
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import artifact_id
from common.contracts.outcomes import classify
from common.contracts.stages import DESIGNATOR
from common.runtree.store import PublishResult, RunTree
from common.seats.models import AbsentSeat, ModelsConfig, SeatIdentity, ServingDetails
from common.seats.protocol import SeatProtocol
from common.seats.registry import SeatRegistry

# Exit codes carry cause, per harvest invariant #11. The old contract worth
# keeping: 0 = complete, 2 = structural or fatal, 3 = accounted but holdable.
# A stage that failed structurally and exited 0 is how a run reports success over
# work it never did.
EXIT_COMPLETE = 0
EXIT_FATAL = 2
EXIT_HELD = 3

# The witness outcomes that mean a seat actually served, and therefore that a
# serving receipt exists for the reading. Named once, here, because both halves
# of the handoff need it and they must not drift: the Attestatores decides
# whether to write a receipt, and the Perlector decides whether to demand one.
# A producer and a consumer disagreeing about this set would refuse a record
# that is in fact correct — `dead` and `not-run` are unresolved or unattempted,
# and inventing a serving moment for either would be a receipt for nothing.
ATTEMPTED_WITNESS_OUTCOMES = frozenset({"read", "genuinely-empty", "failed"})

# Every top-level field a reading's model provenance may carry. A closed set,
# because invariant #42 refuses *wrong-schema* provenance rather than a list of
# fields we already know are wrong: a denylist passes anything a later stage
# invents, and an unvalidated field inside a sealed reading is exactly what #42
# exists to stop. `absence` appears only on an absent seat and the identity
# fields only on a configured one; which combination is legal is decided in
# `validate_serving_provenance`, not here.
_PROVENANCE_FIELDS = frozenset(
    {
        "seat",
        "seat_state",
        "adapter_revision",
        "absence",
        "resolved_identity",
        "resolved_revision",
        "receipt_ref",
        "witness_regime",
    }
)

# ARCHITECTURE's Perlector paragraph: witness identity travels under a run-level
# named/blinded toggle, "every Perlectio recording its regime". The Perlector
# writes it; until this check, nothing read it back, so a Perlectio claiming an
# impossible regime — or a typo — travelled sealed. Binding it to an actual
# run-level toggle is Spec 08's work; refusing a value that cannot be true is
# this system's, because it is provenance and #42 governs provenance.
_WITNESS_REGIMES = frozenset({"named", "blinded"})


class StageSeatProtocol(SeatProtocol, Protocol):
    """The small additional config surface a calling stage needs.

    A stage receives this explicitly rather than knowing which registry
    implementation made it.  The production default is ``SeatRegistry``; the
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
    def witness_seats(self) -> list[str]:
        return list(self.run["witness_seats"])

    @property
    def witness_floor(self) -> int:
        return self.registry.config.witness_floor

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
            approval_ref=approval_ref,
        )
        return self.tree.publish_artifact(envelope)

    def write_serving_receipt(
        self, identity: SeatIdentity, serving: ServingDetails
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
    parser.add_argument("--operation", default="initial")
    parser.add_argument("--act", default=None, help="one act id, for a recovery operation")
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
    models: ModelsConfig, fixture: dict[str, Any], scenario: str
) -> dict[str, Any]:
    """The three `run.json` bindings, and everything that shapes them.

    Since spec 02 `config/models.toml` owns the roster, the witness floor and
    the adapter recipes, so two of the three come straight off it. The third,
    `config_digest`, is the digest of *everything* that shapes this run's
    behaviour — the model configuration, the fixture, and the scenario.

    All three parts are load-bearing, and the scenario is the one easiest to
    drop by accident. Spec 01's third acceptance test reuses one run id under a
    second scenario and requires the refusal *before any write*; the two
    scenarios declare identical source pages, so with the scenario out of this
    digest nothing in `run.json` distinguishes them and the run gets four
    stages in before artifact immutability catches it. A late incidental
    refusal is not the sealed-tree guarantee spec 01 landed.
    """
    return {
        "witness_seats": list(models.witness_seats),
        "config_digest": digest_of(
            {"fixture": fixture, "scenario": scenario, "models": models.to_record()}
        ),
        "adapter_recipes": dict(sorted(models.adapter_recipes.items())),
    }


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


def fixture_serving_details(identity: SeatIdentity) -> ServingDetails:
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
) -> SeatIdentity | None:
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
    if "witness_regime" in provenance and provenance["witness_regime"] not in _WITNESS_REGIMES:
        raise SchemaRefusal(
            f"model provenance records witness regime {provenance['witness_regime']!r}, "
            f"which is not one of {sorted(_WITNESS_REGIMES)}"
        )
    if provenance.get("adapter_revision") != adapter_recipe_for(context.run, producer_stage):
        raise SchemaRefusal(
            f"model provenance does not carry the sealed adapter recipe for {producer_stage!r}"
        )

    state = provenance.get("seat_state")
    seat = provenance.get("seat")
    if not isinstance(seat, str) or not seat:
        raise SchemaRefusal("model provenance has no seat name")
    if state == "absent":
        configured = context.registry.resolve(seat)
        if not isinstance(configured, AbsentSeat):
            raise SchemaRefusal(f"model provenance calls configured seat {seat!r} absent")
        if provenance.get("absence") != configured.to_record():
            raise SchemaRefusal(f"model provenance absence for {seat!r} differs from models config")
        if any(
            provenance.get(field) is not None
            for field in ("resolved_identity", "resolved_revision", "receipt_ref")
        ):
            raise SchemaRefusal(f"absent seat {seat!r} carries a model identity or receipt")
        if require_receipt:
            raise SchemaRefusal(f"absent seat {seat!r} cannot have produced a reading")
        return None

    if state != "configured":
        raise SchemaRefusal(f"model provenance has unknown seat state {state!r}")
    record = provenance.get("resolved_identity")
    if not isinstance(record, dict):
        raise SchemaRefusal(f"configured seat {seat!r} has no resolved identity")
    try:
        identity = SeatIdentity(**record)
    except TypeError as error:
        raise SchemaRefusal(
            f"resolved identity for {seat!r} has the wrong schema: {error}"
        ) from error
    if seat != identity.role or record != identity.to_record():
        raise SchemaRefusal(f"resolved identity for {seat!r} is malformed")
    configured = context.registry.resolve(identity.role)
    if not isinstance(configured, SeatIdentity) or configured != identity:
        raise SchemaRefusal(f"resolved identity for {seat!r} differs from the sealed models config")
    revision = provenance.get("resolved_revision")
    if revision != {
        "kind": identity.receipt_revision_kind,
        "value": identity.receipt_revision,
    }:
        raise SchemaRefusal(f"resolved revision for {seat!r} differs from its immutable identity")

    reference = provenance.get("receipt_ref")
    if not require_receipt:
        if reference is not None:
            raise SchemaRefusal(f"seat {seat!r} was not run but carries a serving receipt")
        return identity
    if not isinstance(reference, dict):
        raise SchemaRefusal(f"reading from seat {seat!r} has no serving receipt reference")
    receipt = context.tree.read_run_receipt(reference)
    expected = {
        "seat": identity.role,
        "source": identity.source,
        "resolved": identity.source_reference,
        "revision": identity.receipt_revision,
        "revision_kind": identity.receipt_revision_kind,
        "digest_manifest": identity.digest_manifest,
    }
    differing = [field for field, value in expected.items() if receipt.get(field) != value]
    if differing:
        raise SchemaRefusal(
            f"serving receipt for {seat!r} differs from the resolved identity at {differing}"
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
    acts = seal["payload"]["expected_acts"]
    for act in acts:
        classify(DESIGNATOR, act.get("outcome"))
    return acts


def open_context(
    args,
    stage: str,
    *,
    registry_factory: Callable[[str], StageSeatProtocol] = SeatRegistry.from_toml,
) -> StageContext:
    """Open an existing run for a stage that is not the first to write."""
    fixture = load_fixture(args.fixture_root)
    scenario_for(fixture, args.scenario)
    registry = registry_factory(args.models_config)
    bindings = run_config_bindings(registry.config, fixture, args.scenario)
    tree = RunTree(Path(args.run_root), args.run_id)
    run = tree.read_run()
    differing = [
        field
        for field in ("config_digest", "adapter_recipes", "witness_seats")
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


def latest_attempt(records: list[dict[str, Any]], what: str) -> dict[str, Any]:
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
    """
    from common.contracts.errors import FatalAccounting

    if not records:
        raise FatalAccounting(f"no {what} to derive a current outcome from")
    for record in records:
        if not isinstance(record.get("payload", {}).get("attempt_ordinal"), int):
            raise FatalAccounting(
                f"a {what} artifact carries no attempt ordinal, so which attempt is "
                "current cannot be derived. A guess here silently picks a stale record"
            )
    return max(records, key=lambda record: record["payload"]["attempt_ordinal"])


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
