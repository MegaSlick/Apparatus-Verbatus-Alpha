"""The one admission module: what may enter, decided by bytes alone.

The harvest's first defect this spec exists to kill: admission rules used to exist
**twice** and drifted (`.gif` accepted by one path, refused by another). There is
exactly one format policy — `config/admitted_formats.toml`, loaded here — and
every caller reads a decision from this module rather than repeating the rule.

**Admission is by bytes, never by extension.** A file's declared name plays no part
in what it *is*: `image_formats.sniff()` reads the real signature and the structural
validators prove the bytes are a genuine instance of whatever they claim. A
`.png`-named file of garbage is refused for what its bytes are, not for its name; a
`.txt`-named file of a genuine JPEG is admitted under the format its bytes prove,
for the same reason. Nothing here ever looks at a suffix — not even to refuse,
because a suffix check that can refuse is a suffix check that decides, and then the
rule exists in two places again.

**Tyrel's ruling 2026-08-04, item 2: nothing is rejected; every image format
works.** "Nothing should be rejected — any image and image formats should work. If
things are failing the image got corrupted … or the pipeline is broken." A refusal
here is always one of two facts, and both are alarms: the bytes are damaged (they do
not match a declared digest, or the container is genuinely malformed), or this
project cannot yet read the format (a gap in the pipeline, never a decision about
his file). There is no third category — "refused by policy" — and the `refuse`
action the walking skeleton shipped is gone. A format nothing here decodes yet gets
the `gap` action instead: still refused today, but refused as a named defect this
project owes him, counted under the same `UNSUPPORTED_VARIANT` code a genuine
undecodable variant gets, because both say the same thing — "we have work to do,"
never "your file is wrong."

Refusal reasons are a closed set — `RefusalReason` below — because the walking
skeleton's free-text reasons are exactly what this spec is required to replace. The
admission payload shape stays what the skeleton landed (`declared_path`, `ordinal`,
`reason`): `reason` is still one string field, and its *content* is now always
`"<reason-code>: <detail>"` with the code drawn from the closed set. `reason_code()`
reads it back, so a consumer can check membership without the payload growing a
field the landed contract does not have.
"""

import tomllib
from enum import Enum
from pathlib import Path
from typing import Final, NamedTuple

import image_formats
from image_formats import MAX_SOURCE_BYTES, FormatRefusal, sniff, validate

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

DEFAULT_FORMAT_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "admitted_formats.toml"
)

# The four things the policy may say about a format. A fifth word in the file is
# refused at load rather than treated as "not admit".
#
# There is deliberately no "refuse" action (ruling 2026-08-04, item 2 deleted it):
# nothing here declines a real, uncorrupted file as a matter of policy. A format
# this project cannot yet decode gets `GAP`, not a refusal-by-name — see the module
# docstring.
ADMIT: Final = "admit"
RENDER_PAGES: Final = "render-pages"
# A format that is *usually* one image but may structurally hold more than one —
# today, exactly TIFF. The door probes every admitted-or-fan-out source for its real
# page count before deciding anything: a single-directory file is admitted as-is,
# unmodified, exactly like `ADMIT`; a multi-directory file is fanned out and each
# page rendered, exactly like `RENDER_PAGES`. Unlike `RENDER_PAGES`, this action
# never re-encodes the common single-page case — Tyrel's ruling that TIFF "100%
# must work" is about not losing a page, not about re-touching bytes that already
# seal cleanly as they are.
ADMIT_OR_FAN_OUT: Final = "admit-or-fan-out"
# A format `image_formats.sniff()` can name, with no reader built yet. Ruling
# 2026-08-04, item 2: "GIF, HEIC, WebP, BMP, and the rest — admit as readers land
# ... until a reader exists, a real file of that format is a named gap, never a
# refusal by policy." The refusal this still produces is real — nothing decodes the
# bytes — but it is filed under `UNSUPPORTED_VARIANT`, the same code a genuine
# undecodable variant of a *supported* format gets, because both mean the same
# thing: a pipeline defect, not a fact about his file.
GAP: Final = "gap"
ACTIONS: Final = frozenset({ADMIT, RENDER_PAGES, ADMIT_OR_FAN_OUT, GAP})

