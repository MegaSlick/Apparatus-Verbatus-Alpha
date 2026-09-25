"""The one checked reader for the run-level hard-failure cap, and its tally.

Distinct from `common/recovery.py`, which bounds rework for one act: this
answers whether the RUN itself is going wrong. The tally is recomputed from
the sealed, self-hashed artifacts already on disk every time it is asked for
rather than kept as a running counter, so a process dying mid-run cannot make
it read zero. A shard is one capped 1,000-page run, so the tally is per shard;
a cross-shard condition is held for Recensor review instead.
"""

import tomllib
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import DOOR, PERLECTOR, STAGES

DEFAULT_HARD_FAILURE_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "hard_failure.toml"
)

# Two continues as an early warning, the third stops; not tunable by config.
# The project lead's own ruling (quoted in full in config/hard_failure.toml),
# not a value this session may change.
RULED_THRESHOLD: Final = 2
# A policy is bounded operator declaration, not a corpus payload: a
# caller-selected file must not turn one checkpoint into unbounded memory or
# policy-length-times-corpus work.
MAX_HARD_FAILURE_CONFIG_BYTES: Final = 1 << 20
MAX_HARD_FAILURE_KINDS: Final = 128
PERLECTOR_INSTRUMENT_KINDS: Final = frozenset(
    {"lectio-nuda", "lectio-prior", "primed-without-prior"}
)
# Duplicated in miniature rather than importing `pipeline/1_exemplar/
# admission.RefusalReason`, since `common/` may not import `pipeline/`. Checked
# so a mistyped door reason is refused at config load, not matched against
# nothing forever with no error to say the cap had gone quiet.
DOOR_REFUSAL_REASONS: Final = frozenset(
    {
        "empty",
        "unreadable",
        "too-large",
        "unrecognized-format",
        "corrupt",
        "unsupported-variant",
        "digest-mismatch",
    }
)


def _reason_code(text: Any) -> str | None:
    """The code prefix of a `"{code}: {detail}"` refusal reason, or None.

    A payload with no reason, or a non-string reason, has no code rather than
    an error: most FAILED outcomes carry no reason, and that absence is not
    itself a defect this function exists to catch.
    """
    if not isinstance(text, str) or ":" not in text:
        return None
    return text.split(":", 1)[0]


