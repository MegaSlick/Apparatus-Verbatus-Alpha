"""The offline ``verbatus triage`` declaration and review queue.

This is deliberately a sibling of the ingest surface.  It reads the producer's
immutable documents, renders only paths to the already-made review proxies, and
has two narrow mutable documents: an append-only queue journal and the existing
Unit 6B confirmation-file shape.  It does not read masters or make a finding
from the instrument's verdict.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of, is_sha256
from common.contracts.errors import SchemaRefusal
from common.corpus_register import _refuse_preference
from operations.pod.durable import sync_directory
from operations.triage import instrument
from operations.triage.producer import (
    CONFIRMATION_SCHEMA,
    ProducerRefusal,
    routes_to_review,
    triage_manifest,
)

QUEUE_SCHEMA = "triage-review-queue.v1"
STATE_SCHEMA = "triage-review-state.v1"
MODE_SCHEMA = "triage-mode-declaration.v1"
_MODES = frozenset({"manual", "semi", "auto"})


class TriageRefusal(ProducerRefusal):
    """A named, operator-visible refusal for this console family."""


def _refuse_preference_named(value: Any, what: str) -> None:
    """Hard rule 8's refusal, in this console's own operator-facing vocabulary.

    `_refuse_preference` raises `SchemaRefusal`, which the CLI boundary does not
    recognise, so the one refusal that says a picker was being built reached the
    operator as the last-resort "unexpected" copy. It is the most consequential
    thing this console can refuse; it gets a name like every other refusal.
    """
    try:
        _refuse_preference(value)
    except SchemaRefusal as error:
        raise TriageRefusal(f"triage refusal {what}-expresses-preference: {error}") from error


def _read_canonical(path: str | Path, what: str) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TriageRefusal(f"triage refusal {what}-unreadable: could not read {what}") from error
    try:
        fixed = canonical_bytes(value)
    except (TypeError, ValueError) as error:
        raise TriageRefusal(
            f"triage refusal {what}-not-canonical: {what} must be canonical JSON"
        ) from error
    if not isinstance(value, dict) or fixed != raw:
        raise TriageRefusal(f"triage refusal {what}-not-canonical: {what} must be canonical JSON")
    return value


def _persisted_form(value: Mapping[str, Any], what: str) -> tuple[bytes, dict[str, Any]]:
    """The exact bytes a write would publish, and the structure read back from them.

    Every durable write in this module validates *this* structure, never the
    caller's in-memory object. The two can differ, and the difference is a
    smuggling channel: `canonical_bytes` serializes a tuple as a JSON array
    while `_refuse_preference` walks only dicts and lists, so a forbidden
    preference field wrapped in a tuple satisfied the exact-object check and
    still reached disk as an ordinary array member. Re-reading the bytes closes
    that divergence and every other one of its shape at once, instead of
    teaching each validator one more container type it must know about.
    """
    try:
        data = canonical_bytes(value)
        persisted = json.loads(data.decode("utf-8"))
        fixed = canonical_bytes(persisted)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TriageRefusal(
            f"triage refusal {what}-not-canonical: {what} has no canonical JSON form"
        ) from error
    if not isinstance(persisted, dict) or fixed != data:
        # A dict subclass can supply duplicate keys from ``items()``. The JSON
        # encoder emits them, the parser collapses them, and validation would see
        # only the collapsed object unless the byte form is required to be a fixed
        # point. The producer's canonical loader makes the same comparison.
        raise TriageRefusal(
            f"triage refusal {what}-not-canonical: {what}'s serialized form is not a "
            "canonical JSON fixed point"
        )
    return data, persisted


def _atomic_bytes(path: Path, data: bytes) -> None:
    """Publish exactly these bytes, and make the name that points at them durable.

    The parent-directory fsync is the half that was missing. Without it a power
    cut can lose the rename after this function has returned success, so a
    decision the console told the operator was recorded is silently absent on
    resume — the failure GOVERNANCE 2 forbids. This is the same contract the
    6B producer's `_atomic_write_canonical` and this package's own
    `records._atomic_replace` already keep, including the distinction between
    "was not published" and "was published but is not proven durable": the
    second must not be reported as a failed write, because the bytes are there.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        published = True
        sync_directory(path.parent, strict=True)
    except OSError as error:
        state = "was published but is not proven durable" if published else "was not published"
        raise TriageRefusal(f"triage refusal write-failed: {path} {state}") from error
    finally:
        # Not only on the error path: an interrupt between mkstemp and replace
        # would otherwise leave a hidden temporary behind in the operator's
        # durable state directory on every Ctrl+C.
        temporary.unlink(missing_ok=True)


