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

The pod's own golden page.  ``operations/pod/bootstrap_main.py`` is the one
production caller of this callable, and it has no fixture author standing by:
``fresh_page_witness`` draws the witness from the CSPRNG and
``render_golden_page`` puts it into pixels, once per preflight, so the value a
chair must read back was never in a committed file, a prompt, or an earlier
run's report.  ``NvidiaSmiUtilization`` is the sampler that same caller wires:
one ``nvidia-smi`` read after the answer, and the process's own load average
for the CPU figure.  A sampler that cannot measure returns no samples, which
``PreflightRunner`` turns into ``utilization-missing`` -- an empty instrument
is red, never a quiet green.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from common.chairs.models import ChairIdentity
from operations.pod.preflight import PlacementTier, SmokeResult, UtilizationSample

from .errors import ServingConfigurationError
from .manager import AdapterCalibration, ServiceHandle, _active_chat_image_bytes

_WITNESS_PREFIX = "PAGE-WITNESS: "
_MINIMUM_WITNESS_LENGTH = 32
_MAXIMUM_WITNESS_LENGTH = 128
_MAXIMUM_UTILIZATION_SAMPLES = 1_024
_MAXIMUM_PNG_BYTES = 64 * 1024 * 1024
_FIXTURE_MIME_TYPE = "image/png"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 32 bytes of CSPRNG output, URL-safe: 43 characters, all inside the witness
# alphabet `VisionSmokeCall` accepts and comfortably inside its length bound.
_WITNESS_ENTROPY_BYTES = 32
# The rendered page. Large type on a wide page, so the witness is a line of
# text a vision model reads rather than a strip of bitmap glyphs, and the whole
# page still sits under the smallest tier's longest-edge cap (1344 pixels,
# config/pod_placement.toml) so `_verify_png` never refuses it.
_GOLDEN_PAGE_SIZE = (1280, 400)
_GOLDEN_PAGE_FONT_SIZE = 40
_NVIDIA_SMI_TIMEOUT_SECONDS = 30.0


def fresh_page_witness() -> str:
    """One unguessable witness for one golden page, from the CSPRNG.

    ``VisionSmokeCall`` states that entropy and rotation are the fixture
    author's job; on the pod that author is this function, called once per
    preflight by ``bootstrap_main`` immediately before the page is rendered.
    """

    return secrets.token_urlsafe(_WITNESS_ENTROPY_BYTES)


