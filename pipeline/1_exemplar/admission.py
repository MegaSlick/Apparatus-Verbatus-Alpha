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

from image_formats import MAX_SOURCE_BYTES, FormatRefusal, sniff, validate

from common.contracts.canonical import digest_bytes
from common.contracts.errors import ContractError

DEFAULT_FORMAT_POLICY_PATH: Final = (
    Path(__file__).resolve().parents[2] / "config" / "admitted_formats.toml"
)

# The three things the policy may say about a format. A fourth word in the file is
# refused at load rather than treated as "not admit".
ADMIT: Final = "admit"
RENDER_PAGES: Final = "render-pages"
REFUSE: Final = "refuse"
ACTIONS: Final = frozenset({ADMIT, RENDER_PAGES, REFUSE})

# Every format name `image_formats.sniff()` can return. The policy must cover this
# set exactly: a format missing from the file could otherwise be admitted by
# omission, and a format named in the file that nothing can sniff is a rule nobody
# will ever reach — the silent drift this module exists to prevent.
SNIFFABLE_FORMATS: Final = frozenset({"png", "jpeg", "tiff", "pdf", "gif", "heic"})

# The formats a structural validator exists for. `render-pages` formats are not
# here: a container is proved by rendering it, not by a raster validator.
STRUCTURALLY_VALIDATED: Final = frozenset({"png", "jpeg", "tiff"})


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
    REFUSED_FORMAT = "refused-format"
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
        if action == ADMIT and format_name not in STRUCTURALLY_VALIDATED:
            raise FormatPolicyRefusal(
                f"{path} admits {format_name!r}, but image_formats.py has no structural "
                f"validator for it. Admitting a format nothing can verify would admit "
                "unverified bytes under a name that claims they were checked. Write the "
                "validator and its fixtures first, then change this line"
            )
        if action == RENDER_PAGES and format_name != "pdf":
            raise FormatPolicyRefusal(
                f"{path} asks for page rendering of {format_name!r}; the door renders "
                "pages out of PDF alone, and no other renderer exists to call"
            )
    return dict(sorted(table.items()))


def classify_detected_format(detected: str | None, policy: dict[str, str]) -> str | RefusalReason:
    """The policy's verdict for a sniffed format: an action, or the reason to refuse.

    Shared by the raster path below and the door's PDF fan-out, so the two admission
    surfaces this spec has read one table rather than two copies that could drift.
    """
    if detected is None:
        return RefusalReason.UNRECOGNIZED_FORMAT
    action = policy.get(detected)
    if action is None or action == REFUSE:
        # `None` is unreachable while `load_format_policy` requires full coverage,
        # and it is kept as the fail-closed catch: a format this table has no
        # opinion on is refused, never admitted by omission.
        return RefusalReason.REFUSED_FORMAT
    return action


def refused_format_detail(detected: str) -> str:
    return f"detected format is {detected}, which this project's admission list refuses by name"


def inspect_source(
    data: bytes, *, declared_sha256: str | None, policy: dict[str, str]
) -> AdmissionOutcome:
    """Decide one already-read source by its bytes, before any run tree is touched.

    A `render-pages` format (PDF) is *not* decided here: a container of pages is
    not one image, and its admission is the door's per-page fan-out. This returns
    the action so the door can dispatch, rather than deciding for it.
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
    if verdict is RefusalReason.REFUSED_FORMAT:
        return AdmissionOutcome(
            "refused", reason(verdict, refused_format_detail(detected)), detected, digest, None
        )
    if verdict == RENDER_PAGES:
        raise ValueError(
            f"{detected} is a multi-page container; the door fans it out rather than "
            "asking this function to admit it as one image"
        )

    try:
        geometry = validate(detected, data)
    except FormatRefusal as error:
        return AdmissionOutcome(
            "refused", reason(_refusal_code(error), str(error)), detected, digest, None
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


def _refusal_code(error: FormatRefusal) -> RefusalReason:
    """Corrupt bytes and an unhandled-but-legal variant are different facts.

    The validators say "unsupported ..." for a file that is a genuine instance of
    a variant this door does not decode, and "corrupt ..." for one that is not a
    genuine instance at all. Collapsing the two would tell Tyrel a real photograph
    was damaged when the truth is that we cannot read that flavour of it yet —
    a different decision for him entirely.
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
