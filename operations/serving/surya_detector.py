"""Surya's text lines and layout blocks, as the Designator receives them.

Surya runs apart, in its own pinned environment (`operations/serving/surya/`),
because it pins Pillow and OpenCV versions this environment cannot share. Two
detectors answer the same call: that environment's runner, started as a child
process on the CPU, and a fixture that answers from the synthetic fixture's
declared rows. Both give one page document per page in the runner's shape, and
every document is checked here against that closed shape, refused by name if
anything differs, before the Designator reads a value from it.

A detection is returned exactly as Surya gave it: float polygons, float
confidences, labels and reading-order positions, with the ordering those
positions came from. Turning that into integer page geometry is the
Designator's declared quantization, not this module's.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from common.chairs.models import ChairIdentity, ServingDetails

from .config import SubprocessProfile
from .errors import ServingConfigurationError, ServingError

REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_PATH = REPO_ROOT / "operations" / "serving" / "surya" / "contract.py"


def _load_contract():
    """The runner's own stdlib contract module, loaded by path: it lives in Surya's
    environment folder, which is not a package of this one."""
    name = "verbatus_surya_contract"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _CONTRACT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contract = _load_contract()
PAGE_SCHEMA = contract.PAGE_SCHEMA
FIXTURE_ENGINE = "fixture"
RUNNER = "runner.py"

_PAGE_FIELDS = {
    "schema",
    "input_ordinal",
    "image_size",
    "run",
    "text_detection",
    "layout",
    "reading_order",
    "reading_order_reason",
}
_LINES_FIELDS = {"bboxes", "image_bbox"}
_LINE_FIELDS = {"polygon", "confidence", "bbox"}
_LAYOUT_FIELDS = {"bboxes", "image_bbox", "raw", "error"}
_BLOCK_FIELDS = _LINE_FIELDS | {"label", "raw_label", "position", "count"}
_SURYA_RUN_FIELDS = {
    "engine",
    "surya_ocr",
    "torch",
    "python",
    "device",
    "cpu_capability",
    "machine",
    "threads",
    "deterministic_algorithms",
    "settings",
    "checkpoints",
    "weights",
}
_FIXTURE_RUN_FIELDS = {"engine", "declared_by"}
_FIXTURE_DECLARATION = "proof/skeleton_fixture.toml"


class SuryaOutputRefusal(ServingConfigurationError):
    """A Surya page document that is not the shape Surya's own schema gives."""


class SuryaRunFailure(ServingError):
    """Surya's runner could not be started, did not finish in time, or failed."""

    code = "SURYA_RUN_FAILED"


@dataclass(frozen=True, slots=True)
class SuryaPage:
    """One page's document: the bytes as the detector wrote them, and their parse."""

    raw: bytes
    document: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SuryaRun:
    """What ran, the serving facts its receipt records, and each page's document."""

    run_facts: Mapping[str, Any]
    serving_details: ServingDetails
    pages: Mapping[int, SuryaPage]


# --- the closed shape --------------------------------------------------------


def _refuse(path: str, what: str) -> SuryaOutputRefusal:
    return SuryaOutputRefusal(f"Surya page document at {path} {what}")