# Every format name `image_formats.sniff()` can return, and every format a
# structural validator exists for. The policy must cover the first set exactly: a
# format missing from the file could otherwise be admitted by omission, and a format
# named in the file that nothing can sniff is a rule nobody will ever reach — the
# silent drift this module exists to prevent.
#
# **Both are imported, never restated.** They used to be hand-written copies of the
# sniffer's return values and the validator table's keys, so the coverage check
# compared one copy against another and would have agreed however far either had
# drifted from the code that actually runs. `render-pages` formats are absent from
# the second set on purpose: a container is proved by rendering it, not by a raster
# validator.
STRUCTURALLY_VALIDATED: Final = image_formats.STRUCTURALLY_VALIDATED
SNIFFABLE_FORMATS: Final = image_formats.SNIFFABLE_FORMATS


class FormatPolicyRefusal(ContractError):
    """The admission list itself is unreadable, incomplete, or unenforceable.

    Fail closed: a policy that cannot be read is not an empty policy, and a policy
    that admits a format nothing can verify is refused rather than honoured. Either
    way nothing is admitted, which is the only safe reading of "unknown".
    """


class RefusalReason(str, Enum):
    """The closed vocabulary a door refusal is drawn from.

    Every member is exercised by the test suite beside this module — an unused
    member would be an untested refusal path, which is exactly the gap invariant
    #3 exists to close, and `test_admission.py` asserts the coverage rather than
    trusting it.
    """

    EMPTY = "empty"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too-large"
    UNRECOGNIZED_FORMAT = "unrecognized-format"
    CORRUPT = "corrupt"
    UNSUPPORTED_VARIANT = "unsupported-variant"
    DIGEST_MISMATCH = "digest-mismatch"
    DUPLICATE = "duplicate"


class AdmissionOutcome(NamedTuple):
    """One source's admission decision: enough for the caller to publish it."""

    outcome: str  # "admitted" | "refused"
    reason: str | None  # "<RefusalReason.value>: <detail>", only when refused
    detected_format: str | None
    digest: str | None  # the sha256 of the bytes the decision was made on
    geometry: tuple[int, int] | None  # (width, height), read off the real container


def reason(code: RefusalReason, detail: str) -> str:
    """The one spelling of a refusal reason. Never assembled anywhere else."""
    return f"{code.value}: {detail}"


class RenderRefusal(ValueError):
    """One page, or one whole multi-page container, refused with a closed-set reason.

    Shared by every door-private container renderer — `pdf_render.py`,
    `tiff_render.py` — so `door.py` catches one exception type regardless of which
    renderer produced it, rather than a per-renderer type the dispatch table would
    have to enumerate. The reason string is assembled by `reason()` above, the
    same single spelling every other refusal in this stage uses.
    """

    def __init__(self, code: RefusalReason, detail: str):
        self.reason = code
        self.detail = detail
        super().__init__(reason(code, detail))


def reason_code(text: object) -> RefusalReason:
    """Read the closed-set code back off a published refusal reason.

    Refuses anything whose prefix is not a member: a refusal carrying a reason
    outside the closed set is exactly the free-text reason this spec replaced, and
    it must not pass a consumer's check because it happened to be a string.
    """
    if not isinstance(text, str) or ":" not in text:
        raise ContractError(f"refusal reason {text!r} does not open with a closed-set code")
    try:
        return RefusalReason(text.split(":", 1)[0])
    except ValueError:
        raise ContractError(
            f"refusal reason {text!r} names a code outside {[member.value for member in RefusalReason]}"
        ) from None


