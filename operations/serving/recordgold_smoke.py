"""The DAI chair's smoke page: one pinned RecordGold record instead of the golden page.

DAI (``Teklia/Qwen2.5-VL-7B-DAI-CReTDHI-RecordGold-ATR``, witness adapter
``dai.v1``) is a handwriting record reader trained on RecordGold. Asked for the
golden page's typed random code it slips three or four characters about half the
time, or answers a non-ASCII look-alike, while it reads real records well. Its
smoke therefore reads one record of the public corpus it was trained on and is
scored by character error rate against that record's expert transcription. Every
other chair keeps the golden page.

The repository holds the record's identity and digests only: the dataset and the
revision the pins were taken from, the record id, its IIIF crop URL, the SHA-256
of the crop bytes as the IIIF server serves them, and the SHA-256 and length of
the transcription. The pod fetches crop and transcription at preflight, checks
both against these pins, and refuses by name when either differs or cannot be
fetched -- a network failure is named as one and is never read as a model
failure. Neither the crop nor the transcription is written into the repository,
a receipt or a report; the receipt carries digests and the measured rate.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from common.chairs.models import ChairIdentity, ModelsConfig
from operations.corpus.normalization import GRAPHEMIC_V1, MeasurementRefusal
from operations.corpus.scoring import score_text

from .errors import ServingConfigurationError

DAI_WITNESS_ADAPTER = "dai.v1"
# The pass line, as a reference-length CER after `graphemic-v1` normalisation.
# Teklia's own card reports CER 9.24 on its DAI test split; a record the model
# was trained on should read well under that, while 0.15 still fails a reader
# that mangles one word in six -- the symptom a smoke exists to catch -- and
# tolerates the handful of abbreviation and accent slips a short record invites.
RECORDGOLD_SMOKE_MAX_CER = Decimal("0.15")
RECORDGOLD_SMOKE_PROFILE = GRAPHEMIC_V1
_CER_PLACES = Decimal("0.0001")
_FETCH_TIMEOUT_SECONDS = 60.0
_MAXIMUM_FETCH_BYTES = 8 * 1024 * 1024
_DATASETS_SERVER = "https://datasets-server.huggingface.co/rows"

RECORDGOLD_SMOKE_REFUSAL_REASONS = frozenset(
    {
        "recordgold-smoke-fetch-failed",
        "recordgold-smoke-image-mismatch",
        "recordgold-smoke-text-mismatch",
        "recordgold-smoke-row-malformed",
    }
)


class RecordGoldSmokeRefusal(ServingConfigurationError):
    """A named refusal, `"<reason>: <detail>"`, raised before any chair reads the record."""

    code = "RECORDGOLD_SMOKE_REFUSAL"

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in RECORDGOLD_SMOKE_REFUSAL_REASONS:
            raise TypeError(f"recordgold smoke declares no reason {reason!r}")
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True, slots=True)
class RecordGoldSmokeRecord:
    """One RecordGold record pinned by identity and digests, never by content.

    ``revision`` is the dataset commit the pins were read from. The rows
    endpoint serves the dataset's current revision, so the transcription is
    pinned by ``record_id``, ``text_sha256`` and ``text_length`` rather than by
    position alone; ``row_index`` only says where to ask.
    """

    dataset: str
    revision: str
    licence: str
    split: str
    row_index: int
    record_id: str
    record_url: str
    source: str
    parish: str
    start_date: int
    image_sha256: str
    image_width: int
    image_height: int
    text_sha256: str
    text_length: int

    @property
    def rows_url(self) -> str:
        query = urllib.parse.urlencode(
            {
                "dataset": self.dataset,
                "config": "default",
                "split": self.split,
                "offset": self.row_index,
                "length": 1,
            }
        )
        return f"{_DATASETS_SERVER}?{query}"

    def to_record(self) -> dict[str, object]:
        return asdict(self)


RECORDGOLD_SMOKE_RECORD = RecordGoldSmokeRecord(
    dataset="Teklia/DAI-CReTDHI-RecordGold-ATR",
    revision="52bcfdd3e1aa7c326982aeda1b7f83104fc98710",
    licence="MIT (dataset card at that revision)",
    split="train",
    row_index=708,
    record_id="e8edfc9e-69f7-4a65-a24b-b150650ddbbd",
    record_url=(
        "https://europe.iiif.teklia.com/iiif/2/geneanet%2FArdennes_BMS%2F383255%2F00041.jpg/"
        "174,1647,1176,248/full/0/default.jpg"
    ),
    source="Ardennes",
    parish="Braux",
    start_date=1771,
    image_sha256="2b49c9dfc2203896e8ed96c206f5860305aab5d6cf2ffe861e60ba499ee0b013",
    image_width=1176,
    image_height=248,
    text_sha256="b4ae2dbdcbc1a2587525d1a08dcead3cb38b4604fb16ddd21b176d05c51fee18",
    text_length=79,
)


@dataclass(frozen=True, slots=True)
class RecordGoldSmokePage:
    """The fetched and verified record: the crop re-encoded as PNG, and its gold text."""

    png: bytes
    text: str


@dataclass(frozen=True, slots=True)
class RecordGoldSmokeScore:
    """One answer measured against the gold transcription."""

    character_error_rate: Decimal
    edits: int
    reference_units: int

    @property
    def passed(self) -> bool:
        return self.character_error_rate <= RECORDGOLD_SMOKE_MAX_CER


def recordgold_smoke_chairs(models: ModelsConfig) -> frozenset[str]:
    """The configured roles whose witness adapter is DAI's."""

    return frozenset(
        role
        for role, chair in models.chairs.items()
        if isinstance(chair, ChairIdentity) and chair.witness_adapter == DAI_WITNESS_ADAPTER
    )


