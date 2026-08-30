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

A wrong link is corrected the same way — by appending, never by editing. A
``retraction`` may name the *current head* of a page's chain, which restores the
predecessor it grew from and leaves the withdrawn link in place as evidence of
what was once declared. Only the head, because every link contains its
predecessor's members: withdrawing one from the middle would leave every
successor asserting the captures it withdrew. This is the answer to a human
confirming two frames as one physical page and being wrong, which no
deterministic instrument can catch — two blank forms agree everywhere.

A correspondence is corrected the same way and reasserted the same way. Its
retraction names the link itself rather than a chain head, because a
correspondence has no chain; and a later run may declare that same link again,
which is a new operator act with its own appending run and not the resurrection
of the withdrawn record. Without that, a retraction made in error would be a
corpus-lifetime fact: the act could never rejoin the physical act it belongs to,
and the only way round would be minting a second physical act for it — the exact
duplication the correspondence step exists to prevent. What may not reassert one
is the geometry-only resolver, which holds any component with a retracted member
rather than proposing it again; undoing a person's correction is a person's act.

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
    {
        "primary",
        "canonical",
        "best",
        "better",
        "preferred",
        "superseded_by",
        # The rest of the consult's §7 shape 1 vocabulary. These are binding
        # review words, so the screen spells all of them rather than the subset
        # that happened to appear first.
        "winner",
        "selected",
        "chosen",
        # These are witness-selection mechanisms under a different spelling.
        # `agree` is deliberately absent: page partition evidence legitimately
        # records `partition_disagreement`.
        "consensus",
        "majority",
        "vote",
        "quorum",
    }
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


def _resolved_register_path(register_path: str | Path, expected_digest: str) -> Path:
    """Resolve one register pathname and refuse a malformed expected digest.

    Both writers take this door, so a later tightening of either check cannot reach
    the appending path and miss the no-op one: a register path that append refused
    and a no-op confirmation accepted would be two safety rules wearing one name.
    """
    try:
        supplied_path = Path(register_path)
        path = supplied_path.parent.resolve(strict=False) / supplied_path.name
    except (OSError, RuntimeError, TypeError) as error:
        raise SchemaRefusal("corpus-register path could not be resolved") from error
    if not _is_sha256(expected_digest):
        raise SchemaRefusal("expected corpus-register digest must be lowercase SHA-256")
    return path


def _require_observed_head(current: bytes, expected_digest: str) -> str:
    """The compare half of the compare-and-swap, in one wording for both writers."""
    observed = register_digest(current)
    if observed != expected_digest:
        raise IncompatibleReuse(
            "the corpus register changed after this writer read it; the append was "
            "not written and must be rebuilt against the current register digest"
        )
    return observed


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
    path = _resolved_register_path(register_path, expected_digest)
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
        try:
            current, predecessor_identity = _read_register_path_with_identity(path)
        except FileNotFoundError:
            current = empty_register()
            predecessor_identity = None
        _require_observed_head(current, expected_digest)
        value = validate_register_bytes(current)
        successor = canonical_bytes({"schema": SCHEMA, "records": [*value["records"], *records]})
        successor_digest = register_digest(successor)
        _require_same_register_identity(path, predecessor_identity)
        _atomic_replace(path, successor)
        return successor_digest


