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
    EXPORT_MISSING = "export-missing"
    EXPORT_FAILED = "export-failed"
    CLOSE_NOTHING = "close-nothing"
    CLOSE_LEASE_UNREADABLE = "close-lease-unreadable"
    CLOSE_LEASE_RECORD_FAILED = "close-lease-record-failed"
    CLOSE_UNVERIFIED = "close-unverified"
    CLOSE_REFUSED = "close-refused"
    STATUS_EMPTY = "status-empty"
    STATUS_UNREADABLE = "status-unreadable"
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
        "No paid or destructive action was started, because the record is missing.",
        "Check that the project folder can be written, then try the command again; this is safe.",
    ),
    ErrorCode.RECORD_WRITE_FAILED: ErrorCopy(
        "Verbatus could not save the result of this step.",
        "The step is not claimed complete, and a paid or destructive action may already have reached its fixture boundary.",
        "Do not repeat a paid or destructive action. Preserve this message, then run `verbatus status` if it can read the earlier records; this is safe.",
    ),
    ErrorCode.PRICE_CHANGED: ErrorCopy(
        "The fixture price changed after the screen you confirmed.",
        "Your earlier confirmation was not used to authorize a different price.",
        "Read the new price and ceilings, then type a new confirmation only if they are acceptable; this is safe.",
    ),
    ErrorCode.SAFETY_CHECK_FAILED: ErrorCopy(
        "Launch was refused by a required safety check.",
        "No paid provider action was sent from this refusal path.",
        "Read the saved detail, repair the named setup or limit, then preview launch again; this is safe.",
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
        "Launch was refused by its safety checks.",
        "Verbatus sent no new paid provider action.",
        "Read the stated limit or setup problem, correct it, then run `verbatus launch` again; this is safe.",
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
    ErrorCode.UNEXPECTED: ErrorCopy(
        "Verbatus met a problem it could not classify.",
        "It did not report the problem as success; the saved diagnostic says where it ended.",
        "Run `verbatus status`, preserve the record, and ask for help before retrying; this is safe.",
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
        if self.detail:
            lines.append(f"Saved detail: {_safe_detail(self.detail)}")
        return "\n".join(lines)


def _safe_detail(value: str) -> str:
    """Keep implementation wording and tracebacks out of the human message."""

    compact = " ".join(value.split())
    if not compact:
        return "no additional detail was recorded"
    if "traceback" in compact.lower():
        return "a technical detail was saved locally; this step was not called complete"
    for pattern, replacement in (
        (r"\bshutdown\b", "close"),
        (r"\btermination\b", "close"),
        (r"\bterminate(?:d|s|ing)?\b", "close"),
        (r"\bstop(?:ped|s|ping)?\b", "paused"),
    ):
        compact = re.sub(pattern, replacement, compact, flags=re.IGNORECASE)
    return compact[:240]