def recordgold_smoke_prompt() -> tuple[str, str]:
    """DAI's own ``system.txt`` and ``query.txt``, so the smoke asks what the run asks.

    The same two strings `pipeline/3_attestatores/feeding.dai_prompt` carries from
    Teklia's repository; `test_recordgold_smoke.py` pins them equal.
    """

    return (
        "Tu es un assistant archiviste. Tu dois lire des actes issus de registres "
        "paroissiaux français, du 16è au 18è siècle. Extrais le texte de la marge, du "
        "corps de l'acte, et éventuellement les signatures.\n",
        "Extrais le texte de ce document.\n",
    )


def fetch_public_bytes(url: str) -> bytes:
    """One bounded GET of a public URL; every failure is a fetch refusal naming the URL."""

    request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            if int(response.status) != 200:
                raise RecordGoldSmokeRefusal(
                    "recordgold-smoke-fetch-failed", f"{url} answered HTTP {response.status}"
                )
            data = response.read(_MAXIMUM_FETCH_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-fetch-failed", f"{url} answered HTTP {error.code}"
        ) from error
    except (OSError, ValueError) as error:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-fetch-failed", f"{url}: {type(error).__name__}: {error}"
        ) from error
    if len(data) > _MAXIMUM_FETCH_BYTES:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-fetch-failed",
            f"{url} returned more than {_MAXIMUM_FETCH_BYTES} bytes",
        )
    return data


def verify_recordgold_text(text: object, record: RecordGoldSmokeRecord) -> str:
    """Return ``text`` when it is exactly the pinned transcription, else refuse by name."""

    if not isinstance(text, str):
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-text-mismatch",
            f"record {record.record_id} transcription is not a string",
        )
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if len(text) != record.text_length or digest != record.text_sha256:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-text-mismatch",
            f"record {record.record_id} transcription has {len(text)} characters and digest "
            f"{digest}; the pin says {record.text_length} and {record.text_sha256}",
        )
    return text