def confirm_unchanged_head(register_path: str | Path, *, expected_digest: str) -> str:
    """Prove, under the writer lock, that the register is still the head a caller read.

    An append of no records is still a compare-and-swap. A writer that computes "there
    is nothing new to append" from bytes it read earlier has read them outside the
    lock, and a retraction published in between moves the head without changing what
    that writer would have appended — so returning its own stale digest reports a head
    that no longer exists, and whatever the caller publishes beside it names memberships
    the register has since withdrawn. This performs the same locked read-and-compare
    `append_records` performs, and returns the digest it proved.
    """
    path = _resolved_register_path(register_path, expected_digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _register_lock(path):
        try:
            current = _read_register_path(path, missing_ok=False)
        except FileNotFoundError:
            current = empty_register()
        return _require_observed_head(current, expected_digest)


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

    The lock name is predictable -- ``.<register name>.lock`` beside the register
    it guards -- so it is opened with ``O_NOFOLLOW``. Without that, anything able
    to place a symlink at that name before the first writer arrives would redirect
    every future writer's exclusion lock onto a file of its choosing: two corpora
    serialized against each other instead of themselves, or a file created at a
    path this process never named. A predictable name is safe only if the open
    refuses to follow it.
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
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise SchemaRefusal("the corpus-register lock is not one unaliased regular file")
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
    """HEAD-era entry point: bounded bytes, absent register optional."""
    try:
        return _read_register_path_with_identity(path)[0]
    except FileNotFoundError:
        if missing_ok:
            return empty_register()
        raise


def read_register_path(register_path: str | Path) -> bytes:
    """Read one direct, unaliased register file without following its final name."""
    return _read_register_path_with_identity(Path(register_path))[0]


def _read_register_path_with_identity(path: Path) -> tuple[bytes, tuple[int, int]]:
    """Read stable, bounded bytes and retain the device/inode identity checked at publish."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:  # pragma: no cover - supported register stores are POSIX
        raise SchemaRefusal(
            "this platform cannot read a corpus register without following symlinks"
        )
    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as error:
        raise SchemaRefusal(
            "corpus register path must be a direct, readable regular file"
        ) from error
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise SchemaRefusal("corpus register path must be one unaliased regular file")
        if status.st_size > MAX_REGISTER_BYTES:
            raise SchemaRefusal(
                f"the corpus register is {status.st_size} bytes, past the "
                f"{MAX_REGISTER_BYTES}-byte validation bound"
            )
        data = handle.read(MAX_REGISTER_BYTES + 1)
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) != (status.st_dev, status.st_ino):
            raise SchemaRefusal("corpus register path changed while its bytes were read")
    if len(data) > MAX_REGISTER_BYTES:
        raise SchemaRefusal(
            f"the corpus register is past the {MAX_REGISTER_BYTES}-byte validation bound"
        )
    return data, (status.st_dev, status.st_ino)


def _require_same_register_identity(path: Path, expected: tuple[int, int] | None) -> None:
    """Refuse a pathname swap between predecessor validation and replacement."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        actual = None
    except OSError as error:
        raise IncompatibleReuse(
            "the corpus register identity could not be rechecked before publication"
        ) from error
    else:
        if stat.S_ISLNK(status.st_mode):
            actual = None
        else:
            actual = (status.st_dev, status.st_ino)
    if actual != expected:
        raise IncompatibleReuse(
            "the corpus register path changed after its predecessor was validated; the append "
            "was not written and must be rebuilt against the current register file"
        )


def _atomic_replace(path: Path, data: bytes) -> None:
    """Publish complete register bytes atomically and make the name durable."""
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(raw_temporary)
    replaced = False
    failure: ContractError | None = None
    cause: OSError | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
        )
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
        failure = ContractError(f"the corpus register {state}")
        cause = error
    cleanup_error = _temporary_cleanup_error(temporary)
    if failure is not None:
        if cleanup_error is not None:
            failure.add_note(
                f"corpus-register temporary {temporary} also could not be removed: {cleanup_error}"
            )
        raise failure from cause
    if cleanup_error is not None:
        # The replacement and the directory fsync both succeeded, so the new register is
        # live and this refusal is about the leftover alone. It has to say so: a caller
        # that reads "could not be removed" as "nothing was written" rebuilds its append
        # against the previous digest, which the moved head then refuses as a concurrent
        # change — two refusals for one durable, successful publish.
        raise SchemaRefusal(
            f"the corpus register was replaced and is durable at digest {digest_bytes(data)}; "
            f"only the temporary {temporary} could not be removed. Do not retry this append "
            "against the previous digest"
        ) from cleanup_error


def _temporary_cleanup_error(path: Path) -> OSError | None:
    """Return cleanup failure so the corpus-register refusal remains primary."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        return error
    return None


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

    One implementation for stage context openers, so every caller that accepts a
    live register applies the same meaning of "the register moved".

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
        live = register_digest(read_register_path(register_path))
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
        # Retracted links must leave this lookup so the same link can later be
        # reasserted as a new operator act.
        self.correspondence_records: dict[str, dict[str, Any]] = {}
        # Every act any correspondence ever named, so a proposal whose links were
        # all withdrawn reads differently from one that never had any.
        self.correspondence_declared: set[str] = set()
        self.correspondence_active: dict[str, set[str]] = {}
        self.membership_head: dict[str, tuple[str, frozenset[str]]] = {}
        # Every link of every page's chain, oldest first, so a retraction of the
        # head can restore the predecessor it grew from without the register
        # carrying a second, editable copy of "what the members are now".
        self.membership_chain: dict[str, list[tuple[str, frozenset[str]]]] = {}
        self.membership_links: set[str] = set()
        self.retracted_membership_links: set[str] = set()


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
    refuse_capture_preference(value)
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

    A retracted head is not the head. The surviving link is the answer, and a
    page whose every link has been retracted reads as the same empty list as one
    that never had a capture — both mean "nothing currently shows this page",
    and the register still carries every retracted link and its reason.
    """
    reading = _read(data)[1]
    _identity(physical_page, "ppg", "membership lookup physical page")
    head = reading.membership_head.get(physical_page)
    return sorted(head[1]) if head is not None else []


