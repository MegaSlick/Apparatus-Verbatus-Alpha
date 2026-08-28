"""The corpus-scoped, append-only declaration register.

It is intentionally not a run-tree artifact.  Triage declares physical pages
and the later correspondence step mints physical acts here; a run receives only
an immutable content-addressed snapshot of the register bytes.

**Every record is immutable, so nothing that can grow lives inside one.** A
physical page's declaration is its `{corpus_id, volume_id, designation}` and
nothing else; the captures known to show it are carried by separate
``membership`` records, each naming the digest of the membership record it
succeeds. A fourth capture found next month appends a fifth record rather than
editing the first — which is what "append-only" has to mean if `physical_page_id`
is not to be re-derived under everything beneath it, and what GOVERNANCE 4 means
one level above the run tree.

The chain is verified on read, not merely written: a reader replays it, so a
membership record removed or reordered from the middle of the register breaks
every successor's predecessor digest. What replay cannot see is truncation of
the newest link, because the register carries no external head; the run tree's
`register_digest` is that anchor, and comparing a fresh register against an
earlier run's is therefore required to detect tail loss between runs.
"""

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, Iterator

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import (
    act_id as local_act_id,
)
from common.contracts.identities import (
    is_well_formed,
    physical_act_id,
    physical_page_id,
)

SCHEMA: Final = "corpus-register-v1"
MAX_REGISTER_BYTES: Final = 64 * 1024 * 1024
MAX_REGISTER_RECORDS: Final = 100_000
MAX_RECORD_LIST_ITEMS: Final = 100_000
_FORBIDDEN_PREFERENCE_FIELDS: Final = frozenset(
    {"primary", "canonical", "best", "preferred", "superseded_by"}
)


def empty_register() -> bytes:
    return canonical_bytes({"schema": SCHEMA, "records": []})


EMPTY_REGISTER_DIGEST: Final = digest_bytes(empty_register())


def register_digest(data: bytes) -> str:
    validate_register_bytes(data)
    return digest_bytes(data)


def read_register_file(register_path: str | Path) -> bytes:
    """Read one bounded regular register without following its final name.

    A corpus register is mutable evidence outside the run tree.  Opening it by
    pathname through ``Path.read_bytes`` lets a symlink substitution redirect a
    stage to unrelated bytes between invocations.  The descriptor is therefore
    the object checked and read, and a platform without ``O_NOFOLLOW`` refuses
    rather than silently weakening that boundary.
    """
    return _read_register_path(Path(register_path), missing_ok=False)