def render_golden_page(path: Path, witness: str) -> bytes:
    """Put ``PAGE-WITNESS: <witness>`` into the pixels of a fresh PNG at ``path``.

    The witness is validated the way the callable validates it, before any
    pixel is drawn, so a page cannot be rendered for a value the smoke would
    later refuse. The rendered bytes are decoded again before being returned:
    a page the decoder cannot read would reach the chair as a request it could
    only fail, and this is where that would be found.
    """

    VisionSmokeCall(witness)
    page = Image.new("L", _GOLDEN_PAGE_SIZE, color="white")
    font = ImageFont.load_default(size=_GOLDEN_PAGE_FONT_SIZE)
    ImageDraw.Draw(page).text(
        (48, 160),
        f"{_WITNESS_PREFIX}{witness}",
        fill="black",
        font=font,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    page.save(path, format="PNG")
    encoded = path.read_bytes()
    _verify_png(encoded, max_pixels=_GOLDEN_PAGE_SIZE[0] * _GOLDEN_PAGE_SIZE[1])
    return encoded


class NvidiaSmiUtilization:
    """The production utilization sampler ``bootstrap_main`` hands ``VisionSmokeCall``.

    One sample per call: ``utilization.gpu`` from ``nvidia-smi`` and the
    one-minute load average per CPU for the CPU figure. Both are measurements
    taken *after* the smoke answer, so they say what the card and the host were
    doing around the read, and nothing more -- no threshold here claims a card
    is saturated. A failed or unparseable read returns an empty tuple, which
    ``PreflightRunner`` reports as ``utilization-missing``.
    """

    def __init__(
        self,
        *,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        load_average: Callable[[], tuple[float, float, float]] | None = None,
        cpu_count: Callable[[], int | None] | None = None,
    ) -> None:
        self.runner = runner or self._run
        self.load_average = load_average or os.getloadavg
        self.cpu_count = cpu_count or os.cpu_count

    def __call__(self) -> tuple[UtilizationSample, ...]:
        try:
            query = self.runner(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"]
            )
            if query.returncode != 0:
                return ()
            gpu_percent = Decimal(query.stdout.strip().splitlines()[0].strip())
            cpus = self.cpu_count() or 0
            if cpus <= 0:
                return ()
            load = Decimal(str(self.load_average()[0])) / Decimal(cpus) * Decimal(100)
            cpu_percent = min(load, Decimal(100)).quantize(Decimal("0.1"))
            return (UtilizationSample(gpu_percent, cpu_percent),)
        except (OSError, ValueError, IndexError, InvalidOperation, subprocess.SubprocessError):
            return ()

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            check=False,
            timeout=_NVIDIA_SMI_TIMEOUT_SECONDS,
        )


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
            or len(self.page_witness) > _MAXIMUM_WITNESS_LENGTH
            or not self.page_witness.strip()
        ):
            raise ServingConfigurationError(
                "golden-page witness must be a non-blank string between "
                f"{_MINIMUM_WITNESS_LENGTH} and {_MAXIMUM_WITNESS_LENGTH} characters"
            )
        # Whitespace makes a token's visible boundary ambiguous and can violate
        # the one-line output contract; reject it before chair inference so a
        # fixture defect cannot be reported as `smoke-output-invalid`.
        if any(character.isspace() for character in self.page_witness):
            raise ServingConfigurationError(
                "golden-page witness must contain no whitespace: it is a visible token "
                "returned on one exact output line"
            )
        if not self.page_witness.isascii() or not all(
            character.isalnum() or character in "-_" for character in self.page_witness
        ):
            raise ServingConfigurationError(
                "golden-page witness must use only visible URL-safe ASCII letters, digits, "
                "hyphen, or underscore"
            )
        # A subclass can override `prompt`; keep the page-only claim enforced at
        # construction even though the base prompt is constant.
        if self.page_witness in self.prompt:
            raise ServingConfigurationError(
                "golden-page witness occurs in the smoke prompt, so a text-only answer "
                "copied from the prompt would satisfy the page-read check"
            )
        if not callable(self.utilization):
            raise ServingConfigurationError("golden-page utilization sampler must be callable")

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
        image_bytes = _active_chat_image_bytes(payload, label="golden-page request")
        _verify_png(image_bytes, max_pixels=placement.recipe.pixel_cap**2)
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
        if len(samples) > _MAXIMUM_UTILIZATION_SAMPLES:
            raise ServingConfigurationError(
                "golden-page utilization sampler returned more than "
                f"{_MAXIMUM_UTILIZATION_SAMPLES} samples for one smoke request"
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


def _verify_png(data: bytes, *, max_pixels: int) -> None:
    """Refuse malformed or amplified bytes before the vision decoder receives them."""

    if len(data) > _MAXIMUM_PNG_BYTES:
        raise ServingConfigurationError(
            f"golden-page PNG exceeds the {_MAXIMUM_PNG_BYTES}-byte smoke request bound"
        )
    if not data.startswith(_PNG_SIGNATURE):
        raise ServingConfigurationError(
            "golden-page request bytes are not a PNG, but the vision smoke request declares "
            f"{_FIXTURE_MIME_TYPE}"
        )
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG":
                raise ValueError("decoder did not identify PNG")
            width, height = image.size
            image.verify()
        if width * height > max_pixels:
            raise ServingConfigurationError(
                f"golden-page PNG has {width * height} pixels, past the measured placement's "
                f"{max_pixels}-pixel smoke bound"
            )
        # ``verify`` checks container integrity without decoding pixels. Reopen and
        # load under the geometry bound so a corrupt compressed stream cannot cross
        # into the child merely because its signature and chunk CRCs looked valid.
        with Image.open(BytesIO(data)) as image:
            image.load()
    except ServingConfigurationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
    ) as error:
        raise ServingConfigurationError(
            "golden-page request bytes are not a complete decodable PNG"
        ) from error