@contextmanager
def _write_lock(path: Path, what: str) -> Iterator[None]:
    """Serialize one pathname's whole check-and-publish transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        handle = lock_path.open("a+b")
    except OSError as error:
        raise TriageRefusal(
            f"triage refusal {what}-lock-failed: write lock could not be opened"
        ) from error
    try:
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - the operator console is POSIX
            raise TriageRefusal(
                f"triage refusal {what}-lock-failed: POSIX write locking is unavailable"
            ) from error
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise TriageRefusal(
                f"triage refusal {what}-lock-failed: write lock could not be taken"
            ) from error
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor releases the POSIX lock. An explicit
                # unlock failure must not replace a more useful body refusal.
                pass
    finally:
        handle.close()


def _check_mode_declaration(value: Mapping[str, Any]) -> None:
    """Refuse anything but the closed declaration record, on the persisted form."""
    if (
        set(value) != {"schema", "batch_id", "mode", "operator"}
        or value.get("schema") != MODE_SCHEMA
        or not all(isinstance(value.get(field), str) for field in ("batch_id", "mode", "operator"))
    ):
        raise TriageRefusal(
            "triage refusal mode-declaration-invalid: declaration has the wrong closed schema"
        )
    declare_mode(value["mode"], batch_id=value["batch_id"], operator=value["operator"])
    _refuse_preference_named(value, "mode-declaration")


def declare_mode(mode: str, *, batch_id: str, operator: str) -> dict[str, Any]:
    """Return the durable declaration of a batch invocation word, not a config edit."""
    if mode not in _MODES:
        raise TriageRefusal("triage refusal mode-not-declared: mode must be manual, semi, or auto")
    if (
        not isinstance(batch_id, str)
        or not batch_id.strip()
        or not isinstance(operator, str)
        or not operator.strip()
    ):
        raise TriageRefusal(
            "triage refusal declaration-incomplete: batch and operator must be non-blank"
        )
    record = {"schema": MODE_SCHEMA, "batch_id": batch_id, "mode": mode, "operator": operator}
    _refuse_preference_named(record, "mode-declaration")
    return record


def write_mode_declaration(path: str | Path, declaration: Mapping[str, Any]) -> None:
    """Persist the selected invocation word as its own closed record.

    Validates the exact bytes it writes, not a stand-in for them: an earlier
    version passed ``str(declaration["batch_id"])`` etc. to `declare_mode` for
    validation and then wrote the original, untouched `declaration` mapping — so
    a non-string `batch_id` or `operator` carrying a nested forbidden field passed
    validation (its `str()` form is an ordinary string) while the smuggled field
    still reached disk in the written record.
    """
    data, persisted = _persisted_form(declaration, "mode-declaration")
    _check_mode_declaration(persisted)
    target = Path(path)
    with _write_lock(target, "mode-declaration"):
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError as error:
                raise TriageRefusal(
                    "triage refusal mode-declaration-target-unreadable: existing declaration "
                    "could not be checked"
                ) from error
            if existing == data:
                return
            raise TriageRefusal(
                "triage refusal mode-declaration-target-exists: a batch's selected mode is not rewritten"
            )
        _atomic_bytes(target, data)


def build_queue(
    manifest: Mapping[str, Any],
    evidence_records: Sequence[Mapping[str, Any]],
    *,
    proxy_paths: Mapping[str, str],
    mode_declaration: Mapping[str, Any],
    triage_modes_path: str | Path,
) -> dict[str, Any]:
    """Make the complete review set without ordering it by merit.

    Every producer row routed to review is retained, and every complementary
    candidate is retained even if its rows have already appeared.  The queue's
    order is digest order solely for stable stop/resume addressing, never a score.
    """
    try:
        triage_manifest.validate_manifest(dict(manifest))
    except SchemaRefusal as error:
        raise TriageRefusal(f"triage refusal manifest-invalid: {error}") from error
    _mode_bytes, persisted_mode = _persisted_form(mode_declaration, "mode-declaration")
    _check_mode_declaration(persisted_mode)
    mode = persisted_mode["mode"]
    rows = {row["source_frame_sha256"]: row for row in manifest["records"]}
    if any(row["mode"] != mode for row in rows.values()):
        raise TriageRefusal("triage refusal mode-mismatch: declaration and producer rows disagree")
    checked_evidence: list[dict[str, Any]] = []
    config = instrument.load_config()
    known_blindness = instrument.producer_recipe(config)["comparison_recipe"]["known_blindness"]
    for record in evidence_records:
        try:
            checked = instrument.validate_candidate_evidence(dict(record), config)
        except instrument.InstrumentRefusal as error:
            raise TriageRefusal(f"triage refusal evidence-invalid: {error}") from error
        if not set(checked["both_digests"]) <= set(rows):
            raise TriageRefusal(
                "triage refusal evidence-outside-manifest: evidence names another batch"
            )
        checked_evidence.append(checked)
    evidence_by_frame: dict[str, list[dict[str, Any]]] = {digest: [] for digest in rows}
    candidates: list[dict[str, Any]] = []
    for record in checked_evidence:
        for digest in record["both_digests"]:
            evidence_by_frame[digest].append(record)
        if record["verdict"] == "complementary-candidate":
            candidates.append(record)
    items: list[dict[str, Any]] = []
    for digest, row in rows.items():
        try:
            routed = routes_to_review(row, triage_modes_path)
        except ProducerRefusal as error:
            raise TriageRefusal(f"triage refusal review-routing-invalid: {error}") from error
        if routed:
            if (
                digest not in proxy_paths
                or not isinstance(proxy_paths[digest], str)
                or not proxy_paths[digest]
            ):
                raise TriageRefusal(
                    "triage refusal proxy-path-missing: every review row needs a proxy file path"
                )
            items.append(
                {
                    "kind": "review-row",
                    "source_frame_sha256": digest,
                    "manifest_row_sha256": row["manifest_row_sha256"],
                    "proxy_path": proxy_paths[digest],
                    "evidence": sorted(evidence_by_frame[digest], key=digest_of),
                    "known_blindness": known_blindness,
                }
            )
    for record in candidates:
        key = digest_of(record)
        if any(digest not in proxy_paths for digest in record["both_digests"]):
            raise TriageRefusal(
                "triage refusal proxy-path-missing: every candidate needs proxy file paths"
            )
        items.append(
            {
                "kind": "cluster-candidate",
                "corpus_id": manifest["corpus_id"],
                "evidence_sha256": key,
                "both_digests": record["both_digests"],
                "proxy_paths": [proxy_paths[digest] for digest in record["both_digests"]],
                "evidence": record,
                "known_blindness": known_blindness,
            }
        )
    # Digest order provides a reproducible address only.
    items.sort(key=digest_of)
    queue = {
        "schema": QUEUE_SCHEMA,
        "mode_declaration": persisted_mode,
        "manifest_sha256": digest_of(manifest),
        "items": items,
    }
    _refuse_preference_named(queue, "queue")
    return queue


_QUEUE_FIELDS = frozenset({"schema", "mode_declaration", "manifest_sha256", "items"})


def _checked_queue(queue: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """The fixed-point queue form whose digest the journal actually persists."""
    data, persisted = _persisted_form(queue, "queue")
    # Name the consequential rule before the ordinary closed-schema refusal: a
    # preference field is not merely an extra key, it is an attempted picker.
    _refuse_preference_named(persisted, "queue")
    if (
        set(persisted) != _QUEUE_FIELDS
        or persisted.get("schema") != QUEUE_SCHEMA
        or not is_sha256(persisted.get("manifest_sha256"))
        or not isinstance(persisted.get("items"), list)
        or not all(isinstance(item, dict) for item in persisted["items"])
        or not isinstance(persisted.get("mode_declaration"), dict)
    ):
        raise TriageRefusal("triage refusal queue-invalid: queue is not a closed review queue")
    _check_mode_declaration(persisted["mode_declaration"])
    return data, persisted


def load_queue(
    manifest_path: str | Path,
    evidence_path: str | Path,
    proxies_path: str | Path,
    *,
    mode: str,
    batch_id: str,
    operator: str,
    triage_modes_path: str | Path,
) -> dict[str, Any]:
    manifest = _read_canonical(manifest_path, "manifest")
    evidence_document = _read_canonical(evidence_path, "evidence")
    proxies = _read_canonical(proxies_path, "proxy-paths")
    records = evidence_document.get("records")
    if (
        set(evidence_document) != {"records"}
        or not isinstance(records, list)
        or not all(isinstance(row, dict) for row in records)
    ):
        raise TriageRefusal("triage refusal evidence-invalid: evidence document must be {records}")
    if not all(is_sha256(key) and isinstance(value, str) for key, value in proxies.items()):
        raise TriageRefusal(
            "triage refusal proxy-paths-invalid: proxy paths must map source digests to paths"
        )
    return build_queue(
        manifest,
        records,
        proxy_paths=proxies,
        mode_declaration=declare_mode(mode, batch_id=batch_id, operator=operator),
        triage_modes_path=triage_modes_path,
    )


_DECISION_ENTRY_FIELDS = frozenset({"item_sha256", "decision", "draft_sha256"})


def _check_queue_state(value: Mapping[str, Any]) -> None:
    """Refuse a journal that is not wholly made of closed decision records.

    Every append rewrites the whole document, so the prior entries are bytes this
    write is itself publishing. Checking only the container let a hand-edited or
    truncated-and-repaired journal carry a record with a non-digest `item_sha256`,
    an unknown `decision` word, or extra keys straight through the next append —
    validated container, unvalidated contents. A non-dict entry reached the
    duplicate scan and raised a bare `AttributeError`, which left the operator
    with the boundary's last-resort "unexpected" copy instead of a named refusal.
    """
    if (
        set(value) != {"schema", "queue_sha256", "decisions"}
        or value.get("schema") != STATE_SCHEMA
        or not is_sha256(value.get("queue_sha256"))
        or not isinstance(value.get("decisions"), list)
    ):
        raise TriageRefusal(
            "triage refusal queue-state-invalid: state is not this queue's append-only journal"
        )
    for entry in value["decisions"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != _DECISION_ENTRY_FIELDS
            or not is_sha256(entry["item_sha256"])
            or not isinstance(entry["decision"], str)
            or entry["decision"] not in {"accept", "decline"}
            or (entry["draft_sha256"] is not None and not is_sha256(entry["draft_sha256"]))
            # An acceptance is the only decision that produces a confirmation, so
            # it is the only one that carries its digest. The two must agree or the
            # journal cannot say which acceptances still owe a confirmation file.
            or (entry["decision"] == "accept") != (entry["draft_sha256"] is not None)
        ):
            raise TriageRefusal(
                "triage refusal queue-state-entry-invalid: the journal holds a record that is "
                "not a closed accept/decline decision"
            )
    _refuse_preference_named(value, "queue-state")


def _load_journal(state_path: str | Path, queue: Mapping[str, Any]) -> dict[str, Any] | None:
    """This queue's validated journal, or None when no decision has been recorded yet.

    A journal that cannot be read, is not canonical, is not wholly closed decision
    records, or belongs to a different queue is refused by name rather than treated
    as absent. Resume after an interrupted or edited write therefore stops loudly;
    it never silently starts a fresh journal over the top of a damaged one.
    """
    path = Path(state_path)
    if not path.exists():
        return None
    queue_bytes, persisted_queue = _checked_queue(queue)
    state = _read_canonical(path, "queue-state")
    _check_queue_state(state)
    if state["queue_sha256"] != digest_bytes(queue_bytes):
        # Named apart from a shape refusal: a well-formed journal for a different
        # queue is an operator pointing at the wrong file, and saying so beats
        # telling them their journal is malformed when it is not.
        raise TriageRefusal(
            "triage refusal queue-state-other-queue: this journal records decisions "
            "for a different review queue"
        )
    candidate_digests = {
        digest_of(item)
        for item in persisted_queue["items"]
        if isinstance(item, dict) and item.get("kind") == "cluster-candidate"
    }
    seen: set[str] = set()
    for entry in state["decisions"]:
        item_digest = entry["item_sha256"]
        if item_digest not in candidate_digests or item_digest in seen:
            raise TriageRefusal(
                "triage refusal queue-state-history-invalid: journal history must name each "
                "candidate from this queue at most once"
            )
        seen.add(item_digest)
    return state


def recorded_decision(
    state_path: str | Path, queue: Mapping[str, Any], *, item_digest: str
) -> dict[str, Any] | None:
    """The decision already journalled for this queue item, or None."""
    state = _load_journal(state_path, queue)
    if state is None:
        return None
    return next(
        (entry for entry in state["decisions"] if entry["item_sha256"] == item_digest), None
    )


def append_decision(
    state_path: str | Path,
    queue: Mapping[str, Any],
    *,
    item_digest: str,
    decision: str,
    draft: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one irreversible visible decision; earlier decisions are never rewritten."""
    queue_bytes, persisted_queue = _checked_queue(queue)
    if decision not in {"accept", "decline"}:
        raise TriageRefusal("triage refusal decision-invalid: decision must be accept or decline")
    items = {digest_of(item): item for item in persisted_queue["items"]}
    if item_digest not in items:
        raise TriageRefusal("triage refusal queue-item-missing: decision does not name this queue")
    if items[item_digest].get("kind") != "cluster-candidate":
        raise TriageRefusal(
            "triage refusal decision-not-candidate: only cluster candidates can be accepted or declined"
        )
    if decision == "accept" and draft is None:
        raise TriageRefusal(
            "triage refusal acceptance-needs-draft: acceptance needs the shown confirmation draft"
        )
    if decision == "decline" and draft is not None:
        raise TriageRefusal(
            "triage refusal decline-carries-draft: a decline records no confirmation draft"
        )
    draft_digest: str | None = None
    if draft is not None:
        # Bound against the same persisted form the confirmation write publishes,
        # so the digest this journal records addresses those exact bytes.
        draft_bytes, persisted_draft = _persisted_form(draft, "confirmation-draft")
        _check_confirmation(persisted_draft)
        cluster = persisted_draft["clusters"][0] if len(persisted_draft["clusters"]) == 1 else None
        page_members = (
            {member for page in cluster["pages"] for member in page["member_frame_sha256"]}
            if cluster is not None
            else set()
        )
        if (
            persisted_draft["corpus_id"] != items[item_digest].get("corpus_id")
            or persisted_draft["instrument_config_sha256"]
            != items[item_digest]["evidence"]["instrument_config_sha256"]
            or cluster is None
            or cluster["evidence_pairs"] != [items[item_digest]["both_digests"]]
            or page_members != set(items[item_digest]["both_digests"])
        ):
            raise TriageRefusal(
                "triage refusal draft-does-not-match-candidate: draft is not this candidate's 6B confirmation"
            )
        draft_digest = digest_bytes(draft_bytes)
    path = Path(state_path)
    with _write_lock(path, "queue-state"):
        state = _load_journal(path, persisted_queue)
        prior: list[dict[str, Any]] = state["decisions"] if state is not None else []
        if any(entry["item_sha256"] == item_digest for entry in prior):
            raise TriageRefusal(
                "triage refusal decision-already-recorded: decided rows are never rewritten"
            )
        entry = {"item_sha256": item_digest, "decision": decision, "draft_sha256": draft_digest}
        data, persisted = _persisted_form(
            {
                "schema": STATE_SCHEMA,
                "queue_sha256": digest_bytes(queue_bytes),
                "decisions": [*prior, entry],
            },
            "queue-state",
        )
        _check_queue_state(persisted)
        _atomic_bytes(path, data)
        return persisted