def append_records(
    register_path: str | Path,
    records: list[dict[str, Any]],
    *,
    expected_digest: str,
) -> str:
    """Append immutable records with optimistic concurrency and atomic durability.

    The register is one canonical JSON value, so an OS-level append would expose
    a torn document to readers after a crash.  Instead, the writer locks a stable
    sibling, validates the complete predecessor, proves the caller observed that
    exact digest, validates the complete successor, and atomically replaces the
    pathname with flushed same-directory bytes.  A crash can leave the old value
    or the new value (and possibly an unreferenced temporary), never a torn value.

    ``expected_digest`` is the external head a writer observed.  It prevents two
    concurrent resolvers from both extending one predecessor and silently losing
    whichever append publishes first.
    """
    path = Path(register_path)
    if not _is_sha256(expected_digest):
        raise SchemaRefusal("expected corpus-register digest must be lowercase SHA-256")
    if (
        not isinstance(records, list)
        or not records
        or not all(isinstance(record, dict) for record in records)
    ):
        raise SchemaRefusal("a corpus-register append must contain one or more records")
    if len(records) > MAX_REGISTER_RECORDS:
        raise SchemaRefusal(
            f"a corpus-register append has {len(records)} records, past the "
            f"{MAX_REGISTER_RECORDS}-record replay bound"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with _register_lock(path):
        current = _read_register_path(path, missing_ok=True)
        observed = register_digest(current)
        if observed != expected_digest:
            raise IncompatibleReuse(
                "the corpus register changed after this writer read it; the append was "
                "not written and must be rebuilt against the current register digest"
            )
        value = validate_register_bytes(current)
        successor = canonical_bytes({"schema": SCHEMA, "records": [*value["records"], *records]})
        successor_digest = register_digest(successor)
        _atomic_replace(path, successor)
        return successor_digest


@contextmanager
def _register_lock(path: Path) -> Iterator[None]:
    """Serialize pathname replacement across writers; a crash releases the lock.

    The lock is load-bearing rather than advisory comfort. The append it guards is
    a read-compare-replace against `expected_digest`, and that compare-and-swap is
    only sound while one writer at a time holds the section: two writers that both
    read digest D both satisfy the check, and the second `os.replace` discards the
    first one's records with nothing anywhere recording that it happened. So every
    way this function could fail to serialize refuses instead of proceeding — a
    register append that was silently not serialized is exactly the append-only
    evidence loss GOVERNANCE 2 and 4 forbid.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:  # pragma: no cover - supported register stores are POSIX
        raise SchemaRefusal(
            "this platform cannot lock a corpus register without following symlinks"
        )
    flags = os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise SchemaRefusal(
            "the corpus-register lock could not be opened without following a redirect"
        ) from error
    with os.fdopen(descriptor, "a+b") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise SchemaRefusal("the corpus-register lock is not a regular file")
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - supported stores are POSIX
            raise SchemaRefusal(
                "this platform cannot lock a corpus register: without flock the "
                "append's digest compare-and-swap would let one writer discard "
                "another's records unseen"
            ) from error
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_register_path(path: Path, *, missing_ok: bool) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:  # pragma: no cover - supported register stores are POSIX
        raise SchemaRefusal(
            "this platform cannot read a corpus register without following symlinks"
        )
    flags = os.O_RDONLY | no_follow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return empty_register()
        raise
    except (OSError, ValueError) as error:
        raise SchemaRefusal(
            "the corpus register could not be opened as a regular non-symlink file"
        ) from error
    with os.fdopen(descriptor, "rb") as handle:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise SchemaRefusal("the corpus register is not a regular file")
        if details.st_size > MAX_REGISTER_BYTES:
            raise SchemaRefusal(
                f"the corpus register is {details.st_size} bytes, past the "
                f"{MAX_REGISTER_BYTES}-byte validation bound"
            )
        data = handle.read(MAX_REGISTER_BYTES + 1)
    if len(data) > MAX_REGISTER_BYTES:
        raise SchemaRefusal(
            f"the corpus register is past the {MAX_REGISTER_BYTES}-byte validation bound"
        )
    return data


def _atomic_replace(path: Path, data: bytes) -> None:
    """Publish complete register bytes atomically and make the name durable."""
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(raw_temporary)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        state = (
            "was replaced but its directory entry is not proven durable"
            if replaced
            else "was not replaced"
        )
        raise ContractError(f"the corpus register {state}") from error
    finally:
        temporary.unlink(missing_ok=True)


def read_snapshot(tree: Any, run: dict[str, Any]) -> bytes:
    """Read and verify the immutable register bytes sealed into one run."""
    digest = run.get("register_digest")
    if not _is_sha256(digest):
        raise IncompatibleReuse("run.json carries no valid register_digest")
    relative_path = tree.blob_path("door", digest)
    try:
        data = tree.read_bytes(relative_path)
    except OSError as error:
        raise IncompatibleReuse(
            "the corpus-register snapshot sealed by run.json is missing or unreadable"
        ) from error
    if digest_bytes(data) != digest:
        raise IncompatibleReuse(
            "the corpus-register snapshot bytes do not match run.json's register_digest"
        )
    validate_register_bytes(data)
    return data


def verify_snapshot_is_current(run: dict[str, Any], register_path: str | None) -> None:
    """The live register must still be the register this run sealed a snapshot of.

    One implementation, called by every stage's context opener, so the Exemplar
    and the six stages behind it cannot drift on what "the register moved" means.

    A run created against a real register refuses when no register is offered at
    all. The check is what makes the snapshot binding, and a check that an
    operator disables by forgetting a flag is not one — an appended
    correspondence would otherwise reach half a run's stages and none of the
    others, with nothing anywhere saying so.
    """
    sealed = run.get("register_digest")
    required = run.get("register_required")
    if not _is_sha256(sealed) or not isinstance(required, bool):
        raise IncompatibleReuse(
            "run.json does not carry a valid corpus-register digest and presence binding"
        )
    if register_path is None:
        if required:
            raise IncompatibleReuse(
                "this run was created against a corpus register, so every stage in it must "
                "be given --corpus-register to check that register against the run's sealed "
                "snapshot; a skipped check reads exactly like a passed one"
            )
        if sealed != EMPTY_REGISTER_DIGEST:
            raise IncompatibleReuse(
                "run.json claims no corpus register but binds a non-empty register snapshot"
            )
        return
    if not required:
        raise IncompatibleReuse(
            "this run was created without a corpus register, so a later stage may not introduce "
            "one; start a new run against that register instead"
        )
    try:
        live = register_digest(read_register_file(register_path))
    except (OSError, ContractError) as error:
        raise IncompatibleReuse("the corpus register could not be read") from error
    if live != sealed:
        raise IncompatibleReuse(
            "the corpus register changed after this run was created; stages must read its "
            "sealed snapshot rather than a drifting live register"
        )


class _Reading:
    """What replaying the records establishes, carried between record checks."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.physical_pages: set[str] = set()
        self.physical_acts: set[str] = set()
        self.physical_act_pages: dict[str, str] = {}
        self.correspondences: set[str] = set()
        self.membership_head: dict[str, tuple[str, frozenset[str]]] = {}
        self.membership_assertions: dict[str, tuple[str, str]] = {}
        self.retracted_members: dict[str, set[str]] = {}


def validate_register_bytes(data: bytes) -> dict[str, Any]:
    return _read(data)[0]


def _read(data: bytes) -> tuple[dict[str, Any], _Reading]:
    """The validated register and everything replaying its records established."""
    if not isinstance(data, bytes):
        raise SchemaRefusal("corpus register must be bytes")
    if len(data) > MAX_REGISTER_BYTES:
        raise SchemaRefusal(
            f"corpus register is {len(data)} bytes, past the "
            f"{MAX_REGISTER_BYTES}-byte validation bound"
        )
    try:
        value = json.loads(data.decode("utf-8"))
    except RecursionError as error:
        # Told apart from a parse failure on purpose. A pathologically nested
        # register is valid UTF-8 and valid JSON; only its depth defeats the
        # parser. Folded into the message below, it sent an operator to check the
        # file's encoding, where there is nothing to find.
        raise SchemaRefusal(
            "corpus register is nested too deeply for this parser to read; it is "
            "well-formed JSON whose structure is past the recursion bound"
        ) from error
    except (UnicodeDecodeError, ValueError) as error:
        raise SchemaRefusal("corpus register is not UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema", "records"}:
        raise SchemaRefusal("corpus register must be the closed {schema, records} record")
    if value["schema"] != SCHEMA or not isinstance(value["records"], list):
        raise SchemaRefusal("corpus register has an unknown schema or non-list records")
    if len(value["records"]) > MAX_REGISTER_RECORDS:
        raise SchemaRefusal(
            f"corpus register has {len(value['records'])} records, past the "
            f"{MAX_REGISTER_RECORDS}-record replay bound"
        )
    _refuse_preference(value)
    reading = _Reading()
    for record in value["records"]:
        _validate_record(record, reading)
    if canonical_bytes(value) != data:
        raise SchemaRefusal("corpus register must be canonical JSON before it can be snapshotted")
    return value, reading


def members_of(data: bytes, physical_page: str) -> list[str]:
    """The captures currently declared to show one physical page.

    The head of that page's membership chain, never a member list read out of
    the declaration itself — the declaration has none. An undeclared page and a
    declared page with no capture yet are both the empty list on purpose: this
    reads membership, it does not assert that a page exists.
    """
    reading = _read(data)[1]
    _identity(physical_page, "ppg", "membership lookup physical page")
    head = reading.membership_head.get(physical_page)
    if head is None:
        return []
    active = head[1] - reading.retracted_members.get(physical_page, set())
    return sorted(active)


def resolve_proposal(data: bytes, act_id: str) -> dict[str, str]:
    """Resolve an image-local proposal through declared correspondence.

    This is intentionally lookup, not derivation: only an appended
    correspondence record can resolve a proposal. An unresolved proposal is a
    named finding, never a silently new physical act.

    A retracted correspondence is not read back. Retraction is the register's
    only correction mechanism, and a correction that the reader ignores is not
    one: the retracted declaration stays in the register as evidence (GOVERNANCE
    4) and stops resolving anything (GOVERNANCE 2). A proposal whose every
    correspondence has been retracted is a named finding, distinct from one that
    never had a correspondence at all, because the two ask a caller for
    different things.
    """
    register = validate_register_bytes(data)
    _identity(act_id, "act", "proposal resolution act_id")
    retracted = {
        record["retracts"] for record in register["records"] if record["kind"] == "retraction"
    }
    declared = [
        record
        for record in register["records"]
        if record["kind"] == "correspondence" and record["act_id"] == act_id
    ]
    matches = [record for record in declared if _correspondence_identity(record) not in retracted]
    if not matches:
        code = "retracted-physical-act" if declared else "unresolved-physical-act"
        return {"outcome": "finding", "code": code, "act_id": act_id}
    physical_acts = {record["physical_act_id"] for record in matches}
    if len(physical_acts) != 1:
        return {"outcome": "finding", "code": "ambiguous-physical-act", "act_id": act_id}
    return {"outcome": "resolved", "act_id": act_id, "physical_act_id": physical_acts.pop()}


def _correspondence_identity(record: dict[str, Any]) -> str:
    return f"{record['act_id']}->{record['physical_act_id']}"


def _membership_assertion_identity(physical_page: str, member: str) -> str:
    """The retractable assertion that one capture shows one physical page."""
    return f"membership:{physical_page}->{member}"


def _refuse_preference(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            forbidden = set(current) & _FORBIDDEN_PREFERENCE_FIELDS
            if forbidden:
                raise SchemaRefusal(
                    f"corpus register may not express capture preference: {sorted(forbidden)}"
                )
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _closed(record: Any, fields: set[str], what: str) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != fields:
        raise SchemaRefusal(f"{what} must be the closed record {sorted(fields)}")
    return record


def _digests(value: Any, what: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_RECORD_LIST_ITEMS
        or value != sorted(set(value))
        or not all(_is_sha256(member) for member in value)
    ):
        raise SchemaRefusal(f"{what} must be sorted unique source digests")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _identity(value: Any, prefix: str, what: str) -> None:
    if not is_well_formed(value) or not value.startswith(f"{prefix}_"):
        raise SchemaRefusal(f"{what} must be a well-formed {prefix}_ identity")


def _evidence(value: Any, what: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_RECORD_LIST_ITEMS
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise SchemaRefusal(f"{what} must name one or more evidence records")


def _validate_record(record: Any, reading: _Reading) -> None:
    if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
        raise SchemaRefusal("corpus register record has no kind")
    kind = record["kind"]
    if kind == "physical-page":
        # No `members` field: a declaration is immutable, and what a page's
        # captures are is the one thing about it that grows.
        row = _closed(
            record,
            {"kind", "corpus_id", "volume_id", "designation", "physical_page_id"},
            kind,
        )
        expected = physical_page_id(row["corpus_id"], row["volume_id"], row["designation"])
        if row["physical_page_id"] != expected:
            raise SchemaRefusal("physical-page record id does not bind its declaration")
        identity = row["physical_page_id"]
        reading.physical_pages.add(identity)
    elif kind == "membership":
        row = _closed(
            record,
            {"kind", "physical_page_id", "members", "predecessor", "appending_run"},
            kind,
        )
        _validate_membership(row, reading)
        identity = f"membership:{digest_of(row)}"
        for member in row["members"]:
            assertion = _membership_assertion_identity(row["physical_page_id"], member)
            reading.membership_assertions[assertion] = (row["physical_page_id"], member)
    elif kind == "physical-act":
        row = _closed(
            record,
            {
                "kind",
                "physical_page_id",
                "mint_designation",
                "physical_act_id",
                "evidence",
                "appending_run",
            },
            kind,
        )
        expected = physical_act_id(row["physical_page_id"], row["mint_designation"])
        if (
            row["physical_act_id"] != expected
            or not isinstance(row["appending_run"], str)
            or not row["appending_run"]
        ):
            raise SchemaRefusal("physical-act record is malformed or does not bind its mint")
        _evidence(row["evidence"], "physical-act evidence")
        _require_declared(row["physical_page_id"], reading.physical_pages, "physical page")
        identity = row["physical_act_id"]
        reading.physical_acts.add(identity)
        reading.physical_act_pages[identity] = row["physical_page_id"]
    elif kind == "correspondence":
        row = _closed(
            record,
            {
                "kind",
                "page_id",
                "act_id",
                "act_class",
                "act_bounds",
                "physical_page_id",
                "physical_act_id",
                "evidence",
                "appending_run",
            },
            kind,
        )
        if not isinstance(row["appending_run"], str) or not row["appending_run"]:
            raise SchemaRefusal("correspondence record is malformed")
        _identity(row["page_id"], "pg", "correspondence page_id")
        _identity(row["act_id"], "act", "correspondence act_id")
        _identity(row["physical_page_id"], "ppg", "correspondence physical_page_id")
        _identity(row["physical_act_id"], "pac", "correspondence physical_act_id")
        try:
            expected_local_act = local_act_id(row["page_id"], row["act_class"], row["act_bounds"])
        except (KeyError, TypeError) as error:  # pragma: no cover - defensive
            raise SchemaRefusal("correspondence has malformed local act bindings") from error
        except ContractError as error:
            raise SchemaRefusal(
                f"correspondence has malformed local act bindings: {error}"
            ) from error
        if row["act_id"] != expected_local_act:
            raise SchemaRefusal(
                "correspondence act_id does not bind the page, class, and bounds beside it"
            )
        _evidence(row["evidence"], "correspondence evidence")
        _require_declared(row["physical_page_id"], reading.physical_pages, "physical page")
        _require_declared(row["physical_act_id"], reading.physical_acts, "physical act")
        if reading.physical_act_pages[row["physical_act_id"]] != row["physical_page_id"]:
            raise SchemaRefusal(
                "correspondence physical_act_id was minted for a different physical page"
            )
        identity = _correspondence_identity(row)
        reading.correspondences.add(identity)
    elif kind == "retraction":
        row = _closed(record, {"kind", "retracts", "reason", "appending_run"}, kind)
        if not all(
            isinstance(row[field], str) and row[field]
            for field in ("retracts", "reason", "appending_run")
        ):
            raise SchemaRefusal("retraction record is malformed")
        # A retraction naming nothing retracts nothing while reading as a
        # correction that happened. It is refused rather than filed. Both
        # resolvable assertion kinds are retractable: an act correspondence,
        # or the claim that one capture shows one physical page.
        membership = reading.membership_assertions.get(row["retracts"])
        if row["retracts"] not in reading.correspondences and membership is None:
            raise SchemaRefusal(
                f"retraction names {row['retracts']!r}, which no earlier correspondence or "
                "membership assertion in this register declares; a retraction that corrects "
                "nothing is not a correction"
            )
        if membership is not None:
            page, member = membership
            reading.retracted_members.setdefault(page, set()).add(member)
        identity = f"retract:{row['retracts']}"
    else:
        raise SchemaRefusal(f"unknown corpus register record kind {kind!r}")
    if identity in reading.seen:
        raise SchemaRefusal(f"corpus register repeats immutable record {identity!r}")
    reading.seen.add(identity)


def _require_declared(identity: str, declared: set[str], what: str) -> None:
    if identity not in declared:
        raise SchemaRefusal(
            f"corpus register names {what} {identity!r} before any earlier record declares it"
        )


def _validate_membership(row: dict[str, Any], reading: _Reading) -> None:
    """One link of one physical page's append-only membership chain."""
    page = row["physical_page_id"]
    if not isinstance(page, str) or not page:
        raise SchemaRefusal("membership record names no physical page")
    if not isinstance(row["appending_run"], str) or not row["appending_run"]:
        raise SchemaRefusal("membership record names no appending run")
    members = frozenset(_digests(row["members"], "membership members"))
    if not members:
        # A membership record that names no capture asserts nothing, and it is
        # immutable once written: no retraction can name an assertion that was
        # never made. `members_of` would report it as `[]`, which is exactly what
        # a page with no membership record at all reports — so a triage bug that
        # wrote one could never be told apart from a page nobody has photographed.
        # `_evidence` refuses an empty list for this reason; so does this. Later
        # records cannot be empty anyway, since each must strictly grow.
        raise SchemaRefusal(
            f"membership record for {page!r} names no capture; a record that asserts "
            "nothing cannot be retracted and cannot be told from no record at all"
        )
    prior = reading.membership_head.get(page)
    if prior is None:
        _require_declared(page, reading.physical_pages, "physical page")
        if row["predecessor"] is not None:
            raise SchemaRefusal(
                f"the first membership record for {page!r} names a predecessor that is not in "
                "this register; a chain cannot start midway through itself"
            )
    else:
        predecessor_digest, prior_members = prior
        if row["predecessor"] != predecessor_digest:
            raise SchemaRefusal(
                f"membership record for {page!r} does not name the digest of the membership "
                "record it succeeds; the chain is what makes growth append-only rather than "
                "an edit nobody can see"
            )
        if not members > prior_members:
            raise SchemaRefusal(
                f"membership record for {page!r} does not add a capture to its predecessor; "
                "membership grows, and a capture already declared is never withdrawn"
            )
    reading.membership_head[page] = (digest_of(row), members)
