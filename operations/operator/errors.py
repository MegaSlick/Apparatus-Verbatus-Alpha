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
    LIVE_PROVIDER_BLOCKED = "live-provider-blocked"
    SPEND_POLICY_REQUIRED = "spend-policy-required"
    CONFIRMATION_REQUIRED = "confirmation-required"
    CONFIRMATION_RECORD_FAILED = "confirmation-record-failed"
    RECORD_WRITE_FAILED = "record-write-failed"
    PRICE_CHANGED = "price-changed"
    BALANCE_FLOOR_REACHED = "balance-floor-reached"
    BALANCE_UNOBSERVABLE = "balance-unobservable"
    SAFETY_CHECK_FAILED = "safety-check-failed"
    LAUNCH_UNRESOLVED = "launch-unresolved"
    ACTIVE_POD_REQUIRES_CLOSE = "active-pod-requires-close"
    ADOPTION_REFUSED = "adoption-refused"
    PAID_ACTION_REFUSED = "paid-action-refused"
    PROVIDER_TIMEOUT = "provider-timeout"
    PROVIDER_ERROR = "provider-error"
    UPLOAD_MANIFEST_MISSING = "upload-manifest-missing"
    UPLOAD_PARTIAL = "upload-partial"
    UPLOAD_REFUSED = "upload-refused"
    UPLOAD_VOLUME_UNAVAILABLE = "upload-volume-unavailable"
    BOOT_RED = "boot-red"
    RUN_INTERRUPTED = "run-interrupted"
    RUN_FAILED = "run-failed"
    RUN_HELD = "run-held"
    EXPORT_MISSING = "export-missing"
    EXPORT_FAILED = "export-failed"
    CLOSE_NOTHING = "close-nothing"
    CLOSE_LEASE_UNREADABLE = "close-lease-unreadable"
    CLOSE_LEASE_RECORD_FAILED = "close-lease-record-failed"
    CLOSE_UNVERIFIED = "close-unverified"
    CLOSE_REFUSED = "close-refused"
    STATUS_EMPTY = "status-empty"
    STATUS_UNREADABLE = "status-unreadable"
    CONSOLE_CUSTODY_REFUSED = "console-custody-refused"
    CONSOLE_TREE_UNREADABLE = "console-tree-unreadable"
    ADVANCE_REFUSED = "advance-refused"
    BACKUP_FAILED = "backup-failed"
    INGEST_REFUSED = "ingest-refused"
    INGEST_PREVIEW_UNRESOLVED = "ingest-preview-unresolved"
    INGEST_UNRESOLVED = "ingest-unresolved"
    TRIAGE_REFUSED = "triage-refused"
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
    ErrorCode.LIVE_PROVIDER_BLOCKED: ErrorCopy(
        "This copy of Verbatus is not allowed to contact a cloud provider.",
        "No pod was inspected, started, or billed.",
        "Use the offline rehearsal only, or ask for the separately approved live demonstration; this is safe.",
    ),
    ErrorCode.SPEND_POLICY_REQUIRED: ErrorCopy(
        "Launch could not proceed because no reviewed spend limit is configured.",
        "Verbatus sent no new paid provider action.",
        "Set the GPU and spending limits in the reviewed policy, then run `verbatus launch` again; this is safe.",
    ),
    ErrorCode.CONFIRMATION_REQUIRED: ErrorCopy(
        "The required typed confirmation was not received.",
        "No new paid or destructive action was started.",
        "Read the price or close notice, then type the exact confirmation when you are ready; this is safe.",
    ),
    ErrorCode.CONFIRMATION_RECORD_FAILED: ErrorCopy(
        "Verbatus could not save your confirmation before acting.",
        "No paid or destructive action was started, because a usable indexed record is missing.",
        "Check that the project folder can be written, then try the command again; this is safe.",
    ),
    ErrorCode.RECORD_WRITE_FAILED: ErrorCopy(
        "Verbatus could not save the result of this step.",
        "The step is not claimed complete, and a paid or destructive action may already have reached its fixture boundary.",
        "Do not repeat a paid or destructive action. Preserve this message and its saved receipt path, then run `verbatus status`; if status cannot show it, ask for help.",
    ),
    ErrorCode.PRICE_CHANGED: ErrorCopy(
        "The fixture price changed after the screen you confirmed.",
        "Your earlier confirmation was not used to authorize a different price.",
        "Read the new price and ceilings, then type a new confirmation only if they are acceptable; this is safe.",
    ),
    ErrorCode.BALANCE_FLOOR_REACHED: ErrorCopy(
        "Launch could not preserve the reviewed account-balance reserve.",
        "Verbatus sent no new paid provider action from this refusal path.",
        "Read the observed balance and reserved liabilities, restore enough available balance, then preview launch again; this is safe.",
    ),
    ErrorCode.BALANCE_UNOBSERVABLE: ErrorCopy(
        "Launch could not establish the current account balance or reserved liability.",
        "Verbatus sent no new paid provider action because the reviewed reserve could not be proved.",
        "Repair the named balance source or lease record, then preview launch again; this is safe.",
    ),
    ErrorCode.SAFETY_CHECK_FAILED: ErrorCopy(
        "Launch could not prove that its runtime safeguards were ready.",
        "No paid provider action was sent from this refusal path.",
        "Read the saved detail, repair the named controller, lease, or close-readiness problem, then preview launch again; this is safe.",
    ),
    ErrorCode.LAUNCH_UNRESOLVED: ErrorCopy(
        "Launch could not prove that its fixture pod is safely accounted for.",
        "It is not called ready, and a provider request may already have occurred.",
        "Do not launch again. Run `verbatus status`, preserve the saved receipt, and resolve the named close evidence before retrying.",
    ),
    ErrorCode.ACTIVE_POD_REQUIRES_CLOSE: ErrorCopy(
        "A recorded fixture pod is still awaiting a verified close.",
        "Verbatus will not begin or adopt another fixture pod while that cost record is open.",
        "Run `verbatus close` for the recorded pod, then read `verbatus status` before another launch; this is safe.",
    ),
    ErrorCode.ADOPTION_REFUSED: ErrorCopy(
        "The existing fixture pod was not adopted by Verbatus.",
        "Verbatus sent no new paid provider action, but that existing pod may still be billing.",
        "Run `verbatus status`, read the saved pod and cost records, and do not assume billing ended before deciding what to do next.",
    ),
    ErrorCode.PAID_ACTION_REFUSED: ErrorCopy(
        "Launch exceeds a reviewed spending limit.",
        "Verbatus sent no new paid provider action.",
        "Read the stated price and limit, revise the reviewed policy or request if appropriate, then run `verbatus launch` again; this is safe.",
    ),
    ErrorCode.PROVIDER_TIMEOUT: ErrorCopy(
        "The test provider did not answer in time.",
        "Verbatus cannot say that the requested action happened.",
        "Run `verbatus status` to read the saved record, then retry only if it says to; this is safe.",
    ),
    ErrorCode.PROVIDER_ERROR: ErrorCopy(
        "The test provider could not complete the requested check or action.",
        "The result is recorded as unresolved rather than treated as success.",
        "Run `verbatus status`, read the saved result, then retry the named step when it is safe.",
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
    ErrorCode.BOOT_RED: ErrorCopy(
        "Boot finished with a red report.",
        "The environment is not ready to run, and Verbatus did not call it ready.",
        "Repair the named setup, cache, or proof-page check, then run `verbatus boot` again; this is safe.",
    ),
    ErrorCode.RUN_INTERRUPTED: ErrorCopy(
        "The run was interrupted before all pages and acts were finished.",
        "Completed evidence remains in the run tree; it was not erased or called complete.",
        "Run `verbatus run` again with the same run name to resume; this is safe.",
    ),
    ErrorCode.RUN_FAILED: ErrorCopy(
        "The run could not reach its recorded end state.",
        "The run remains visible as a named incomplete result, not a success.",
        "Run `verbatus status` to read the saved run record, then repair the named problem before resuming.",
    ),
    ErrorCode.RUN_HELD: ErrorCopy(
        "The run is held for review. This is a decision to make, not a failure.",
        "Every named act and hold reason was recorded; if notifications were enabled, a "
        "decision alert was also attempted.",
        "Run `verbatus status` to read the hold reasons, resolve them, then run `verbatus run` again with the same run name; this is safe.",
    ),
    ErrorCode.EXPORT_MISSING: ErrorCopy(
        "There is no completed Armarium export record for that run.",
        "No local bundle was made and no result was invented.",
        "Run `verbatus run` first, or use the run name that already has an export; this is safe.",
    ),
    ErrorCode.EXPORT_FAILED: ErrorCopy(
        "Verbatus could not make the local export copy.",
        "The sealed Armarium record remains where it was; no changed export was claimed.",
        "Check the local export folder, then run `verbatus export` again; this is safe.",
    ),
    ErrorCode.CLOSE_NOTHING: ErrorCopy(
        "There is no recorded pod waiting to be closed.",
        "No close request was sent and nothing changed.",
        "Run `verbatus status` to read the saved records before taking another step; this is safe.",
    ),
    ErrorCode.CLOSE_LEASE_UNREADABLE: ErrorCopy(
        "Verbatus could not read the safety lease for the recorded pod.",
        "No close request was sent, so the pod and its billing may still exist.",
        "Do not assume billing ended. Preserve the saved launch receipt and ask for the lease record to be repaired before trying close again.",
    ),
    ErrorCode.CLOSE_LEASE_RECORD_FAILED: ErrorCopy(
        "Close reached the fixture provider, but Verbatus could not update its safety lease.",
        "The result is not treated as complete, even if provider evidence was observed.",
        "Do not repeat close until you have checked the recorded pod and repaired the named lease record; this is safe.",
    ),
    ErrorCode.CLOSE_UNVERIFIED: ErrorCopy(
        "Close could not verify both pod absence and billing evidence.",
        "The pod is not claimed closed, and no claim is made about future charges.",
        "Follow the manual check named in the close record now, then run `verbatus close` again only when it is safe.",
    ),
    ErrorCode.CLOSE_REFUSED: ErrorCopy(
        "Close was not authorized by its typed confirmation.",
        "No close request was sent and the recorded pod was left unchanged.",
        "Read the close notice, then type the exact confirmation when you are ready; this is safe.",
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
    ErrorCode.CONSOLE_CUSTODY_REFUSED: ErrorCopy(
        "The operator console or a custody worker could not complete inside its OS boundary.",
        "No provider action was reached. An advance may have appended a record, and a backup may have added verified objects, even though no checked result returned.",
        "Follow the saved detail below. Inspect advance_records before retrying an advance; for a backup, keep its objects and retry after fixing the named boundary; otherwise fix Landlock or Seatbelt before reopening the console.",
    ),
    ErrorCode.CONSOLE_TREE_UNREADABLE: ErrorCopy(
        "The operator console could not read the selected run tree safely.",
        "It did not guess at missing evidence or change the run tree.",
        "Preserve the run tree unchanged and investigate the named evidence problem. Resume only from retained valid evidence, or create a new run; never edit the damaged evidence in place.",
    ),
    ErrorCode.ADVANCE_REFUSED: ErrorCopy(
        "The requested stage boundary could not be advanced.",
        "No later stage was started. A worker failure after append may have left an immutable advance record even though no checked result returned.",
        "Open review and inspect advance_records before retrying, then address the named seal or worker problem; never assume a retry is record-free.",
    ),
    ErrorCode.BACKUP_FAILED: ErrorCopy(
        "The Mac backup did not finish with a verified snapshot.",
        "Existing content-addressed backup objects remain intact, but this run is not called backed up.",
        "Keep the saved detail, repair the named source, backup-directory, or worker-report problem, then run `verbatus backup` again; it safely reuses verified files.",
    ),
    ErrorCode.INGEST_REFUSED: ErrorCopy(
        "The submission could not be prepared for the Door.",
        "No folder was called ready to submit, and no pod was started or billed.",
        "Read the refusal reason below, correct the named source, output folder, policy, instrument setting, or confirmation, then run `verbatus ingest` again; this is safe.",
    ),
    ErrorCode.INGEST_PREVIEW_UNRESOLVED: ErrorCopy(
        "Ingest could not show you the plan it was going to write.",
        "Nothing was written: the preview runs with no write rights at all, so the output folder you chose is untouched.",
        "Keep the saved detail and run `verbatus ingest` again with the same empty output folder; this is safe.",
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
    ErrorCode.UNEXPECTED: ErrorCopy(
        "Verbatus met a problem it could not classify.",
        "It did not report the problem as success; this terminal message is the only record "
        "of what happened, so keep it.",
        "Copy or photograph this message, run `verbatus status` to check for saved records "
        "from earlier steps, and ask for help before retrying; this is safe.",
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
            # Not truthiness: a raise site that had a diagnostic slot and
            # filled it with nothing is a different fact from one that never
            # had a diagnostic to give, and the line below says which.
            lines.append(f"Saved detail: {sanitize_detail(self.detail)}")
        return "\n".join(lines)


# C0/C1 control bytes, including ESC (0x1B), plus Unicode format characters
# that reorder or invisibly change a terminal line: text this surface prints can embed
# a filename, a path or a refusal reason an operator did not choose (a submitted
# folder, a manifest entry, a page-census reason that travelled up from the run
# tree), and an ANSI escape sequence in one could clear the screen or spoof a
# fake confirmation line on the terminal that renders it.
_CONTROL_CHARACTERS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]"
)


def strip_control_bytes(value: str) -> str:
    """Make one line safe to print, and change nothing else about it.

    Every operator-facing line goes through this, not only an error detail: the
    argument above is about where the text came from, and a receipt summary, a
    reconciliation row and a recorded pod id come from the same places a detail
    does. Deliberately separate from `sanitize_detail`, which additionally
    rewrites vocabulary, collapses whitespace and truncates — none of which a
    reconciliation table or a price screen should have done to it.
    """

    # A space, not deletion: deletion silently joins two identifiers into one
    # name the operator would read as a single thing.
    return _CONTROL_CHARACTERS.sub(" ", value)


def sanitize_detail(value: str, *, maximum: int = 2000) -> str:
    """Keep implementation wording and tracebacks out of the human message.

    Called in exactly one place: `render`, on the way to a person. A raise site
    passes its detail through raw, and so does every site that persists a
    detail into a receipt — a receipt is never rendered, and a person never
    reads it through this function, so shortening or rewriting its text before
    it is saved would discard the one copy of the diagnostic that exists
    anywhere. Calling this anywhere but `render` is a second spelling of one
    idea, and for a persist site it is worse than redundant: it can throw away
    evidence GOVERNANCE 2 requires to still be visibly there.

    `maximum` stays generous rather than terminal-width-sized: a workspace
    nested inside a synced cloud-drive folder can easily produce a receipt
    path several hundred characters long, and truncating that away would
    silently break the "preserve this message and its saved receipt path"
    instruction most of these error codes give. Where the cut still happens it
    is named, because a rendered fragment that reads as a whole diagnostic is
    exactly the partial result GOVERNANCE 2 requires to be visibly partial.
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
        # Keep everything before the structured trace header. In particular,
        # RECORD_WRITE_FAILED prefixes a receipt path the operator is told to
        # preserve; suppressing frames must not suppress that usable evidence.
        prefix = compact[: trace.start()].rstrip()
        compact = " ".join(
            part for part in (prefix, "[technical trace omitted from this message]") if part
        )
    compact = " ".join(_translate_close_vocabulary(word) for word in compact.split(" "))
    if len(compact) <= maximum:
        return compact
    marker = f" … (detail truncated at {maximum} characters)"
    return compact[:maximum] + marker


_CLOSE_VOCABULARY: Final = (
    (re.compile(r"(?<![\w.-])shutdown(?![\w.-])", re.IGNORECASE), "close"),
    (re.compile(r"(?<![\w.-])termination(?![\w.-])", re.IGNORECASE), "close"),
    (re.compile(r"(?<![\w.-])terminate(?:d|s|ing)?(?![\w.-])", re.IGNORECASE), "close"),
    (re.compile(r"(?<![\w.-])stop(?:ped|s|ping)?(?![\w.-])", re.IGNORECASE), "paused"),
)

# Match real Python traceback structure, not a bare word that may legitimately
# occur in a path, pod id, filename, or step name.
_TRACEBACK_SHAPE: Final = re.compile(
    r"Traceback\s+\(most recent call last\):?.*",
    re.IGNORECASE | re.DOTALL,
)


def _translate_close_vocabulary(word: str) -> str:
    """Rewrite old close vocabulary in one prose word, never inside a path.

    A path segment is exactly the substring this rewrite must not touch — a
    receipt path with "stop" or "shutdown" in one of its directory names is
    not prose to translate, it is an identifier a person needs intact to find
    the file again, and this module's own detail contract asks a raise site to
    "preserve this message and its saved receipt path". A path separator is
    decisive. Dots, underscores and hyphens touching the vocabulary are also
    preserved because a bare submitted name such as ``stop-list`` has no
    separator but is still an identifier the person needs byte-true. A bare
    name equal to one ordinary prose word is inherently ambiguous at this
    unstructured boundary.
    """

    if "/" in word or "\\" in word:
        return word
    for pattern, replacement in _CLOSE_VOCABULARY:
        word = pattern.sub(replacement, word)
    return word