def accept_candidate(
    state_path: str | Path,
    queue: Mapping[str, Any],
    *,
    item_digest: str,
    draft: Mapping[str, Any],
    confirmation_path: str | Path,
    preview_sha256: str,
) -> dict[str, Any]:
    """Journal the acceptance, then publish its confirmation — and finish either half.

    An acceptance is two durable writes to two files and cannot be made one. The
    journal is written first so a draft that fails the candidate binding never
    reaches the confirmation file, but that ordering leaves a real interruption
    window: a crash between the two used to end with `accept` permanently
    journalled, no confirmation file on disk, and every retry refused as
    `decision-already-recorded` — an acceptance that could never be completed and
    a message describing the wrong problem (hard rule 7).

    A retry that replays the *same* acceptance of the *same* item with the *same*
    draft bytes is that interrupted write resuming, not a second decision. It
    rewrites nothing: the journal is left exactly as it stands and only the missing
    confirmation is published, which `write_shown_confirmation` already makes
    idempotent against a file that is already correct. Any other retry — a
    different draft, or a row that was declined — is still refused by name.
    """
    # Preview validation is predictable and therefore belongs before the
    # irreversible journal half. `write_shown_confirmation` repeats it at the
    # publication seam, after a possible interruption, but it must not be the
    # first place a mismatch is discovered.
    draft_bytes, _persisted = _confirmation_form(draft, preview_sha256)
    target = Path(confirmation_path)
    with _write_lock(target, "confirmation-target"):
        _existing_confirmation_matches(target, draft_bytes)
        state = _load_journal(state_path, queue)
        recorded = (
            next(
                (entry for entry in state["decisions"] if entry["item_sha256"] == item_digest),
                None,
            )
            if state is not None
            else None
        )
        if recorded is None:
            state = append_decision(
                state_path, queue, item_digest=item_digest, decision="accept", draft=draft
            )
        elif recorded["decision"] != "accept" or recorded["draft_sha256"] != digest_bytes(
            draft_bytes
        ):
            raise TriageRefusal(
                "triage refusal decision-already-recorded: this row already carries a different "
                "decision, and decided rows are never rewritten"
            )
        _publish_confirmation(target, draft_bytes)
        return state


