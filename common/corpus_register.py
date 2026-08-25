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

The chain is verified on read, not merely written: a reader replays it, so a
membership record removed or reordered from the middle of the register breaks
every successor's predecessor digest. What replay cannot see is truncation of
the newest link, because the register carries no external head; the run tree's
`register_digest` is that anchor, and comparing a fresh register against an
earlier run's is the cross-run check Unit 19 owns.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, Iterator

from common.contracts.canonical import canonical_bytes, digest_bytes, digest_of
from common.contracts.errors import ContractError, IncompatibleReuse, SchemaRefusal
from common.contracts.identities import is_well_formed, physical_act_id, physical_page_id

SCHEMA: Final = "corpus-register-v1"
_FORBIDDEN_PREFERENCE_FIELDS: Final = frozenset(
    {"primary", "canonical", "best", "preferred", "superseded_by"}
)


def empty_register() -> bytes:
    return canonical_bytes({"schema": SCHEMA, "records": []})


EMPTY_REGISTER_DIGEST: Final = digest_bytes(empty_register())


def register_digest(data: bytes) -> str:
    validate_register_bytes(data)
    return digest_bytes(data)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    with _register_lock(path):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = empty_register()
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
    """Serialize pathname replacement across writers; a crash releases the lock."""
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+b") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - supported register stores are POSIX
            yield
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
    if register_path is None:
        if sealed != EMPTY_REGISTER_DIGEST:
            raise IncompatibleReuse(
                "this run was created against a corpus register, so every stage in it must "
                "be given --corpus-register to check that register against the run's sealed "
                "snapshot; a skipped check reads exactly like a passed one"
            )
        return
    try:
        live = register_digest(Path(register_path).read_bytes())
    except OSError as error:
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
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SchemaRefusal("corpus register is not UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema", "records"}:
        raise SchemaRefusal("corpus register must be the closed {schema, records} record")
    if value["schema"] != SCHEMA or not isinstance(value["records"], list):
        raise SchemaRefusal("corpus register has an unknown schema or non-list records")
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

    A retracted head is not the head. The surviving link is the answer, and a
    page whose every link has been retracted reads as the same empty list as one
    that never had a capture — both mean "nothing currently shows this page",
    and the register still carries every retracted link and its reason.
    """
    head = _read(data)[1].membership_head.get(physical_page)
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


def resolve_proposal(data: bytes, act_id: str) -> dict[str, str]:
    """Resolve an image-local proposal through declared correspondence.

    This is intentionally lookup, not derivation: Unit 19 will infer and append
    correspondence records.  Until then an unresolved proposal is a named
    finding, never a silently new physical act.

    A retracted correspondence is not read back. Retraction is the register's
    only correction mechanism, and a correction that the reader ignores is not
    one: the retracted declaration stays in the register as evidence (GOVERNANCE
    4) and stops resolving anything (GOVERNANCE 2). A proposal whose every
    correspondence has been retracted is a named finding, distinct from one that
    never had a correspondence at all, because the two ask a caller for
    different things.
    """
    register = validate_register_bytes(data)
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


def _refuse_preference(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = set(value) & _FORBIDDEN_PREFERENCE_FIELDS
        if forbidden:
            raise SchemaRefusal(
                f"corpus register may not express capture preference: {sorted(forbidden)}"
            )
        for child in value.values():
            _refuse_preference(child)
    elif isinstance(value, list):
        for child in value:
            _refuse_preference(child)


def _closed(record: Any, fields: set[str], what: str) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != fields:
        raise SchemaRefusal(f"{what} must be the closed record {sorted(fields)}")
    return record


def _digests(value: Any, what: str) -> list[str]:
    if (
        not isinstance(value, list)
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


def _identity(value: Any, prefix: str, what: str) -> str:
    if not is_well_formed(value) or not value.startswith(f"{prefix}_"):
        raise SchemaRefusal(f"{what} must be a well-formed {prefix}_ identity")
    return value


def _evidence(value: Any, what: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise SchemaRefusal(f"{what} must name one or more evidence records")
    return value


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
        # correction that happened. It is refused rather than filed.
        if row["retracts"].startswith("membership:"):
            _retract_membership(row, reading)
        elif row["retracts"] not in reading.correspondences:
            raise SchemaRefusal(
                f"retraction names {row['retracts']!r}, which no earlier correspondence or "
                "membership link in this register declares; a retraction that corrects "
                "nothing is not a correction"
            )
        identity = f"retract:{row['retracts']}"
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
        raise SchemaRefusal(
            f"retraction names {row['retracts']!r}, which no earlier correspondence or "
            "membership link in this register declares; a retraction that corrects "
            "nothing is not a correction"
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
