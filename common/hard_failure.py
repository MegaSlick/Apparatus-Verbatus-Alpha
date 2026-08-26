"""The one checked reader for the run-level hard-failure cap, and its tally.

Distinct from `common/recovery.py`: that module bounds how often ONE ACT may ask
for bounded rework. This module answers a different question -- is this RUN
going wrong -- and the two are not the same mechanism wearing two names. Tyrel's
ruling of 2026-08-05 blesses both: the per-act recovery budget stays exactly as
built, and this cap sits beside it.

The tally is recomputed from the artifacts already on disk every time it is
asked for, never from a running counter or an event stream: a count kept only in
memory reads zero exactly when the process that was tallying it dies before
anything asks it a second time, which is precisely backwards for a mechanism
whose entire job is noticing that something died. Every stage's outcome
artifacts are already the sealed, self-hashed, append-only evidence this
pipeline keeps for every other accounting purpose; this reuses that evidence
rather than inventing a parallel ledger for it.
"""

import tomllib
from pathlib import Path
from typing import Any, Final

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError, FatalAccounting
from common.contracts.outcomes import OutcomeClass, classify
from common.contracts.stages import PERLECTOR, STAGES

DEFAULT_HARD_FAILURE_CONFIG_PATH: Final = (
    Path(__file__).resolve().parents[1] / "config" / "hard_failure.toml"
)

# Tyrel's ruled boundary, 2026-08-05: two continues as an early warning and the
# third stops. Unlike the recovery budget, the hard-failure threshold was not
# delegated for downward tuning, so configuration cannot move it either way.
RULED_THRESHOLD: Final = 2
# A policy is a small operator declaration, not a corpus payload. Bound both the
# bytes parsed and the entries that each drive a pass over stage evidence, so a
# caller-selected file cannot turn one checkpoint into unbounded memory or
# policy-length-times-corpus work. The shipped policy is under 8 KiB and six
# entries; these ceilings leave ample configuration headroom without making the
# boundary nominal.
MAX_HARD_FAILURE_CONFIG_BYTES: Final = 1 << 20
MAX_HARD_FAILURE_KINDS: Final = 128
PERLECTOR_INSTRUMENT_KINDS: Final = frozenset(
    {"lectio-nuda", "lectio-prior", "primed-without-prior"}
)


def _reason_code(text: Any) -> str | None:
    """The closed-set code prefix of a `"{code}: {detail}"` refusal reason.

    Duplicated in miniature rather than importing `pipeline/1_exemplar/
    admission.reason_code`: `common/` may not import `pipeline/` (the import-
    boundary test in `common/chairs/` enforces that), and this module does not
    need the full `RefusalReason` enum -- only the same colon-split convention
    every reason-coded payload in this pipeline already uses. A payload with no
    reason, or a reason that is not a string, has no code rather than an error:
    absence of a reason is common (most FAILED outcomes carry none at all) and
    is not itself a defect this function exists to catch.
    """
    if not isinstance(text, str) or ":" not in text:
        return None
    return text.split(":", 1)[0]


def load_hard_failure_policy(path: str | Path = DEFAULT_HARD_FAILURE_CONFIG_PATH) -> dict[str, Any]:
    """Read one policy, validate its closed kind list, and return its resolved record.

    Every configured `(stage, outcome)` pair must already be a real member of
    that stage's closed outcome vocabulary, classified FAILED. This is what
    keeps the list "configuration, not literals" honest in the other direction
    too: a typo'd stage or outcome name is refused here rather than silently
    never matching anything, and a pair that classifies UNRESOLVED or COMPLETED
    (an ordinary hold, an ordinary acceptance) can never be configured into a
    mechanism that is supposed to name systemic breakage.

    A `[[kind]]` entry may additionally carry `reason`, narrowing it to only
    the artifacts of that (stage, outcome) whose payload's `reason` field opens
    with that exact code (`common/hard_failure._reason_code`). This is what
    lets the policy count `(door, refused)` only for `corrupt`/`unreadable` --
    the old pipeline's own "corrupt or unrenderable image" -- without counting
    every door refusal, most of which are routine bulk-corpus noise (an
    unsupported format, an oversized file) rather than evidence the run itself
    is going wrong. Reason-scoped entries are tracked separately from bare
    (stage, outcome) ones precisely so a bare entry is never accidentally
    widened by a reason-scoped sibling, or vice versa.
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
            # `classify` raises `FatalAccounting` for an outcome outside its
            # stage's closed vocabulary at all -- deliberately not catchable
            # as an ordinary refusal, because during a live run "in no
            # terminal set" is invariant #10's fatal imbalance. This is
            # config validation before any run exists: a misspelled outcome
            # here is a typo in a file, exactly like this loader's other
            # `[[kind]]` refusals a few lines either side of it, and should
            # surface the same way they do.
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

    # Sorted tuples rather than frozensets: this resolved record is sealed into
    # `run.json`'s config digest exactly as the recovery policy is, and a run
    # binding has to be canonically serializable. Sorting is what makes it a
    # deterministic binding rather than a set whose iteration order is incidental.
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

    Counted as `(stage, subject_id)` pairs, not as raw artifact counts: an act
    that failed and was later recovered still contributes one hard-failure
    incident (the event happened; recovering the coverage does not erase that
    it happened), while a stage retrying the identical failing outcome twice
    for the same subject is one incident, not two. The manifest a stage's own
    `build_manifest` derives is verified evidence -- every entry comes from an
    artifact that passed its envelope, run-binding, and path checks on the way
    into that manifest -- so reading its `outcome` and `subject_id` fields
    directly does not trust unchecked tally data. Callers that own a whole-run
    boundary also verify each artifact's input bytes. A directly invoked stage
    disables that recursive check: its own consumer boundary must diagnose
    stale lineage, while the cap remains responsible for whether its tally
    records can be read.
    """
    # One manifest per stage, however many kinds the policy names on it. Building
    # a manifest revalidates every artifact of that stage and re-verifies every
    # byte it references, so walking it once per configured kind would make the
    # cost of this tally the policy's LENGTH times the corpus size — and the
    # orchestrator recomputes it at every stage boundary and every recovery
    # round. Two entries already share the door today. Caching also gives every
    # kind in one tally the same snapshot of the tree.
    manifests: dict[str, list[dict[str, Any]]] = {}
    reasons_seen: dict[tuple[str, str, str], str | None] = {}

    def artifacts(stage: str) -> list[dict[str, Any]]:
        if stage not in manifests:
            manifests[stage] = tree.build_manifest(stage, verify_inputs=verify_inputs)["artifacts"]
        return manifests[stage]

    def reason_of(stage: str, entry: dict[str, Any]) -> str | None:
        # A reason-scoped policy can name several reasons on the same
        # (stage, outcome) pair (`door:refused:corrupt` and `:unreadable`
        # today), and each pass over `artifacts(stage)` would otherwise
        # re-read every one of that stage's artifacts from disk once per
        # reason. Cached per artifact instead, so each is read at most once
        # regardless of how many reason_kinds entries name its stage.
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

        Stated once for both loops below: the two entry shapes differ only in
        how they select candidates, and a partition rule written twice is a
        partition rule that can come to mean two things. A subject with both a
        production and an instrument failure appears in both lists, which is
        the honest answer -- the instrument arm neither excuses nor doubles the
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