def load_format_policy(path: Path = DEFAULT_FORMAT_POLICY_PATH) -> dict[str, str]:
    """The admission list, read fresh from `config/admitted_formats.toml`.

    Never cached: the door reads it once per run and hands it down, so caching
    would only hide an edit made between two runs — and this file is the thing
    Tyrel's ledger rulings change, so reading what is currently on disk is the
    whole point.
    """
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise FormatPolicyRefusal(
            f"the admission list at {path} could not be read: {error}"
        ) from error

    table = document.get("format")
    if set(document) != {"format"} or not isinstance(table, dict):
        raise FormatPolicyRefusal(f"{path} is not a single [format] table of format-name rows")

    named = set(table)
    if named != SNIFFABLE_FORMATS:
        missing = sorted(SNIFFABLE_FORMATS - named)
        unknown = sorted(named - SNIFFABLE_FORMATS)
        raise FormatPolicyRefusal(
            f"{path} must name exactly the formats the door can detect. "
            f"Missing: {missing}. Unknown: {unknown}. A format with no row would be "
            "admitted or refused by omission, and a row nothing can detect is a rule "
            "no source will ever reach"
        )
    for format_name, action in sorted(table.items()):
        if action not in ACTIONS:
            raise FormatPolicyRefusal(
                f"{path} gives format {format_name!r} the action {action!r}, "
                f"which is not one of {sorted(ACTIONS)}"
            )
        if action in (ADMIT, ADMIT_OR_FAN_OUT) and format_name not in STRUCTURALLY_VALIDATED:
            raise FormatPolicyRefusal(
                f"{path} admits {format_name!r}, but image_formats.py has no structural "
                f"validator for it. Admitting a format nothing can verify would admit "
                "unverified bytes under a name that claims they were checked. Write the "
                "validator and its fixtures first, then change this line"
            )
        if action == RENDER_PAGES and format_name != "pdf":
            raise FormatPolicyRefusal(
                f"{path} asks for page rendering of {format_name!r}; the door renders "
                "pages unconditionally out of PDF alone, and no other renderer is wired "
                "that way — a format that is *usually* one image belongs to "
                f"{ADMIT_OR_FAN_OUT!r} instead"
            )
        if action == ADMIT_OR_FAN_OUT and format_name != "tiff":
            raise FormatPolicyRefusal(
                f"{path} asks {format_name!r} to admit-or-fan-out; only TIFF can "
                "structurally hold more than one image directory under one file, and "
                "the door has no other renderer wired for this action"
            )
    return dict(sorted(table.items()))


def classify_detected_format(detected: str | None, policy: dict[str, str]) -> str | RefusalReason:
    """The policy's verdict for a sniffed format: an action, or the reason to refuse.

    Shared by the raster path below and the door's container fan-out, so the
    admission surfaces this spec has read one table rather than copies that could
    drift. The return value is one of the four actions (`ADMIT`, `RENDER_PAGES`,
    `ADMIT_OR_FAN_OUT`, `GAP`) or an already-terminal `RefusalReason` — never a
    fifth, undocumented shape.
    """
    if detected is None:
        return RefusalReason.UNRECOGNIZED_FORMAT
    action = policy.get(detected)
    if action is None:
        # Unreachable while `load_format_policy` requires full coverage: every
        # sniffable format names a row. Kept as the fail-closed catch — a format
        # this table has no opinion on is a gap, never admitted by omission.
        return RefusalReason.UNSUPPORTED_VARIANT
    return action


def gap_detail(detected: str) -> str:
    """Why a `GAP`-action format refuses: a named pipeline defect, not his file.

    Ruling 2026-08-04, item 2: "if things are failing ... the pipeline is broken."
    A format nothing here decodes yet is exactly that — this project owes a reader
    for it — and the wording says so rather than framing it as a decision made
    about the specific file.
    """
    return (
        f"detected format is {detected}, and this project has no reader for it yet; "
        "that is a gap in the pipeline, not a decision about this file"
    )


def no_policy_opinion_detail(detected: str) -> str:
    """The dead-code safety net's own wording, kept distinct from `gap_detail`.

    Unreachable while `load_format_policy` enforces full coverage of
    `SNIFFABLE_FORMATS`; if it is ever reached, the admission list itself is the
    defect, not merely an unread format.
    """
    return (
        f"detected format is {detected}, and this project's admission list names no "
        "action for it at all"
    )


