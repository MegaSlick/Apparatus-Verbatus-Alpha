"""The production golden-page vision smoke callable.

The serving lifecycle proves that a completed request contains the exact local
fixture bytes.  This callable adds the semantic half of that proof: it asks the
chair for a page witness which is deliberately not present in the prompt, then
requires the exact witness known to be printed on the golden page.

**What the witness is, and whose job it is.**  Nothing here generates one, and
that is not an omission.  The witness only proves a page read because someone
rendered it into that page's pixels, so its entropy source, its lifetime and its
rotation belong to whoever authors the golden fixture; this callable can only
refuse a value that could not do the job whatever its origin.  The contract for
that caller: draw it from a CSPRNG over a large alphabet, mint a new one
whenever the page is re-rendered, and never carry one across fixtures.  This
callable cannot infer how a supplied string was generated.  A weak or
reused witness can be guessed or memorized and therefore can make the smoke
falsely green without a page read; the caller contract is a precondition of the
claim, not a property this class pretends to measure.

**One call at a time.**  :class:`~.manager.ServiceHandle` records its last
fixture request on itself and :class:`~.preflight.ServingSmokeReader`
corroborates the returned receipt against that record after this callable
returns, so a handle carries one smoke call at a time.  Two concurrent calls on
one handle would cross those records.  Nothing reaches this seam concurrently
today — ``ServingManager.start`` refuses a second start while a handle is active,
and ``PreflightRunner`` reads its chairs in sequence — so the precondition is
stated rather than locked: a lock inside the handle would not make the reader's
read-after-write atomic anyway, and adding one would advertise a concurrency
this seam does not support.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.chairs.models import ChairIdentity
from operations.pod.preflight import PlacementTier, SmokeResult, UtilizationSample

from .errors import ServingConfigurationError
from .manager import AdapterCalibration, ServiceHandle

_WITNESS_PREFIX = "PAGE-WITNESS: "
_MINIMUM_WITNESS_LENGTH = 32
_FIXTURE_MIME_TYPE = "image/png"
# Restated rather than imported: `common.imaging` owns the project's PNG codec
# but registers Pillow and libheif openers at import time, and this package has
# no other reason to pull an image stack into a serving request.
# `pipeline/1_exemplar/image_formats.py` restates it for its own layer already.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True, slots=True)
class VisionSmokeCall:
    """Read one golden page and verify its page-only witness.

    ``page_witness`` must be an unguessable value rendered on the selected
    golden page.  It is never interpolated into :attr:`prompt`: otherwise a
    text-only answer copied from the prompt could appear to prove page reading.
    """

    page_witness: str
    utilization: Callable[[], tuple[UtilizationSample, ...]] = lambda: ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page_witness, str)
            or len(self.page_witness) < _MINIMUM_WITNESS_LENGTH
            or not self.page_witness.strip()
        ):
            raise ValueError(
                "golden-page witness must be a non-blank unguessable string of at least "
                f"{_MINIMUM_WITNESS_LENGTH} characters"
            )
        # A page witness is a token, not prose.  Whitespace makes its visible
        # boundary ambiguous, and line-breaking whitespace contradicts the one
        # output line the prompt requires.  Refuse that fixture-authoring defect
        # before it can be blamed on a chair as `smoke-output-invalid`.
        if any(character.isspace() for character in self.page_witness):
            raise ValueError(
                "golden-page witness must contain no whitespace: it is a visible token "
                "returned on one exact output line"
            )
        # The class contract above is that the witness never reaches the model
        # in text.  `prompt` is a constant today, so this holds by inspection —
        # which is exactly why it is worth asserting: an edit or a subclass that
        # interpolated the witness would leave every other check here passing.
        if self.page_witness in self.prompt:
            raise ValueError(
                "golden-page witness occurs in the smoke prompt, so a text-only answer "
                "copied from the prompt would satisfy the page-read check"
            )
        if not callable(self.utilization):
            raise ValueError("golden-page utilization sampler must be callable")

    @property
    def prompt(self) -> str:
        """The page-reading instruction, intentionally without the expected witness."""

        return (
            "Read the supplied page image. Reply with exactly one line in this format: "
            "PAGE-WITNESS: <the page witness string>. Do not add explanation, markup, "
            "or any other text."
        )

    def __call__(
        self,
        handle: ServiceHandle,
        identity: ChairIdentity,
        fixture: Path,
        placement: PlacementTier,
    ) -> SmokeResult:
        """Submit one fixture-bound vision request and return measurable smoke facts."""

        del placement  # Placement is selected and checked by ServingSmokeReader.
        if not isinstance(handle, ServiceHandle):
            raise ServingConfigurationError(
                "vision smoke requires a ServiceHandle started by ServingManager"
            )
        if handle.identity != identity:
            raise ServingConfigurationError(
                "vision smoke handle identity differs from the resolved chair identity"
            )

        calibration = AdapterCalibration.from_image_fixture(
            fixture=fixture,
            prompt=self.prompt,
            mime_type=_FIXTURE_MIME_TYPE,
        )
        _require_declared_format(fixture)
        payload = calibration.request_payload()
        answer = handle.request_fixture_image("chat-completions", payload, fixture=fixture)

        expected = _WITNESS_PREFIX + self.page_witness
        shape_valid = len(answer.outputs) == 1
        # `nonempty` is structurally equal to `shape_valid` here, and is kept
        # because `SmokeResult` requires the field, not because it adds a
        # measurement: `parse_openai_answer` already refuses any choice whose
        # content is blank, so a blank answer never reaches this line — it
        # arrives at the runner as `smoke-read-failed`, earlier and louder.
        nonempty = shape_valid and bool(answer.outputs[0].strip())
        # The prompt asks for exactly one line.  Stripping here would silently
        # accept surrounding spaces or extra blank lines and report them as
        # format-valid, a broader claim than the output rule actually measured.
        format_valid = shape_valid and answer.outputs[0] == expected
        samples = self.utilization()
        if not isinstance(samples, tuple) or not all(
            isinstance(sample, UtilizationSample) for sample in samples
        ):
            raise ServingConfigurationError(
                "golden-page utilization sampler must return a tuple of UtilizationSample values"
            )
        return SmokeResult(
            shape_valid=shape_valid,
            nonempty=nonempty,
            format_valid=format_valid,
            receipt={
                "fixture": fixture.name,
                "fixture_response_sha256": answer.response_sha256,
                "resolved_identity": identity.to_record(),
                "resolved_revision": identity.receipt_revision,
                "resolved_revision_kind": identity.receipt_revision_kind,
                "served_model_id": answer.model_id,
                "page_witness_sha256": hashlib.sha256(self.page_witness.encode()).hexdigest(),
                "page_witness_matches": format_valid,
            },
            utilization=samples,
        )


def _require_declared_format(fixture: Path) -> None:
    """Refuse a golden page whose bytes are not the format the request declares.

    The request travels as ``data:image/png;base64,...``.  Nothing else on this
    path inspects those bytes for format — ``AdapterCalibration`` binds their
    digest, and ``ServingSmokeReader`` re-hashes them — and the runner's fixture
    path is a free caller seam (``PreflightRunner`` takes any ``Path``).  A JPEG
    or TIFF golden page would therefore be sent declared as a PNG, false in the
    request and in the launch audit that digests it.  The signature is what the
    declaration is about; the filename suffix is not.
    """

    try:
        with fixture.open("rb") as stream:
            signature = stream.read(len(_PNG_SIGNATURE))
    except OSError as error:
        raise ServingConfigurationError(
            f"cannot read golden-page fixture {fixture}: {error}"
        ) from error
    if signature != _PNG_SIGNATURE:
        raise ServingConfigurationError(
            f"golden-page fixture {fixture.name} is not a PNG, but the vision smoke request "
            f"declares {_FIXTURE_MIME_TYPE}; supply a PNG page, or a smoke callable that "
            "declares the format it actually sends"
        )