def membership_heads(data: bytes) -> dict[str, tuple[str, frozenset[str]]]:
    """Return the replayed current head of every membership chain.

    A historical scan of ``membership`` records is not equivalent to replay: a
    retraction leaves its withdrawn link in the register as evidence. Writers that
    extend a chain need both the surviving members and that surviving link's digest,
    so this deliberately exposes the same replayed state that :func:`members_of`
    reads rather than inviting each writer to reconstruct it differently.
    """
    return dict(_read(data)[1].membership_head)


def physical_act_page(data: bytes, physical_act: str) -> str | None:
    """The physical page one physical act was minted on, or None if undeclared.

    A physical act belongs to exactly one physical page and validation enforces
    it, so this is a lookup with one answer. It exists so a writer naming an
    existing physical act proves it against the register rather than against the
    page it happens to be proposing.
    """
    # Validated like `members_of` and `resolve_proposal` do, so a malformed
    # identity is a named refusal rather than a `None` that reads identically to
    # "this physical act was never declared".
    _identity(physical_act, "pac", "physical act page lookup physical_act")
    return _read(data)[1].physical_act_pages.get(physical_act)


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
    different things. A resolved row retains both the rendered page declared by
    the correspondence and the physical page declared by its physical act, so a
    consumer can prove both sides of the local act's lineage instead of dropping
    the former during lookup.
    """
    _identity(act_id, "act", "proposal resolution act_id")
    reading = _read(data)[1]
    active = reading.correspondence_active.get(act_id, set())
    if not active:
        code = (
            "retracted-physical-act"
            if act_id in reading.correspondence_declared
            else "unresolved-physical-act"
        )
        return {"outcome": "finding", "code": code, "act_id": act_id}
    if len(active) != 1:
        return {"outcome": "finding", "code": "ambiguous-physical-act", "act_id": act_id}
    # Exactly one, proven above rather than chosen.
    (physical_act,) = active
    correspondence = reading.correspondence_records[f"{act_id}->{physical_act}"]
    # The physical page comes from the register's own declaration, never from a
    # caller's alignment table: a physical act is minted on exactly one physical
    # page (validation enforces it), so every match agrees and reading it here is
    # a lookup rather than a choice between rows.
    return {
        "outcome": "resolved",
        "act_id": act_id,
        "page_id": correspondence["page_id"],
        "physical_act_id": physical_act,
        "physical_page_id": reading.physical_act_pages[physical_act],
    }


def _correspondence_identity(record: dict[str, Any]) -> str:
    return f"{record['act_id']}->{record['physical_act_id']}"


def refuse_capture_preference(value: Any, *, what: str = "corpus register") -> None:
    """Refuse a nested capture-preference claim, naming the record it was in.

    Public because the rule is not the corpus register's alone: a Testimonium
    must not express preference either (ARCHITECTURE, GOVERNANCE 3), and it was
    reaching this through the private name -- which also told an operator
    reading a witness record that the *corpus register* was at fault.

    Iterative on purpose: the value is untrusted input, and a deeply nested
    payload must exhaust the walk's own list, never the interpreter stack.
    """
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            forbidden = set(current) & _FORBIDDEN_PREFERENCE_FIELDS
            if forbidden:
                raise SchemaRefusal(
                    f"{what} may not express capture preference: {sorted(forbidden)}"
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
            {
                "kind",
                "corpus_id",
                "volume_id",
                "designation",
                "physical_page_id",
                "appending_run",
            },
            kind,
        )
        expected = physical_page_id(row["corpus_id"], row["volume_id"], row["designation"])
        if row["physical_page_id"] != expected:
            raise SchemaRefusal("physical-page record id does not bind its declaration")
        # Every other record kind names the run that appended it, and a
        # declaration needs it most: the register is append-only, so a folio
        # typed against the wrong volume stands for ever, and without this there
        # is no field on which to find the rest of what that same pass entered.
        # It is not one of `physical_page_id`'s bindings, so identity is
        # unchanged by carrying it.
        if not isinstance(row["appending_run"], str) or not row["appending_run"]:
            raise SchemaRefusal("physical-page record names no appending run")
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
        if identity in reading.correspondence_records:
            raise SchemaRefusal(
                f"corpus register already declares correspondence {identity!r} and no "
                "retraction has withdrawn it; declaring it twice records nothing new"
            )
        reading.correspondence_records[identity] = row
        reading.correspondence_declared.add(row["act_id"])
        reading.correspondence_active.setdefault(row["act_id"], set()).add(row["physical_act_id"])
        # Like a membership link, the record's identity carries the run that
        # appended it: reasserting a withdrawn correspondence is a new act by a
        # new run, not the resurrection of the immutable record that was
        # withdrawn. Without the run, a corrected link could never be re-declared
        # at all, and a mistaken retraction would be a corpus-lifetime fact.
        identity = f"{identity}@{row['appending_run']}"
    elif kind == "retraction":
        row = _closed(record, {"kind", "retracts", "reason", "appending_run"}, kind)
        if not all(
            isinstance(row[field], str) and row[field]
            for field in ("retracts", "reason", "appending_run")
        ):
            raise SchemaRefusal("retraction record is malformed")
        # A retraction naming nothing retracts nothing while reading as a
        # correction that happened. It is refused rather than filed.
        if row["retracts"].startswith("membership:"):
            _retract_membership(row, reading)
        elif row["retracts"] not in reading.correspondence_records:
            # Either nothing ever declared it, or an earlier retraction already
            # withdrew it. Both are a retraction that corrects nothing.
            raise SchemaRefusal(
                f"retraction names {row['retracts']!r}, which no earlier correspondence or "
                "membership link in this register declares; a retraction that corrects "
                "nothing is not a correction"
            )
        else:
            withdrawn = reading.correspondence_records.pop(row["retracts"])
            reading.correspondence_active[withdrawn["act_id"]].discard(withdrawn["physical_act_id"])
        identity = f"retract:{row['retracts']}@{row['appending_run']}"
    else:
        raise SchemaRefusal(f"unknown corpus register record kind {kind!r}")
    if identity in reading.seen:
        raise SchemaRefusal(f"corpus register repeats immutable record {identity!r}")
    reading.seen.add(identity)


def _retract_membership(row: dict[str, Any], reading: _Reading) -> None:
    """Withdraw the newest link of one physical page's membership chain.

    This is the correction path for the case the instrument is blind to: two
    frames a human confirmed as one physical page when they are not — two blank
    forms that agree everywhere because neither carries ink. Memberships grow and
    are never edited, so without this a wrong confirmation is a corpus-lifetime
    fact nobody can answer, and GOVERNANCE 2 does not allow a result that can
    only be wrong in silence.

    Only the current head may be retracted, and that restriction is the whole
    design rather than a convenience. Each link's members contain its
    predecessor's, so retracting a link from the middle would leave every
    successor still asserting the captures it withdrew — a correction the reader
    would have to ignore, which GOVERNANCE 4 says is not a correction. Unwinding
    from the head is the only order in which the surviving head is the honest
    answer; a page corrected two links deep is corrected by two retractions.

    Nothing is deleted. The retracted link stays in the register as evidence of
    what was once declared and of the appending run that declared it; it simply
    stops being the answer to "what shows this page".
    """
    target = row["retracts"].removeprefix("membership:")
    page = next(
        (
            name
            for name, head in reading.membership_head.items()
            if head[0] == target and reading.membership_chain.get(name)
        ),
        None,
    )
    if page is None:
        if target in reading.retracted_membership_links:
            raise SchemaRefusal(
                f"retraction names membership link {target!r}, which was already retracted; "
                "a retraction that corrects nothing is not a correction"
            )
        if target in reading.membership_links:
            raise SchemaRefusal(
                f"retraction names membership link {target!r}, which is not the current head "
                "of its page's chain; every successor contains the captures it declared, so "
                "withdrawing it would leave them asserted anyway. Retract from the head."
            )
        # Its own wording, not the generic branch's. The two are reached by different
        # routes — a target that opened with `membership:` was searched for among the
        # links, and one that did not was searched for among the correspondences — and
        # a message that cannot tell an operator which namespace was searched also
        # cannot tell a test that the routing above still exists.
        raise SchemaRefusal(
            f"retraction names membership link {target!r}, which this register never declares; "
            "a retraction that corrects nothing is not a correction"
        )
    chain = reading.membership_chain[page]
    chain.pop()
    reading.retracted_membership_links.add(target)
    if chain:
        reading.membership_head[page] = chain[-1]
    else:
        # The page keeps its declaration and returns to having no capture yet,
        # which `members_of` already spells as the empty list.
        del reading.membership_head[page]


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
    membership_digest = digest_of(row)
    link = (membership_digest, members)
    reading.membership_head[page] = link
    reading.membership_chain.setdefault(page, []).append(link)
    reading.membership_links.add(membership_digest)