def _closed(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _refuse(path, "is not an object")
    unknown, missing = sorted(set(value) - fields), sorted(fields - set(value))
    if unknown or missing:
        raise _refuse(path, f"has unknown field(s) {unknown} or missing field(s) {missing}")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _refuse(path, "is not a finite number")
    return float(value)


def _count(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _refuse(path, "is not a non-negative integer")
    return value


def _polygon_box(item: dict[str, Any], path: str) -> None:
    """Four (x, y) corners, and the bbox Surya derives from them, as Surya derives it."""
    polygon = item["polygon"]
    if not isinstance(polygon, list) or len(polygon) != 4:
        raise _refuse(f"{path}.polygon", "is not four corners")
    for index, point in enumerate(polygon):
        if not isinstance(point, list) or len(point) != 2:
            raise _refuse(f"{path}.polygon[{index}]", "is not an (x, y) pair")
        for axis, value in enumerate(point):
            _number(value, f"{path}.polygon[{index}][{axis}]")
    bbox = item["bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise _refuse(f"{path}.bbox", "is not four numbers")
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    if [_number(value, f"{path}.bbox") for value in bbox] != [min(xs), min(ys), max(xs), max(ys)]:
        raise _refuse(f"{path}.bbox", "is not the extent of its polygon")
    confidence = item["confidence"]
    if confidence is not None and not 0 <= _number(confidence, f"{path}.confidence") <= 1:
        raise _refuse(f"{path}.confidence", "is not in [0, 1]")


def _image_bbox(value: Any, width: int, height: int, path: str) -> None:
    if not isinstance(value, list) or [_number(v, path) for v in value] != [0, 0, width, height]:
        raise _refuse(path, f"is not the whole {width}x{height} page")


def _no_floats(value: Any, path: str) -> None:
    """Run facts travel into records, which carry no floats."""
    if isinstance(value, float):
        raise _refuse(path, "carries a float")
    if isinstance(value, dict):
        for key, item in value.items():
            _no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_floats(item, f"{path}[{index}]")


def _check_run(run: Any) -> None:
    if not isinstance(run, dict) or run.get("engine") not in ("surya", FIXTURE_ENGINE):
        raise _refuse("$.run", "names neither the surya nor the fixture engine")
    if run["engine"] == FIXTURE_ENGINE:
        _closed(run, _FIXTURE_RUN_FIELDS, "$.run")
        return
    _closed(run, _SURYA_RUN_FIELDS, "$.run")
    _no_floats(run, "$.run")
    if run["device"] != "cpu" or run["deterministic_algorithms"] is not True:
        raise _refuse("$.run", "is not a deterministic CPU run")
    for field in ("surya_ocr", "torch", "python", "cpu_capability", "machine"):
        if not isinstance(run[field], str) or not run[field].strip():
            raise _refuse(f"$.run.{field}", "is not a non-blank string")
    if (
        isinstance(run["threads"], bool)
        or not isinstance(run["threads"], int)
        or run["threads"] < 1
    ):
        raise _refuse("$.run.threads", "is not a positive integer")
    settings = _closed(run["settings"], set(contract.OUTPUT_SETTINGS), "$.run.settings")
    if not all(isinstance(value, str) for value in settings.values()):
        raise _refuse("$.run.settings", "carries a setting that is not recorded as text")
    if not isinstance(run["weights"], list) or not run["weights"]:
        raise _refuse("$.run.weights", "lists no weight file")
    # The bundle lock's own checks: each checkpoint at its host's pin, and
    # every weight file a path, a sha256 digest and a size.
    try:
        contract.check_checkpoints(run["checkpoints"])
        contract.check_file_rows(run["weights"])
    except contract.BundleRefusal as error:
        raise _refuse("$.run", f"does not describe a locked bundle: {error}") from error


def validate_page_document(
    document: Any, *, width: int, height: int, input_ordinal: int
) -> dict[str, Any]:
    """One page document, checked against its closed shape and its own page.

    Lines keep Surya's order. Blocks come in Surya's reading order, so each
    block's `position` is its index; anything else is not what Surya returns.
    `reading_order` names where those positions came from, with the reason
    when Surya raster-sorted instead of running its head. A page whose layout
    call reports an error is refused, and so is a block `count` other than
    0, which Surya's fast layout never sets.
    """
    page = _closed(document, _PAGE_FIELDS, "$")
    if page["schema"] != PAGE_SCHEMA:
        raise _refuse("$.schema", f"is {page['schema']!r}, not {PAGE_SCHEMA!r}")
    if page["input_ordinal"] != input_ordinal:
        raise _refuse("$.input_ordinal", f"is not {input_ordinal}")
    if page["image_size"] != [width, height]:
        raise _refuse("$.image_size", f"is not the sealed page's {width}x{height}")
    _check_run(page["run"])
    lines = _closed(page["text_detection"], _LINES_FIELDS, "$.text_detection")
    _image_bbox(lines["image_bbox"], width, height, "$.text_detection.image_bbox")
    if not isinstance(lines["bboxes"], list):
        raise _refuse("$.text_detection.bboxes", "is not a list")
    for index, line in enumerate(lines["bboxes"]):
        where = f"$.text_detection.bboxes[{index}]"
        _polygon_box(_closed(line, _LINE_FIELDS, where), where)
    layout = _closed(page["layout"], _LAYOUT_FIELDS, "$.layout")
    _image_bbox(layout["image_bbox"], width, height, "$.layout.image_bbox")
    if layout["error"] is not False:
        raise _refuse("$.layout.error", "is not false: Surya's layout call reported an error")
    if layout["raw"] is not None and not isinstance(layout["raw"], str):
        raise _refuse("$.layout.raw", "is neither null nor a string")
    if not isinstance(layout["bboxes"], list):
        raise _refuse("$.layout.bboxes", "is not a list")
    for index, block in enumerate(layout["bboxes"]):
        where = f"$.layout.bboxes[{index}]"
        _polygon_box(_closed(block, _BLOCK_FIELDS, where), where)
        for field in ("label", "raw_label"):
            if not isinstance(block[field], str) or not block[field]:
                raise _refuse(f"{where}.{field}", "is not a non-blank string")
        if _count(block["position"], f"{where}.position") != index:
            raise _refuse(f"{where}.position", f"is not {index}, its place in reading order")
        if _count(block["count"], f"{where}.count") != 0:
            raise _refuse(f"{where}.count", "is not 0, which Surya's fast layout always gives")
    order, reason = page["reading_order"], page["reading_order_reason"]
    if order not in contract.READING_ORDERS:
        raise _refuse("$.reading_order", f"is not one of {list(contract.READING_ORDERS)}")
    if (order == contract.ORDER_HEAD) != (reason is None) or (
        reason is not None and (not isinstance(reason, str) or not reason.strip())
    ):
        raise _refuse(
            "$.reading_order_reason",
            "is not null for the order head and a non-blank reason for a raster fallback",
        )
    return page


def parse_page_document(raw: bytes, *, width: int, height: int, input_ordinal: int) -> SuryaPage:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise SuryaOutputRefusal(f"a Surya page document is not JSON: {error}") from error
    return SuryaPage(
        raw=raw,
        document=validate_page_document(
            document, width=width, height=height, input_ordinal=input_ordinal
        ),
    )


def _page_bytes(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


# --- the fixture detector ----------------------------------------------------


def _require_pages(pages: Mapping[int, Any]) -> None:
    if not pages:
        raise SuryaOutputRefusal("Surya was given no page to run on")


def _fixture_polygon(row: Mapping[str, Any]) -> dict[str, Any]:
    polygon = [[float(x), float(y)] for x, y in row["polygon"]]
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return {
        "polygon": polygon,
        "bbox": [min(xs), min(ys), max(xs), max(ys)],
        "confidence": row["confidence_bp"] / 10_000,
    }


def declared_page_documents(
    lines: Sequence[Mapping[str, Any]],
    blocks: Sequence[Mapping[str, Any]],
    pages: Mapping[int, tuple[int, int]],
    run_facts: Mapping[str, Any],
    *,
    reading_orders: Mapping[int, tuple[str, str | None]] | None = None,
) -> dict[int, bytes]:
    """Each page's document in the runner's shape, built from declared rows.

    `pages` maps each page ordinal to its (width, height); the documents are
    numbered by input order, as the runner numbers the pages it is given. A
    row declared for a page that is not in `pages` is refused by name.
    `reading_orders` gives a page's ordering and its reason; a page it does
    not name was ordered by Surya's head.
    """
    _require_pages(pages)
    for family, rows in (("surya_line", lines), ("surya_block", blocks)):
        unsealed = sorted({row["page_ordinal"] for row in rows} - set(pages))
        if unsealed:
            raise SuryaOutputRefusal(
                f"the fixture declares {family} rows for page(s) {unsealed}, which are not "
                "among the sealed pages Surya was given"
            )
    documents = {}
    for input_ordinal, (ordinal, (width, height)) in enumerate(sorted(pages.items()), start=1):
        page_blocks = sorted(
            (row for row in blocks if row["page_ordinal"] == ordinal),
            key=lambda row: row["position"],
        )
        order, reason = (reading_orders or {}).get(ordinal, (contract.ORDER_HEAD, None))
        documents[ordinal] = _page_bytes(
            {
                "schema": PAGE_SCHEMA,
                "input_ordinal": input_ordinal,
                "image_size": [width, height],
                "run": dict(run_facts),
                "text_detection": {
                    "bboxes": [
                        _fixture_polygon(row) for row in lines if row["page_ordinal"] == ordinal
                    ],
                    "image_bbox": [0.0, 0.0, float(width), float(height)],
                },
                "layout": {
                    "bboxes": [
                        {
                            **_fixture_polygon(row),
                            "label": row["label"],
                            "raw_label": row["raw_label"],
                            "position": row["position"],
                            "count": 0,
                        }
                        for row in page_blocks
                    ],
                    "image_bbox": [0.0, 0.0, float(width), float(height)],
                    "raw": None,
                    "error": False,
                },
                "reading_order": order,
                "reading_order_reason": reason,
            }
        )
    return documents


def _parsed(written: Mapping[int, bytes], sizes: Mapping[int, tuple[int, int]]) -> dict:
    return {
        ordinal: parse_page_document(
            written[ordinal],
            width=sizes[ordinal][0],
            height=sizes[ordinal][1],
            input_ordinal=input_ordinal,
        )
        for input_ordinal, ordinal in enumerate(sorted(written), start=1)
    }


def fixture_surya_run(
    lines: Sequence[Mapping[str, Any]],
    blocks: Sequence[Mapping[str, Any]],
    pages: Mapping[int, tuple[int, int]],
    identity: ChairIdentity,
    details: ServingDetails,
) -> SuryaRun:
    """Answer each page with the lines and blocks the synthetic fixture declares for it.

    `pages` maps each page ordinal to its (width, height). Documents are built in
    the runner's shape and checked like the runner's, so the fixture proves the
    same reader the real detector feeds.
    """
    run_facts = {"engine": FIXTURE_ENGINE, "declared_by": _FIXTURE_DECLARATION}
    documents = _parsed(declared_page_documents(lines, blocks, pages, run_facts), pages)
    return SuryaRun(run_facts=run_facts, serving_details=details, pages=documents)


# --- the real detector, in its own environment -------------------------------

Runner = Callable[..., subprocess.CompletedProcess]


def _environment_dir(profile: SubprocessProfile) -> Path:
    return REPO_ROOT / profile.environment


def _command(profile: SubprocessProfile, *arguments: str) -> list[str]:
    """The runner under the environment's own interpreter, by absolute path.

    `uv sync --locked --project <environment>` builds that interpreter from the
    committed lock; nothing here syncs, resolves or searches PATH for it.
    """
    environment = _environment_dir(profile)
    interpreter = environment / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        raise ServingConfigurationError(
            f"Surya's environment has no interpreter at {interpreter}; build it with "
            f"`uv sync --locked --project {profile.environment}`"
        )
    return [str(interpreter), str(environment / RUNNER), *arguments]


def _child_environment() -> dict[str, str]:
    """What the child inherits: a locale and a temporary directory, and nothing
    that could set one of Surya's own settings behind the record's back."""
    kept = {"LANG", "LC_ALL", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key in kept}


def _run_child(
    runner: Runner, argv: list[str], timeout: int, what: str
) -> subprocess.CompletedProcess:
    """One child process, with a timeout or a failed start named as such."""
    try:
        return runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SuryaRunFailure(
            f"Surya's {what} did not finish within {timeout} seconds and was stopped"
        ) from error
    except OSError as error:
        raise SuryaRunFailure(f"Surya's {what} could not be started: {error}") from error


def _release(version: str) -> str:
    """`2.14.0+cu130` and `2.14.0` are the same release."""
    return version.split("+", 1)[0]


def environment_versions(
    profile: SubprocessProfile, *, runner: Runner = subprocess.run
) -> dict[str, str]:
    """The versions Surya's environment reports, refused unless the row's pins match."""
    result = _run_child(
        runner, _command(profile, "--check"), profile.startup_timeout_seconds, "version check"
    )
    if result.returncode != 0:
        raise ServingConfigurationError(
            f"Surya's environment in {profile.environment} did not answer its version check "
            f"(exit {result.returncode}): {result.stderr.strip()[-400:]}; sync it with "
            f"`uv sync --locked --project {profile.environment}`"
        )
    try:
        found = json.loads(result.stdout)
    except ValueError as error:
        raise ServingConfigurationError(
            "Surya's environment answered its version check with something other than JSON"
        ) from error
    expected = {"surya_ocr": profile.required_packages["surya-ocr"]}
    expected["torch"] = profile.required_packages["torch"]
    if not isinstance(found, dict) or any(
        _release(str(found.get(key))) != value for key, value in expected.items()
    ):
        raise ServingConfigurationError(
            f"Surya's environment reports {found}, and the serving row pins {expected}; the "
            "sealed catalogue would describe an engine that did not run"
        )
    return {key: str(value) for key, value in found.items()}


def run_surya_subprocess(
    profile: SubprocessProfile,
    bundle_root: Path,
    pages: Mapping[int, bytes],
    sizes: Mapping[int, tuple[int, int]],
    identity: ChairIdentity,
    *,
    manifest_rows: Sequence[Mapping[str, Any]] | None = None,
    runner: Runner = subprocess.run,
) -> SuryaRun:
    """Run Surya once over every page, in page order, and check what it wrote.

    One process loads the models once and reads every page; its timeout is the
    row's startup allowance plus its per-page allowance for each page.
    """
    _require_pages(pages)
    versions = environment_versions(profile, runner=runner)
    ordinals = sorted(pages)
    with tempfile.TemporaryDirectory(prefix="verbatus-surya-") as work:
        work_root = Path(work)
        inputs = []
        for ordinal in ordinals:
            # No extension: the sealed page may be any format Pillow reads.
            path = work_root / f"page-{ordinal}"
            path.write_bytes(pages[ordinal])
            inputs.append(str(path))
        output = work_root / "out"
        started_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        argv = _command(
            profile,
            "--weights",
            str(bundle_root),
            "--threads",
            str(profile.threads),
            "--output-dir",
            str(output),
            *inputs,
        )
        result = _run_child(runner, argv, profile.run_timeout_seconds(len(pages)), "runner")
        if result.returncode != 0:
            raise SuryaRunFailure(
                f"Surya's runner failed (exit {result.returncode}): {result.stderr.strip()[-800:]}"
            )
        written = {}
        for input_ordinal, ordinal in enumerate(ordinals, start=1):
            document = output / f"page-{input_ordinal}.json"
            if not document.is_file():
                raise SuryaOutputRefusal(f"Surya's runner wrote no document for page {ordinal}")
            written[ordinal] = document.read_bytes()
    return surya_run(
        profile, identity, versions, started_at, written, sizes, manifest_rows=manifest_rows
    )


def surya_run(
    profile: SubprocessProfile,
    identity: ChairIdentity,
    versions: Mapping[str, str],
    started_at: str,
    written: Mapping[int, bytes],
    sizes: Mapping[int, tuple[int, int]],
    *,
    manifest_rows: Sequence[Mapping[str, Any]] | None = None,
) -> SuryaRun:
    """The run the parent records from what Surya's runner wrote, page by page.

    `written` maps each page ordinal to its document's bytes; `versions` is
    what the environment reported. Every document is checked against the
    closed shape, and all must name the one run this row asked for. Given the
    chair's digest manifest, the weights the run names must be exactly the
    files it pins, less the bundle's own lock.
    """
    _require_pages(written)
    documents = _parsed(written, sizes)
    run_facts = documents[min(documents)].document["run"]
    if any(page.document["run"] != run_facts for page in documents.values()):
        raise SuryaOutputRefusal("Surya's page documents disagree about the run that wrote them")
    if (
        run_facts["engine"] != "surya"
        or run_facts["threads"] != profile.threads
        or run_facts["surya_ocr"] != versions["surya_ocr"]
        or run_facts["torch"] != versions["torch"]
    ):
        raise SuryaOutputRefusal("Surya's run facts do not describe the run this row asked for")
    if manifest_rows is not None:
        pinned = sorted(
            (
                {"path": row["path"], "sha256": row["sha256"], "size": row["size"]}
                for row in manifest_rows
                if row["path"] != contract.BUNDLE_FILE
            ),
            key=lambda row: row["path"],
        )
        if run_facts["weights"] != pinned:
            raise SuryaOutputRefusal(
                "the weights Surya's run names are not the files the chair's digest manifest pins"
            )
    details = ServingDetails(
        tokenizer_revision=identity.receipt_revision,
        seed=0,
        # A detector has no token context, and each model sizes the page itself.
        context_cap=0,
        pixel_cap=0,
        engine="surya-ocr",
        # The CPU instruction set is part of what ran: torch picks kernels by it.
        engine_version=(
            f"surya-ocr {versions['surya_ocr']}; torch {versions['torch']}; "
            f"cpu {run_facts['cpu_capability']} on {run_facts['machine']}"
        ),
        dtype="float32",
        adapter_identity=None,
        endpoint=f"subprocess://{profile.device}/threads-{profile.threads}",
        started_at=started_at,
    )
    return SuryaRun(run_facts=run_facts, serving_details=details, pages=documents)


PREFETCH = "prefetch.py"
BUNDLE_ARTIFACT = "surya2-detection"
PREFETCH_TIMEOUT_SECONDS = 1800
# What the prefetch child inherits: a locale, a temporary directory and the
# route to the network, never a variable that could set one of Surya's settings.
_PREFETCH_ENVIRONMENT = {
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
}


class SuryaBundleFetcher:
    """The model store's fetcher for Surya's weight bundle: `prefetch.py`, run in
    Surya's own environment, writes the bundle and its lock at the destination.

    The Hugging Face client's own cache goes beside the destination, never into
    it, so the bundle holds only what the lock names.
    """

    def __init__(self, environment: str, *, runner: Runner = subprocess.run) -> None:
        self.environment = environment
        self.runner = runner

    def check(self, artifact: str) -> None:
        """Refuse by name, before anything downloads, an artifact that is not
        Surya's bundle or an environment that is not built."""
        self._interpreter(artifact)

    def _interpreter(self, artifact: str) -> Path:
        if artifact != BUNDLE_ARTIFACT:
            raise ServingConfigurationError(
                f"Surya's prefetch writes {BUNDLE_ARTIFACT!r}, not {artifact!r}"
            )
        interpreter = REPO_ROOT / self.environment / ".venv" / "bin" / "python"
        if not interpreter.is_file():
            raise ServingConfigurationError(
                f"Surya's environment has no interpreter at {interpreter}; build it with "
                f"`uv sync --locked --project {self.environment}`"
            )
        return interpreter

    def fetch(self, artifact: str, destination: Path) -> None:
        interpreter = self._interpreter(artifact)
        environment = REPO_ROOT / self.environment
        child = {key: value for key, value in os.environ.items() if key in _PREFETCH_ENVIRONMENT}
        child["HF_HOME"] = str(destination.parent / "hf-home")
        child["HF_HUB_DISABLE_TELEMETRY"] = "1"
        argv = [str(interpreter), str(environment / PREFETCH), "--out", str(destination)]
        try:
            result = self.runner(
                argv,
                capture_output=True,
                text=True,
                timeout=PREFETCH_TIMEOUT_SECONDS,
                env=child,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SuryaRunFailure(
                f"Surya's prefetch did not finish within {PREFETCH_TIMEOUT_SECONDS} seconds"
            ) from error
        except OSError as error:
            raise SuryaRunFailure(f"Surya's prefetch could not be started: {error}") from error
        if result.returncode != 0:
            raise SuryaRunFailure(
                f"Surya's prefetch failed (exit {result.returncode}): "
                f"{_without_url_credentials(result.stderr.strip())[-800:]}"
            )


# The user-information part of a URL, as a proxy URL carries a credential.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def _without_url_credentials(text: str) -> str:
    """The text with any URL's user information replaced, so an excerpt that
    echoes a proxy URL carries no credential into a journal or report."""
    return _URL_CREDENTIALS.sub(r"\g<scheme><redacted>@", text)


class SuryaSubprocess:
    """How a stage answers a subprocess Surya row: its environment is checked
    before any paid work starts, and its runner is started when the pages are
    ready. Tests stand in for both with `operations.serving.fakes.InProcessSurya`.
    """

    def check(self, profile: SubprocessProfile) -> dict[str, str]:
        return environment_versions(profile)

    def __call__(
        self,
        profile: SubprocessProfile,
        bundle_root: Path,
        pages: Mapping[int, bytes],
        sizes: Mapping[int, tuple[int, int]],
        identity: ChairIdentity,
        *,
        manifest_rows: Sequence[Mapping[str, Any]] | None = None,
    ) -> SuryaRun:
        return run_surya_subprocess(
            profile, bundle_root, pages, sizes, identity, manifest_rows=manifest_rows
        )