def draft_confirmation(
    queue: Mapping[str, Any],
    *,
    item_digest: str,
    corpus_id: str,
    appending_run: str,
    authority_identity: str,
    pages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Render the exact 6B confirmation object before an acceptance may write it."""
    _queue_bytes, persisted_queue = _checked_queue(queue)
    candidates = {
        digest_of(item): item
        for item in persisted_queue["items"]
        if isinstance(item, dict) and item.get("kind") == "cluster-candidate"
    }
    candidate = candidates.get(item_digest)
    if candidate is None:
        raise TriageRefusal(
            "triage refusal queue-item-missing: confirmation draft names no cluster candidate"
        )
    evidence = candidate["evidence"]
    confirmation = {
        "schema": CONFIRMATION_SCHEMA,
        "corpus_id": corpus_id,
        "appending_run": appending_run,
        "authority": {"kind": "human", "identity": authority_identity, "revision": None},
        "instrument_config_sha256": evidence["instrument_config_sha256"],
        "evidence_manifest_sha256": UNPINNED_EVIDENCE_MANIFEST,
        "clusters": [{"pages": list(pages), "evidence_pairs": [evidence["both_digests"]]}],
    }
    # `evidence_manifest_sha256` is left at the unbound placeholder deliberately:
    # the real digest is only known at preview time, and `pin_draft` supplies it.
    # This draft is therefore not yet committable, and both durable seams refuse
    # the placeholder (`_check_confirmation`) rather than trusting a caller to pin.
    _refuse_preference_named(confirmation, "confirmation-draft")
    return confirmation


def pin_draft(draft: Mapping[str, Any], *, evidence_manifest_sha256: str) -> dict[str, Any]:
    if not is_sha256(evidence_manifest_sha256):
        raise TriageRefusal(
            "triage refusal evidence-manifest-pin-invalid: preview needs a SHA-256 digest"
        )
    pinned = {**draft, "evidence_manifest_sha256": evidence_manifest_sha256}
    _refuse_preference_named(pinned, "confirmation-draft")
    return pinned


_CONFIRMATION_FIELDS = frozenset(
    {
        "schema",
        "corpus_id",
        "appending_run",
        "authority",
        "instrument_config_sha256",
        "evidence_manifest_sha256",
        "clusters",
    }
)

# `draft_confirmation` cannot know the evidence manifest's digest, so it emits this
# and `pin_draft` replaces it. It is a placeholder, never a digest: no evidence
# manifest hashes to it, and the producer refuses it at `_evidenced_pairs`. The
# console refuses it too, at the seam, so an unpinned draft cannot become a
# journalled acceptance whose confirmation file can never be committed.
UNPINNED_EVIDENCE_MANIFEST = "0" * 64


def _plain(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_confirmation(value: Mapping[str, Any]) -> None:
    """Refuse a draft the 6B producer would refuse, before it is durably written.

    This is the console's half of the boundary in `producer._confirmation_cluster_ids`.
    It deliberately mirrors only the *structural* rules — the ones checkable without
    the instrument recipe, evidence manifest and candidate records the producer also
    demands, none of which this offline console holds. The evidential half stays the
    producer's, and stays authoritative.

    The reason to mirror at all is that an acceptance is irreversible here. Writing a
    structurally invalid confirmation costs the operator a journalled `accept` bound
    to a file no producer will ever commit; refusing it at the seam costs a retry.
    `test_console_draft_is_accepted_and_edits_are_refused_by_the_producer` is what
    keeps the mirror honest: it drives a real `produce()` with a console-authored
    draft, so the two halves drifting apart fails a test rather than an operator.
    """
    if set(value) != _CONFIRMATION_FIELDS or value.get("schema") != CONFIRMATION_SCHEMA:
        raise TriageRefusal(
            "triage refusal draft-invalid: draft is not a closed 6B confirmation record"
        )
    _refuse_preference_named(value, "confirmation-draft")
    if not _plain(value["corpus_id"]) or not _plain(value["appending_run"]):
        raise TriageRefusal("triage refusal draft-invalid: draft names no corpus or appending run")
    if not is_sha256(value["instrument_config_sha256"]) or not is_sha256(
        value["evidence_manifest_sha256"]
    ):
        raise TriageRefusal("triage refusal draft-invalid: draft carries a malformed digest")
    if value["evidence_manifest_sha256"] == UNPINNED_EVIDENCE_MANIFEST:
        raise TriageRefusal(
            "triage refusal draft-unpinned: draft still carries the placeholder evidence-manifest "
            "digest and is bound to no instrument pass"
        )
    authority = value["authority"]
    if (
        not isinstance(authority, dict)
        or set(authority) != {"kind", "identity", "revision"}
        or authority["kind"] != "human"
        or not _plain(authority["identity"])
        or authority["revision"] is not None
    ):
        # The console only ever authors a human authority; a fixture or measured
        # authority reaching this seam means the draft came from somewhere else.
        raise TriageRefusal(
            "triage refusal draft-invalid: draft carries no closed human authority record"
        )
    clusters = value["clusters"]
    if not isinstance(clusters, list) or not clusters:
        raise TriageRefusal("triage refusal draft-invalid: draft declares no cluster")
    for cluster in clusters:
        if not isinstance(cluster, dict) or set(cluster) != {"pages", "evidence_pairs"}:
            raise TriageRefusal(
                "triage refusal draft-invalid: cluster is not a closed pages/evidence_pairs record"
            )
        pages = cluster["pages"]
        if not isinstance(pages, list) or not pages:
            raise TriageRefusal("triage refusal draft-invalid: cluster declares no physical page")
        for page in pages:
            members = page.get("member_frame_sha256") if isinstance(page, dict) else None
            if (
                not isinstance(page, dict)
                or set(page) != {"volume_id", "designation", "member_frame_sha256"}
                or not _plain(page["volume_id"])
                or not _plain(page["designation"])
                or not isinstance(members, list)
                or not members
                or not all(is_sha256(member) for member in members)
                or members != sorted(set(members))
            ):
                raise TriageRefusal(
                    "triage refusal draft-invalid: physical page is not a closed declaration"
                )
        pairs = cluster["evidence_pairs"]
        if (
            not isinstance(pairs, list)
            or not pairs
            or not all(
                isinstance(pair, list)
                and len(pair) == 2
                and all(is_sha256(member) for member in pair)
                for pair in pairs
            )
        ):
            raise TriageRefusal(
                "triage refusal draft-invalid: cluster retains no candidate evidence pair"
            )


def _confirmation_form(
    draft: Mapping[str, Any], preview_sha256: str
) -> tuple[bytes, dict[str, Any]]:
    """The fixed-point bytes and checked structure bound to the shown preview."""
    data, persisted = _persisted_form(draft, "confirmation-draft")
    _check_confirmation(persisted)
    if not is_sha256(preview_sha256) or digest_bytes(data) != preview_sha256:
        raise TriageRefusal(
            "triage refusal preview-changed: confirmation differs from the shown digest"
        )
    return data, persisted


def _existing_confirmation_matches(target: Path, data: bytes) -> bool:
    """Refuse an occupied target unless it already holds these producer-valid bytes."""
    if not target.exists():
        return False
    try:
        from operations.triage.producer import load_confirmation

        existing = load_confirmation(target)
    except (ProducerRefusal, SchemaRefusal, TypeError, ValueError) as error:
        raise TriageRefusal(f"triage refusal confirmation-target-invalid: {error}") from error
    if canonical_bytes(existing) != data:
        raise TriageRefusal(
            "triage refusal confirmation-target-exists: confirmation decisions are not overwritten"
        )
    return True


def _publish_confirmation(target: Path, data: bytes) -> None:
    """Publish checked bytes while the caller holds the target's write lock."""
    if not _existing_confirmation_matches(target, data):
        _atomic_bytes(target, data)


def write_shown_confirmation(
    path: str | Path, draft: Mapping[str, Any], *, preview_sha256: str
) -> None:
    """Commit exactly the bytes previewed; this is the digest pin at the write seam.

    Validated here, not only by the caller's later `append_decision`: this function
    is the one that durably writes the confirmation file, so it is the one that must
    refuse a malformed or preference-carrying draft before that write, not after it.
    A caller that validates afterward (as the decision record does) still closes the
    loop, but only this refusal keeps a bad draft from ever reaching disk.

    The validated structure is the one read back out of the bytes about to be
    written, so no container the serializer flattens can carry a field past the
    check — see `_persisted_form`.
    """
    data, _persisted = _confirmation_form(draft, preview_sha256)
    target = Path(path)
    # Producer's own loader is the consumer contract. Compared as bytes, not as
    # objects: these bytes are what a resumed acceptance recognises as published.
    with _write_lock(target, "confirmation-target"):
        _publish_confirmation(target, data)
