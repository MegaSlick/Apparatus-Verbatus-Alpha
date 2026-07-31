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
from typing import Any

from common.contracts.canonical import digest_of
from common.contracts.envelope import build_envelope
from common.contracts.errors import ContractError
from common.contracts.identities import artifact_id
from common.runtree.store import PublishResult, RunTree

# Exit codes carry cause, per harvest invariant #11. The old contract worth
# keeping: 0 = complete, 2 = structural or fatal, 3 = accounted but holdable.
# A stage that failed structurally and exited 0 is how a run reports success over
# work it never did.
EXIT_COMPLETE = 0
EXIT_FATAL = 2
EXIT_HELD = 3


class StageContext:
    """One stage's view of the run it is part of."""

    __slots__ = ("tree", "run", "fixture", "scenario", "stage", "adapter_revision", "args")

    def __init__(self, tree, run, fixture, scenario, stage, adapter_revision, args):
        self.tree = tree
        self.run = run
        self.fixture = fixture
        self.scenario = scenario
        self.stage = stage
        self.adapter_revision = adapter_revision
        self.args = args

    @property
    def config_digest(self) -> str:
        return self.run["config_digest"]

    @property
    def witness_seats(self) -> list[str]:
        return list(self.run["witness_seats"])

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
    parser.add_argument("--scenario", default="happy", choices=("happy", "review"))
    parser.add_argument("--fixture-root", default="proof")
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


def fixture_config_digest(fixture: dict[str, Any], scenario: str) -> str:
    """The canonical digest of everything that shapes this run's behaviour."""
    return digest_of({"fixture": fixture, "scenario": scenario})


def open_context(args, stage: str, adapter_revision: str) -> StageContext:
    """Open an existing run for a stage that is not the first to write."""
    fixture = load_fixture(args.fixture_root)
    tree = RunTree(Path(args.run_root), args.run_id)
    return StageContext(
        tree=tree,
        run=tree.read_run(),
        fixture=fixture,
        scenario=args.scenario,
        stage=stage,
        adapter_revision=adapter_revision,
        args=args,
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