def inspect_source(
    data: bytes, *, declared_sha256: str | None, policy: dict[str, str]
) -> AdmissionOutcome:
    """Decide one already-read source by its bytes, before any run tree is touched.

    A `render-pages` format (PDF) is *not* decided here: a container of pages is
    not one image, and its admission is the door's per-page fan-out. `admit-or-fan-out`
    (TIFF) *is* decided here for the common case: a single-directory file is an
    ordinary raster and is admitted exactly like `admit`. Only a source the door has
    already found to hold more than one directory bypasses this function, because a
    multi-page TIFF is fanned out the same way a PDF is. This returns the action so
    the door can dispatch a container it must fan out, rather than deciding for it.
    """
    if not data:
        return AdmissionOutcome(
            "refused", reason(RefusalReason.EMPTY, "the source is empty"), None, None, None
        )
    if len(data) > MAX_SOURCE_BYTES:
        return AdmissionOutcome(
            "refused",
            reason(
                RefusalReason.TOO_LARGE,
                f"{len(data)} bytes exceeds the {MAX_SOURCE_BYTES}-byte admission limit",
            ),
            None,
            digest_bytes(data),
            None,
        )

    digest = digest_bytes(data)
    detected = sniff(data)
    verdict = classify_detected_format(detected, policy)
    if verdict is RefusalReason.UNRECOGNIZED_FORMAT:
        return AdmissionOutcome(
            "refused",
            reason(verdict, "bytes do not match any known image signature"),
            detected,
            digest,
            None,
        )
    if verdict is RefusalReason.UNSUPPORTED_VARIANT:
        return AdmissionOutcome(
            "refused",
            reason(verdict, no_policy_opinion_detail(detected)),
            detected,
            digest,
            None,
        )
    if verdict == GAP:
        return AdmissionOutcome(
            "refused",
            reason(RefusalReason.UNSUPPORTED_VARIANT, gap_detail(detected)),
            detected,
            digest,
            None,
        )
    if verdict == RENDER_PAGES:
        raise ValueError(
            f"{detected} is a multi-page container; the door fans it out rather than "
            "asking this function to admit it as one image"
        )

    # ADMIT and ADMIT_OR_FAN_OUT both reach here. For ADMIT_OR_FAN_OUT the
    # validator itself proves whether this is the common single-directory case
    # (admitted, exactly like ADMIT) or a multi-directory file that should have
    # been fanned out before it ever reached this function.
    try:
        geometry = validate(detected, data)
    except FormatRefusal as error:
        return AdmissionOutcome(
            "refused", reason(refusal_code_for_format_error(error), str(error)), detected, digest, None
        )

    if declared_sha256 is not None and digest != declared_sha256:
        return AdmissionOutcome(
            "refused",
            reason(
                RefusalReason.DIGEST_MISMATCH,
                f"computed {digest}, but {declared_sha256} was declared",
            ),
            detected,
            digest,
            None,
        )
    return AdmissionOutcome("admitted", None, detected, digest, (geometry.width, geometry.height))


def refusal_code_for_format_error(error: FormatRefusal) -> RefusalReason:
    """Corrupt bytes and an unhandled-but-legal variant are different facts.

    The validators say "unsupported ..." for a file that is a genuine instance of
    a variant this door does not decode, and "corrupt ..." for one that is not a
    genuine instance at all. Collapsing the two would tell Tyrel a real photograph
    was damaged when the truth is that we cannot read that flavour of it yet —
    a different decision for him entirely. Public rather than module-private
    because every door-private container renderer (`pdf_render.py`,
    `tiff_render.py`) reads a `FormatRefusal` back through the same one rule.
    """
    return (
        RefusalReason.UNSUPPORTED_VARIANT
        if str(error).startswith("unsupported")
        else RefusalReason.CORRUPT
    )


def duplicate_reason(first_ordinal: int) -> str:
    """The reason text for a source whose content was already admitted this run."""
    return reason(
        RefusalReason.DUPLICATE, f"identical content already admitted as source-{first_ordinal}"
    )