def _verify_image(data: bytes, record: RecordGoldSmokeRecord) -> bytes:
    """Check the fetched crop against its pin and re-encode it losslessly as PNG."""

    digest = hashlib.sha256(data).hexdigest()
    if digest != record.image_sha256:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-image-mismatch",
            f"{record.record_url} served {len(data)} bytes with digest {digest}; the pin "
            f"says {record.image_sha256}",
        )
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            if image.size != (record.image_width, record.image_height):
                raise RecordGoldSmokeRefusal(
                    "recordgold-smoke-image-mismatch",
                    f"{record.record_url} decodes to {image.size}, the pin says "
                    f"{(record.image_width, record.image_height)}",
                )
            encoded = BytesIO()
            image.save(encoded, format="PNG")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-image-mismatch",
            f"{record.record_url} served bytes the decoder cannot read: {error}",
        ) from error
    return encoded.getvalue()


def _row_text(body: bytes, record: RecordGoldSmokeRecord) -> str:
    """The transcription out of one datasets-server rows answer, checked to be this record's."""

    try:
        parsed = json.loads(body.decode("utf-8"))
        rows = parsed["rows"]
        if len(rows) != 1:
            raise ValueError(f"{len(rows)} rows answered where one was asked")
        row = rows[0]["row"]
        record_id = row["record_id"]
        text = row["text"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, IndexError) as error:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-row-malformed",
            f"{record.rows_url}: {type(error).__name__}: {error}",
        ) from error
    if record_id != record.record_id:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-row-malformed",
            f"{record.rows_url} answered record {record_id!r} at row {record.row_index}, "
            f"not the pinned {record.record_id!r}; the dataset has moved since the pin was taken",
        )
    return verify_recordgold_text(text, record)


COMMITTED_ROOT = Path(__file__).resolve().parents[2] / "proof" / "fixtures" / "recordgold-smoke-v0"


def committed_recordgold_bytes(
    url: str, record: RecordGoldSmokeRecord = RECORDGOLD_SMOKE_RECORD
) -> bytes:
    """Answer the two pinned URLs from the copies committed beside the code.

    This one public record is committed so a pod's preflight needs no network
    for it. The bytes are those the IIIF server
    and the datasets server served when the pins were taken; the same digest
    checks as a live fetch run on them, so a changed file is refused by name.
    """

    try:
        if url == record.record_url:
            return (COMMITTED_ROOT / f"{record.record_id}.jpg").read_bytes()
        if url == record.rows_url:
            text = (COMMITTED_ROOT / f"{record.record_id}.txt").read_text(encoding="utf-8")
            row = {"record_id": record.record_id, "text": text}
            return json.dumps({"rows": [{"row": row}]}).encode("utf-8")
    except OSError as error:
        raise RecordGoldSmokeRefusal(
            "recordgold-smoke-fetch-failed", f"{url}: committed copy unreadable: {error}"
        ) from error
    raise RecordGoldSmokeRefusal(
        "recordgold-smoke-fetch-failed", f"{url}: no committed copy for this URL"
    )


def fetch_recordgold_smoke_page(
    record: RecordGoldSmokeRecord = RECORDGOLD_SMOKE_RECORD,
    *,
    fetch: Callable[[str], bytes] = fetch_public_bytes,
) -> RecordGoldSmokePage:
    """Fetch the pinned crop and transcription and verify both, or refuse by name."""

    png = _verify_image(fetch(record.record_url), record)
    text = _row_text(fetch(record.rows_url), record)
    return RecordGoldSmokePage(png=png, text=text)


def score_recordgold_answer(answer: str, gold: str) -> RecordGoldSmokeScore | None:
    """Measure one answer's CER against the gold, or None when it cannot be measured.

    Both strings go through the corpus's own `graphemic-v1` normalisation, so
    the smoke scores the way the evaluation does. An answer the profile refuses
    (past its text bounds) is unmeasurable, which the caller treats as a failed
    format check rather than a pass.
    """

    try:
        scored = score_text(gold, answer, profile=RECORDGOLD_SMOKE_PROFILE)
    except MeasurementRefusal:
        return None
    rate = Decimal(scored.cer.edits.errors) / Decimal(scored.cer.reference_units)
    return RecordGoldSmokeScore(
        character_error_rate=rate.quantize(_CER_PLACES, rounding=ROUND_HALF_UP),
        edits=scored.cer.edits.errors,
        reference_units=scored.cer.reference_units,
    )