def load_hard_failure_policy(path: str | Path = DEFAULT_HARD_FAILURE_CONFIG_PATH) -> dict[str, Any]:
    """Read one policy, validate its closed kind list, and return its resolved record.

    Every configured `(stage, outcome)` pair must classify FAILED for that
    stage, so a typo'd name is refused at load and an ordinary hold or
    acceptance can never be configured into a mechanism meant to name systemic
    breakage. A `[[kind]]` entry may additionally carry `reason`, narrowing it
    to artifacts whose payload `reason` opens with that exact code -- letting
    the policy count `(door, refused)` only for `corrupt`/`unreadable` rather
    than routine bulk-corpus noise. Reason-scoped entries are tracked
    separately from bare ones so neither widens the other, and a door-scoped
    `reason` is checked against `DOOR_REFUSAL_REASONS` so a typo is refused
    loudly rather than silently matching nothing.
    """
    path = Path(path)
    try:
        with path.open("rb") as policy_file:
            data = policy_file.read(MAX_HARD_FAILURE_CONFIG_BYTES + 1)
    except OSError as error:
        raise ContractError(
            f"the hard-failure configuration at {path} could not be read as a policy: {error}"
        ) from error
    if len(data) > MAX_HARD_FAILURE_CONFIG_BYTES:
        raise ContractError(
            f"the hard-failure configuration exceeds {MAX_HARD_FAILURE_CONFIG_BYTES} bytes; "
            "a run policy is bounded metadata, not a corpus payload"
        )
    try:
        config = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(
            f"the hard-failure configuration at {path} could not be read as a policy: {error}"
        ) from error
    if not isinstance(config, dict):
        raise ContractError("the hard-failure configuration is not a table")

    threshold = config.get("threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        raise ContractError("the hard-failure configuration has no non-negative integer threshold")
    if threshold != RULED_THRESHOLD:
        raise ContractError(
            f"the hard-failure configuration names threshold {threshold}, but the ruled value "
            f"is exactly {RULED_THRESHOLD}: two is an early warning and the third stops"
        )

    raw_kinds = config.get("kind")
    if not isinstance(raw_kinds, list) or not raw_kinds:
        raise ContractError("the hard-failure configuration names no [[kind]] entries")
    if len(raw_kinds) > MAX_HARD_FAILURE_KINDS:
        raise ContractError(
            f"the hard-failure configuration names {len(raw_kinds)} [[kind]] entries, above "
            f"the bounded maximum of {MAX_HARD_FAILURE_KINDS}"
        )

    kinds: set[tuple[str, str]] = set()
    reason_kinds: set[tuple[str, str, str]] = set()
    seen: set[tuple[str, str, str | None]] = set()
    for entry in raw_kinds:
        fields = set(entry) if isinstance(entry, dict) else set()
        if fields not in ({"stage", "outcome"}, {"stage", "outcome", "reason"}):
            raise ContractError(
                "a hard-failure [[kind]] entry must carry stage and outcome, and may "
                "additionally carry reason"
            )
        stage, outcome = entry["stage"], entry["outcome"]
        if stage not in STAGES:
            raise ContractError(f"a hard-failure [[kind]] names unknown stage {stage!r}")
        try:
            failed = classify(stage, outcome) is OutcomeClass.FAILED
        except FatalAccounting as error:
            # No run exists yet, so an outcome outside the closed vocabulary
            # here is a config typo, not the live-run fatal imbalance
            # FatalAccounting means elsewhere -- surface it like this
            # loader's other `[[kind]]` refusals.
            raise ContractError(str(error)) from error
        if not failed:
            raise ContractError(
                f"a hard-failure [[kind]] names ({stage!r}, {outcome!r}), which does not "
                "classify FAILED; this cap counts failures, not ordinary holds or acceptances"
            )
        reason = entry.get("reason")
        if "reason" in fields and (not isinstance(reason, str) or not reason):
            raise ContractError(
                f"a hard-failure [[kind]] names ({stage!r}, {outcome!r}) with a reason that "
                "is not a non-empty string"
            )
        if "reason" in fields and stage == DOOR and reason not in DOOR_REFUSAL_REASONS:
            raise ContractError(
                f"a hard-failure [[kind]] names ({stage!r}, {outcome!r}) with reason "
                f"{reason!r}, which is not one of the Door's closed refusal reasons "
                f"{sorted(DOOR_REFUSAL_REASONS)}"
            )
        identity = (stage, outcome, reason if "reason" in fields else None)
        if identity in seen:
            raise ContractError(
                f"the hard-failure configuration names ({stage!r}, {outcome!r}"
                f"{f', reason={reason!r}' if reason else ''}) more than once"
            )
        seen.add(identity)
        if "reason" in fields:
            reason_kinds.add((stage, outcome, reason))
        else:
            kinds.add((stage, outcome))

    # Sorted, not a frozenset: this is sealed into run.json's config digest and
    # must be a deterministic binding, not one with incidental iteration order.
    return {
        "config_sha256": digest_bytes(data),
        "threshold": threshold,
        "kinds": sorted(kinds),
        "reason_kinds": sorted(reason_kinds),
    }


def tally_hard_failures(
    tree, policy: dict[str, Any], *, verify_inputs: bool = True
) -> dict[str, Any]:
    """Recompute the run's hard-failure tally from the sealed partition on disk.

    Counted as `(stage, subject_id)` pairs, not raw artifact counts: a failed
    act that was later recovered still contributes one incident, and a stage
    retrying the same failing outcome twice for one subject is one incident,
    not two. `build_manifest`'s entries are already verified evidence, so
    reading `outcome`/`subject_id` off them trusts nothing unchecked; a
    directly invoked stage skips the recursive input check, leaving its own
    consumer boundary responsible for stale lineage.
    """
    # One manifest per stage regardless of how many kinds name it: building a
    # manifest re-verifies every byte of that stage, so caching keeps the cost
    # from scaling with policy length, and gives every kind the same snapshot.
    manifests: dict[str, list[dict[str, Any]]] = {}
    reasons_seen: dict[tuple[str, str, str], str | None] = {}

    def artifacts(stage: str) -> list[dict[str, Any]]:
        if stage not in manifests:
            manifests[stage] = tree.build_manifest(stage, verify_inputs=verify_inputs)["artifacts"]
        return manifests[stage]

    def reason_of(stage: str, entry: dict[str, Any]) -> str | None:
        # Cached per artifact so several reason_kinds entries on one stage
        # (e.g. door:refused:corrupt and :unreadable) don't each re-read disk.
        key = (stage, entry["kind"], entry["artifact_id"])
        if key not in reasons_seen:
            record = tree.read_artifact(stage, entry["kind"], entry["artifact_id"])
            reasons_seen[key] = _reason_code(record["payload"].get("reason"))
        return reasons_seen[key]

    by_kind: dict[str, list[str]] = {}
    instrument_by_kind: dict[str, list[str]] = {}
    subjects: set[tuple[str, str]] = set()

    def record(key: str, stage: str, candidates: list[dict[str, Any]]) -> None:
        """Split one policy entry's matches into production and instrument arms.

        A subject with both a production and an instrument failure appears in
        both lists: the instrument arm neither excuses nor doubles the
        production incident.
        """
        production: set[str] = set()
        instrument: set[str] = set()
        for entry in candidates:
            is_instrument = stage == PERLECTOR and entry["kind"] in PERLECTOR_INSTRUMENT_KINDS
            (instrument if is_instrument else production).add(entry["subject_id"])
        by_kind[key] = sorted(production)
        if instrument:
            instrument_by_kind[key] = sorted(instrument)
        subjects.update((stage, subject_id) for subject_id in production)

    for stage, outcome in sorted(policy["kinds"]):
        record(
            f"{stage}:{outcome}",
            stage,
            [entry for entry in artifacts(stage) if entry["outcome"] == outcome],
        )

    for stage, outcome, reason in sorted(policy.get("reason_kinds") or ()):
        record(
            f"{stage}:{outcome}:{reason}",
            stage,
            [
                entry
                for entry in artifacts(stage)
                if entry["outcome"] == outcome and reason_of(stage, entry) == reason
            ],
        )

    count = len(subjects)
    return {
        "threshold": policy["threshold"],
        "count": count,
        "breached": count > policy["threshold"],
        "by_kind": by_kind,
        "instrument_by_kind": instrument_by_kind,
        "instrument_count": len(
            {subject for matches in instrument_by_kind.values() for subject in matches}
        ),
        "subjects": sorted(f"{stage}:{subject_id}" for stage, subject_id in subjects),
    }
