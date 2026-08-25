"""The production golden-page vision smoke callable.

The lifecycle proves that a request carried the exact fixture bytes; the
page-only witness proves that the chair read those bytes.  The fixture author
must draw a fresh witness from a CSPRNG over the URL-safe ASCII token alphabet
whenever the page is rendered.  Generation quality cannot be inferred from the
supplied value, so a weak or reused witness can make the smoke falsely green.

One handle supports one smoke call at a time.  The handle stores its latest
fixture request and :class:`~.preflight.ServingSmokeReader` corroborates that
state after this call returns; concurrent calls would cross those records.
``ServingManager.start`` and ``PreflightRunner`` currently enforce sequential
use outside this class.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.chairs.models import ChairIdentity
from operations.pod.preflight import PlacementTier, SmokeResult, UtilizationSample

from .errors import ServingConfigurationError
from .manager import AdapterCalibration, ServiceHandle, _active_chat_image_bytes

_WITNESS_PREFIX = "PAGE-WITNESS: "
_MINIMUM_WITNESS_LENGTH = 32
_FIXTURE_MIME_TYPE = "image/png"
# Importing the project's image codec registers Pillow and libheif openers;
# signature validation at this boundary does not require those global side effects.
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
                "golden-page witness must be a non-blank string of at least "
                f"{_MINIMUM_WITNESS_LENGTH} characters"
            )
        # Whitespace makes a token's visible boundary ambiguous and can violate
        # the one-line output contract; reject it before chair inference so a
        # fixture defect cannot be reported as `smoke-output-invalid`.
        if any(character.isspace() for character in self.page_witness):
            raise ValueError(
                "golden-page witness must contain no whitespace: it is a visible token "
                "returned on one exact output line"
            )
        if not self.page_witness.isascii() or not all(
            character.isalnum() or character in "-_" for character in self.page_witness
        ):
            raise ValueError(
                "golden-page witness must use only visible URL-safe ASCII letters, digits, "
                "hyphen, or underscore"
            )
        # A subclass can override `prompt`; keep the page-only claim enforced at
        # construction even though the base prompt is constant.
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
        """Send the sealed PNG through the fixture-bound handle, never a plain request."""

        # The SmokeCall protocol supplies placement; ServingSmokeReader checks it
        # before constructing the owned handle passed here.
        del placement
        if not isinstance(handle, ServiceHandle):
            raise ServingConfigurationError(
                "vision smoke requires a ServiceHandle started by ServingManager"
            )
        if handle.identity != identity:
            raise ServingConfigurationError(
                "vision smoke handle identity differs from the resolved chair identity"
            )

        payload = AdapterCalibration.from_image_fixture(
            fixture=fixture,
            prompt=self.prompt,
            mime_type=_FIXTURE_MIME_TYPE,
        ).request_payload()
        # Inspect the sealed payload, not the path: reopening the fixture could
        # validate replacement bytes rather than the snapshot about to be sent.
        if not _active_chat_image_bytes(payload, label="golden-page request").startswith(
            _PNG_SIGNATURE
        ):
            raise ServingConfigurationError(
                f"golden-page fixture {fixture.name} is not a PNG, but the vision smoke request "
                f"declares {_FIXTURE_MIME_TYPE}; supply a PNG page, or a smoke callable that "
                "declares the format it actually sends"
            )
        answer = handle.request_fixture_image("chat-completions", payload, fixture=fixture)

        shape_valid = len(answer.outputs) == 1
        # Keep this independent of shape: multiple parsed choices are still
        # nonempty, even though they fail the one-output and exact-format rules.
        # `parse_openai_answer` already refuses a blank choice, so a blank
        # answer never reaches this line — it arrives at the runner as
        # `smoke-read-failed`, earlier and louder.
        nonempty = bool(answer.outputs) and all(output.strip() for output in answer.outputs)
        # The prompt asks for exactly one line.  Stripping here would silently
        # accept surrounding spaces or extra blank lines and report them as
        # format-valid, a broader claim than the output rule actually measured.
        format_valid = answer.outputs == (_WITNESS_PREFIX + self.page_witness,)
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
