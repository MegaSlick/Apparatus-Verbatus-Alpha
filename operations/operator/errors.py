"""Closed, testable copy for every message an operator can be shown.

The application boundary turns every known failure, and every otherwise
unexpected exception, into an :class:`OperatorError`.  The enum/table equality
check runs at import time, so adding a code without all three pieces of useful
copy makes the program fail during development rather than leak a raw error to
the person running it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ErrorCode(StrEnum):
    """Every operator-facing non-success state, with no fallback vocabulary."""

    INVALID_COMMAND = "invalid-command"
    SPEND_POLICY_UNCONFIGURED = "spend-policy-unconfigured"
    SPEND_POLICY_UNREADABLE = "spend-policy-unreadable"
    RECORD_WRITE_FAILED = "record-write-failed"
    UPLOAD_MANIFEST_MISSING = "upload-manifest-missing"
    UPLOAD_PARTIAL = "upload-partial"
    UPLOAD_REFUSED = "upload-refused"
    UPLOAD_VOLUME_UNAVAILABLE = "upload-volume-unavailable"
    RUN_INTERRUPTED = "run-interrupted"
    RUN_FAILED = "run-failed"
    RUN_HELD = "run-held"
    EXPORT_MISSING = "export-missing"
    EXPORT_AMBIGUOUS = "export-ambiguous"
    EXPORT_FAILED = "export-failed"
    EXPORT_PARTIAL = "export-partial"
    EXPORT_UNRECONCILED = "export-unreconciled"
    STATUS_EMPTY = "status-empty"
    STATUS_UNREADABLE = "status-unreadable"
    CONSOLE_TREE_UNREADABLE = "console-tree-unreadable"
    CONSOLE_PROJECTION_UNREADABLE = "console-projection-unreadable"
    ADVANCE_REFUSED = "advance-refused"
    DECISION_REFUSED = "decision-refused"
    BACKUP_FAILED = "backup-failed"
    CLEAR_LEFTOVERS_STOPPED = "clear-leftovers-stopped"
    FETCH_RUN_FAILED = "fetch-run-failed"
    CANARY_ALARM = "canary-alarm"
    CANARY_VERDICT_SAVE_FAILED = "canary-verdict-save-failed"
    CANARY_VERDICT_CONFLICT = "canary-verdict-conflict"
    INGEST_REFUSED = "ingest-refused"
    INGEST_UNRESOLVED = "ingest-unresolved"
    TRIAGE_REFUSED = "triage-refused"
    NOT_A_CHECKOUT = "not-a-checkout"
    INTERRUPTED = "interrupted"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class ErrorCopy:
    """The three statements every recovery-oriented error must carry."""

    what_happened: str
    what_it_means: str
    next_step: str

    def __post_init__(self) -> None:
        for name, value in (
            ("what_happened", self.what_happened),
            ("what_it_means", self.what_it_means),
            ("next_step", self.next_step),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"operator error {name} must be non-blank")


ERRORS: Final[dict[ErrorCode, ErrorCopy]] = {
    ErrorCode.INVALID_COMMAND: ErrorCopy(
        "That instruction was not understood.",
        "Nothing was started, changed, or billed.",
        "Run `verbatus` to see the available words, then try again; this is safe.",
    ),
    ErrorCode.SPEND_POLICY_UNCONFIGURED: ErrorCopy(
        "The reviewed spend policy is intentionally unconfigured.",
        "It has no approved ceilings, balance floor, or alert threshold, so Verbatus will not display it as configured.",
        "Keep this policy unconfigured until the project lead supplies a reviewed policy; no provider was contacted and nothing was changed.",
    ),
    ErrorCode.SPEND_POLICY_UNREADABLE: ErrorCopy(
        "Verbatus could not read the reviewed spend policy.",
        "No policy was presented as current, and no provider was contacted.",
        "Preserve the saved detail and use a readable reviewed policy file; this command changes nothing.",
    ),
    ErrorCode.RECORD_WRITE_FAILED: ErrorCopy(
        "Verbatus could not save the result of this step.",
        "The step is not claimed complete, though its action may already have happened.",
        "Do not repeat the step blindly. Preserve this message and its saved receipt path, then run `verbatus status`; if status cannot show it, ask for help.",
    ),
    ErrorCode.UPLOAD_MANIFEST_MISSING: ErrorCopy(
        "Upload needs a sealed submission record, but one was not available.",
        "No file was transferred and no pod is needed for this step.",
        "Create or locate the sealed submission record, then run `verbatus upload` again; this is safe.",
    ),
    ErrorCode.UPLOAD_PARTIAL: ErrorCopy(
        "Upload ended before every file was verified.",
        "The verified files remain recorded; unfinished files were not called complete, and no GPU-hours were used.",
        "Run `verbatus upload` again with the same files and submission record; it safely resumes the unfinished files.",
    ),
    ErrorCode.UPLOAD_REFUSED: ErrorCopy(
        "The submitted files could not be accepted for upload.",
        "Nothing was transferred and no pod was started.",
        "Read the saved submission reason, correct the named file or approval, then retry; this is safe.",
    ),
    ErrorCode.UPLOAD_VOLUME_UNAVAILABLE: ErrorCopy(
        "The network volume you named could not be prepared.",
        "Nothing was sent, nothing was started, and no pod was involved.",
        "Check the volume id, its datacenter, and that both storage-key environment "
        "variables are set on this computer, then run `verbatus upload` again; this is safe.",
    ),
    ErrorCode.RUN_INTERRUPTED: ErrorCopy(
        "The run was interrupted before all pages and acts were finished.",
        "Completed evidence remains in the run tree; it was not erased or called complete.",
        "Run `verbatus run` again with the same run name to resume; this is safe.",
    ),
    ErrorCode.RUN_FAILED: ErrorCopy(
        "The run could not reach its recorded end state.",
        "The run remains visible as a named incomplete result, not a success. The reason is "
        "on the detail line below and in the saved run record, with the run's output.",
        "Read the reason below, run `verbatus status` to see the saved run record and the "
        "review command for its run tree, then repair the named problem before resuming.",
    ),
    ErrorCode.RUN_HELD: ErrorCopy(
        "The run is held for review. This is a decision to make, not a failure.",
        "Every named act and hold reason was recorded, on this screen and in the saved run "
        "record; the run tree keeps every sealed stage and nothing in it was called "
        "complete. If notifications were enabled, a decision alert was also attempted.",
        "Read the hold reasons above or with `verbatus status`, and open the run tree "
        "read-only with the `verbatus review` command printed with the run. A hold is "
        "resolved only by a new authorized run over the same sealed source -- running "
        "`verbatus run` again with the same run name republishes the same hold; `advance` "
        "records permission to pass one sealed stage boundary and neither certifies a "
        "reading nor clears a hold; any correction of the text happens outside the "
        "pipeline. `verbatus export --run-id <run name>` copies what was delivered and "
        "says it is partial.",
    ),
    ErrorCode.EXPORT_MISSING: ErrorCopy(
        "There is no completed Armarium export record for that run.",
        "No local bundle was made and no result was invented.",
        "Run `verbatus run` first, or use the run name that already has an export; this is safe.",
    ),
    ErrorCode.EXPORT_AMBIGUOUS: ErrorCopy(
        "That run name is recorded under more than one run root.",
        "Verbatus will not guess which recorded run you mean, so no local bundle was made.",
        "Name the intended one with --run-root (the saved detail lists every candidate); this is safe.",
    ),
    ErrorCode.EXPORT_FAILED: ErrorCopy(
        "Verbatus could not make the local export copy.",
        "The sealed Armarium record remains where it was; no changed export was claimed.",
        "Check the local export folder, then run `verbatus export` again; this is safe.",
    ),
    ErrorCode.EXPORT_PARTIAL: ErrorCopy(
        "The export copied a run that is not complete.",
        "The bundle and its receipt were kept and hold only what the run delivered; every "
        "held, refused or unresolved act is listed above with its recorded reason, and "
        "nothing here calls the run complete.",
        "Read the recorded reasons, open the run tree read-only with `verbatus review`, and "
        "decide with the project lead what happens next; a hold is resolved only by a new authorized "
        "run over the same sealed source. Nothing was started or charged; this is safe.",
    ),
    ErrorCode.EXPORT_UNRECONCILED: ErrorCopy(
        "The recorded run claims complete, but its acts do not add up.",
        "The Armarium record was found and read; its delivered and non-delivered acts do "
        "not add up to a total that can be checked, so no bundle was made and nothing was "
        "called complete.",
        "Open the run tree read-only with `verbatus review` and check it against the sealed "
        "source with the project lead; this is not a corrupted export, it is a record that cannot back "
        "up its own claim. Nothing was started or charged; this is safe.",
    ),
    ErrorCode.STATUS_EMPTY: ErrorCopy(
        "There are no saved operator records to show.",
        "Status did not contact a provider or make a new record.",
        "Run the relevant Verbatus step first, then use `verbatus status`; this is safe.",
    ),
    ErrorCode.STATUS_UNREADABLE: ErrorCopy(
        "A saved operator record could not be read safely.",
        "Status did not guess what the record meant or contact a provider.",
        "Preserve that record for review and repair or replace it before continuing; this is safe.",
    ),
    ErrorCode.CONSOLE_TREE_UNREADABLE: ErrorCopy(
        "Verbatus could not read the selected run tree safely.",
        "It did not guess at missing evidence or change the run tree.",
        "Preserve the run tree unchanged and investigate the named evidence problem. Resume only from retained valid evidence, or create a new run; never edit the damaged evidence in place.",
    ),
    ErrorCode.CONSOLE_PROJECTION_UNREADABLE: ErrorCopy(
        "Verbatus could not read out the view of the run it built.",
        "This is a fault in this tool's own view, not a claim about the run tree, which was only read and is unchanged.",
        "Run the same `verbatus review` with `--json` to see the whole view, keep the saved detail below, and report it.",
    ),
    ErrorCode.ADVANCE_REFUSED: ErrorCopy(
        "The requested stage boundary could not be advanced.",
        "No later stage was started. A failure after the record was written leaves that immutable advance record in the run tree.",
        "Open review and inspect advance_records before retrying, then address the named seal problem; never assume a retry is record-free.",
    ),
    ErrorCode.DECISION_REFUSED: ErrorCopy(
        "The review decision was not recorded.",
        "Nothing was written to the run tree, and no stage was started.",
        "Read the named reason, open `verbatus review` on the run to see its current holds, and record the decision again against what it shows.",
    ),
    ErrorCode.BACKUP_FAILED: ErrorCopy(
        "The Mac backup did not finish with a verified snapshot.",
        "Existing content-addressed backup objects remain intact, but this run is not called backed up.",
        "Keep the saved detail, repair the named source or backup-directory problem, then run `verbatus backup` again; it safely reuses verified files.",
    ),
    ErrorCode.CLEAR_LEFTOVERS_STOPPED: ErrorCopy(
        "Clearing leftovers stopped part-way: part of the named folder could not be read or changed.",
        "Anything listed above as removed may already be gone, and an item being removed may remain renamed as `.<name>.clearing-<id>`, a folder partly emptied; the next run leaves it alone for an hour, then removes it. Nothing else was touched, and nothing was started or billed.",
        "Fix the folder named in the saved detail, then run `verbatus clear-leftovers` again; it only lists unless you add --apply.",
    ),
    ErrorCode.FETCH_RUN_FAILED: ErrorCopy(
        "The run tree was not brought back from the network volume as one verified whole.",
        "Nothing this attempt fetched was kept; files verified by an earlier fetch stay where they landed, an existing local file that differed was not touched, and no pod was started or billed.",
        "Keep the saved detail, repair the named object, digest, or local-copy conflict, then run `verbatus fetch-run` again; it safely reuses files an earlier fetch already verified.",
    ),
    ErrorCode.CANARY_ALARM: ErrorCopy(
        "A golden canary died in the fetched run.",
        "The run tree was fetched, but at least one stage failed its private canary check.",
        "Read the saved canary verdict and inspect the named stage before using this run's export.",
    ),
    ErrorCode.CANARY_VERDICT_SAVE_FAILED: ErrorCopy(
        "The golden canary verdict could not be saved.",
        "The fetched run tree is present, but its private canary check has no sealed verdict.",
        "Repair the private canary verdict location and fetch the run again.",
    ),
    ErrorCode.CANARY_VERDICT_CONFLICT: ErrorCopy(
        "An existing golden canary verdict conflicts with this fetched run.",
        "Both the fetched run and the earlier private verdict remain available.",
        "Preserve both records and investigate the changed canary verdict before using the export.",
    ),
    ErrorCode.INGEST_REFUSED: ErrorCopy(
        "The submission could not be prepared for the Door.",
        "No folder was called ready to submit, and no pod was started or billed.",
        "Read the refusal reason below, correct the named source, output folder, policy, instrument setting, or confirmation, then run `verbatus ingest` again; this is safe.",
    ),
    ErrorCode.INGEST_UNRESOLVED: ErrorCopy(
        "Ingest did not return a checked ready-folder record.",
        "No pod was started or billed, but immutable ingest records may have been written before the interruption.",
        "Do not reuse or remove the output folder. Preserve it and the saved detail, inspect its records, then use a new empty approved folder when retrying.",
    ),
    ErrorCode.TRIAGE_REFUSED: ErrorCopy(
        "Triage could not safely record or show that review step.",
        "Evidence was not changed. A mode declaration, queue decision, or confirmation may already have been written; this refusal did not silently roll recorded state back.",
        "Read the saved detail and inspect the named mode, journal, and confirmation files; repair the named problem, then resume the same step when the detail permits it.",
    ),
    ErrorCode.INTERRUPTED: ErrorCopy(
        "The Verbatus command was interrupted before it reported an end state.",
        "It is not called complete, and a provider or transfer action may already have started.",
        "Do not repeat a paid or destructive step blindly. Run `verbatus status`, preserve its records, and follow the named recovery step.",
    ),
    ErrorCode.NOT_A_CHECKOUT: ErrorCopy(
        "Verbatus was started from something that is not a source checkout.",
        "The configuration, stage code and proof material this needs live in the repository "
        "beside the code, not inside an installed package, so a run started here could not "
        "finish. Nothing was started and nothing was charged.",
        "Run `verbatus` from a checkout of the repository, the same way the pod bootstrap "
        "does: clone it at the commit you mean to run and use that working copy.",
    ),
    ErrorCode.UNEXPECTED: ErrorCopy(
        "Verbatus met a problem it could not classify.",
        "It did not report the problem as success. Where it could, it saved an `unexpected` "
        "receipt naming the failure, the command and the technical trace, and the detail "
        "line below names that receipt; if no receipt could be saved, the line says so and "
        "this message is the only record, so keep it.",
        "Copy or photograph this message, run `verbatus status` to read the saved records, "
        "and ask for help before retrying; this is safe.",
    ),
}


def assert_error_registry_complete() -> None:
    """Refuse a new error code without a complete three-part recovery message."""

    codes = set(ErrorCode)
    registered = set(ERRORS)
    if codes != registered:
        missing = sorted(code.value for code in codes - registered)
        extra = sorted(code.value for code in registered - codes)
        raise RuntimeError(f"operator error registry mismatch; missing={missing}, extra={extra}")
    for code, copy in ERRORS.items():
        if not isinstance(copy, ErrorCopy):
            raise RuntimeError(f"operator error registry entry {code.value!r} is not ErrorCopy")


assert_error_registry_complete()


class OperatorError(RuntimeError):
    """A recovery message plus private diagnostic detail, never a raw traceback."""

    def __init__(self, code: ErrorCode, *, detail: str | None = None) -> None:
        if not isinstance(code, ErrorCode):
            raise TypeError("operator errors must use a registered ErrorCode")
        self.code = code
        self.copy = ERRORS[code]
        self.detail = detail
        super().__init__(self.copy.what_happened)

    def render(self) -> str:
        lines = [
            f"What happened: {self.copy.what_happened}",
            f"What it means: {self.copy.what_it_means}",
            f"Next step: {self.copy.next_step}",
        ]
        if self.detail is not None:
            # Not truthiness: an empty detail must still render as "no
            # additional detail was recorded", not be dropped like a missing one.
            lines.append(f"Saved detail: {sanitize_detail(self.detail)}")
        return "\n".join(lines)


# C0/C1 control bytes, including ESC, plus Unicode format characters that
# reorder or invisibly change a terminal line: text this surface prints can
# embed an operator-uncontrolled filename or reason, and an ANSI escape
# sequence in one could clear the screen or spoof a confirmation line.
_CONTROL_CHARACTERS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)


def strip_control_bytes(value: str) -> str:
    """Make one line safe to print, and change nothing else about it.

    Every operator-facing line goes through this, not only an error detail,
    since a receipt summary, a reconciliation row and a recorded pod id come
    from the same untrusted places. Deliberately separate from
    `sanitize_detail`, which also rewrites vocabulary, collapses whitespace
    and truncates, none of which a reconciliation table should have done to
    it.
    """

    # A space, not deletion: deletion silently joins two identifiers into one
    # name the operator would read as a single thing.
    return _CONTROL_CHARACTERS.sub(" ", value)


def sanitize_detail(value: str, *, maximum: int = 2000) -> str:
    """Keep implementation wording and tracebacks out of the human message.

    Called in exactly one place, `render`, on the way to a person: a receipt
    is never rendered, so shortening or rewriting a persisted detail would
    discard the one copy of the diagnostic that exists anywhere.

    `maximum` stays generous rather than terminal-width-sized, since a
    workspace nested inside a synced cloud-drive folder can produce a
    receipt path several hundred characters long, and several error codes
    tell the person to preserve exactly that path. Where a cut still
    happens it is named, so a truncated fragment is never mistaken for a
    complete diagnostic.
    """

    # Control characters become single spaces; ordinary spaces are preserved.
    # Collapsing all whitespace would silently rewrite a receipt path that
    # legitimately contains two spaces, and the message tells the person to
    # preserve that exact path.
    compact = _CONTROL_CHARACTERS.sub(" ", value).strip()
    if not compact:
        return "no additional detail was recorded"
    trace = _TRACEBACK_SHAPE.search(compact)
    if trace is not None:
        # Keep everything before the structured trace header, since a
        # receipt path the operator is told to preserve may prefix it.
        prefix = compact[: trace.start()].rstrip()
        compact = " ".join(
            part for part in (prefix, "[technical trace omitted from this message]") if part
        )
    if len(compact) <= maximum:
        return compact
    marker = f" … (detail truncated at {maximum} characters)"
    return compact[:maximum] + marker


# Match real Python traceback structure, not a bare word that may legitimately
# occur in a path, pod id, filename, or step name.
_TRACEBACK_SHAPE: Final = re.compile(
    r"Traceback\s+\(most recent call last\):?.*",
    re.IGNORECASE | re.DOTALL,
)
