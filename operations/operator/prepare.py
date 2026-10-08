"""`verbatus prepare`: prepared page images, and the triage manifest that cuts the same
pages from the original scans at the Door.

pagekit prepares the pages: lossless TIFF images to look at and reuse, its manifest
and its project file, which a later run continues from so corrections are never lost.
Every model reads the page the Door seals, so the prepared images never enter a run
themselves. What enters is pagekit's geometry, written as a
`triage-decision-manifest-v1` over the original scans (actor `producer`, identity
`pagekit`, revision pagekit's version): the Door keeps each original as the page's
`parent_frame` and cuts the page from it, so every reading still traces to the scan
(`operations/triage/pagekit_geometry.py` says how, and where triage cannot follow
pagekit exactly).

The scans are untrusted images, so the work runs in a child Python process (`main`)
whose environment holds no credential, as `verbatus ingest` does. The child prints
what it did; the parent relays it. A Ctrl-C leaves the output folder as it was:
pagekit writes all its files or none, and the triage documents are written after it,
each whole, with a second interrupt ignored while anything is being put back or written.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import signal
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

from .errors import ErrorCode, OperatorError, strip_control_bytes

TRIAGE_MANIFEST_NAME: Final = "triage-decision-manifest.json"
TRIAGE_RECIPE_NAME: Final = "triage-producer-recipe.json"
NOTES_NAME: Final = "triage-notes.txt"
MAX_CORPUS_ID_CHARACTERS: Final = 256
# The child's exit statuses.
REFUSED_EXIT: Final = 2
INCOMPLETE_EXIT: Final = 3
INTERRUPTED_EXIT: Final = 130
# The child's first line once it starts writing; before it, nothing was written.
WRITING_MARKER: Final = "verbatus-prepare: writing"
MAX_CHILD_STDERR_BYTES: Final = 64 * 1024
# How many per-page notes and stray files the terminal shows before it summarises.
_SHOWN: Final = 8
_CHECKOUT: Final = Path(__file__).resolve().parents[2]


def prepare(
    *,
    scans: Path,
    out: Path,
    overrides: Path | None,
    corpus_id: str | None,
    workspace: Path,
    printer: Callable[[str], None],
    state_dir: Path | None = None,
    crop: str | None = None,
    cache: Path | None = None,
    no_cache: bool = False,
) -> None:
    """Prepare `scans` into `out` in a credential-free child process.

    `state_dir`, the operator's state folder, is named in the commands printed next:
    the run keeps its tree there, and the Door accepts it only inside approved storage.
    """

    from .ingest import _deny_same_user_inspection
    from .surface import credential_free_environment

    scans_path = _absolute(scans, workspace)
    request = {
        "scans": str(scans_path),
        "out": str(_absolute(out, workspace)),
        "overrides": None if overrides is None else str(_absolute(overrides, workspace)),
        "corpus_id": corpus_id if corpus_id is not None else scans_path.name,
        "state_dir": None if state_dir is None else str(_absolute(state_dir, workspace)),
        "crop": crop,
        "cache": None if cache is None else str(_absolute(cache, workspace)),
        "no_cache": bool(no_cache),
    }
    try:
        _check_request(request)
    except ValueError as error:
        raise OperatorError(ErrorCode.PREPARE_REFUSED, detail=str(error)) from error
    _deny_same_user_inspection(ErrorCode.PREPARE_REFUSED)
    returncode, writing, stderr, interrupted = _run_child(
        json.dumps(request), _child_environment(credential_free_environment()), printer
    )
    detail = stderr.strip() or f"the preparation process exited {returncode}"
    if returncode == 0:
        return
    if returncode == INTERRUPTED_EXIT or (interrupted and returncode < 0):
        raise OperatorError(ErrorCode.PREPARE_INTERRUPTED, detail=detail)
    if returncode == REFUSED_EXIT or not writing:
        raise OperatorError(ErrorCode.PREPARE_REFUSED, detail=detail)
    raise OperatorError(ErrorCode.PREPARE_INCOMPLETE, detail=detail)


def _child_environment(environment: dict[str, str]) -> dict[str, str]:
    """The worker's environment, importing the repository this module belongs to.

    pagekit is not an installed package, and with PYTHONSAFEPATH set Python does not
    import from the folder it starts in, so the repository root is named explicitly.
    It replaces any PYTHONPATH the shell set, so no other code is imported first.
    """
    return {**environment, "PYTHONPATH": str(_CHECKOUT)}


def _absolute(path: Path, workspace: Path) -> Path:
    path = Path(path).expanduser()
    return path if path.is_absolute() else workspace / path


def _check_request(request: dict[str, Any]) -> dict[str, Any]:
    for name in ("scans", "out", "corpus_id"):
        if not isinstance(request.get(name), str) or not request[name].strip():
            raise ValueError(f"prepare needs a non-blank {name.replace('_', ' ')}")
    if len(request["corpus_id"]) > MAX_CORPUS_ID_CHARACTERS:
        raise ValueError(f"the corpus id is longer than {MAX_CORPUS_ID_CHARACTERS} characters")
    if request.get("overrides") is not None and not isinstance(request["overrides"], str):
        raise ValueError("the overrides file must be a path")
    return request


def _run_child(
    request: str, environment: dict[str, str], printer: Callable[[str], None]
) -> tuple[int, bool, str, bool]:
    """Run the child, relaying its output as it comes.

    A Ctrl-C reaches the child from the terminal too; the parent passes it on in case
    it did not, then waits for the child to put the output folder back as it was.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "operations.operator.prepare"],
        cwd=_CHECKOUT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    kept: dict[str, str] = {}

    def drain() -> None:
        data = bytearray()
        assert process.stderr is not None
        while chunk := process.stderr.read(65536):
            data += chunk[: MAX_CHILD_STDERR_BYTES - len(data)]
        kept["stderr"] = data.decode("utf-8", errors="replace")

    reader = threading.Thread(target=drain)
    reader.start()
    assert process.stdin is not None and process.stdout is not None
    with contextlib.suppress(BrokenPipeError):
        process.stdin.write(request.encode("utf-8"))
        process.stdin.close()
    writing = interrupted = False
    while True:
        try:
            for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if line == WRITING_MARKER:
                    writing = True
                else:
                    printer(strip_control_bytes(line))
            returncode = process.wait()
            break
        except KeyboardInterrupt:
            interrupted = True
            with contextlib.suppress(OSError):
                process.send_signal(signal.SIGINT)
    reader.join()
    return returncode, writing, kept.get("stderr", ""), interrupted


# The child.


def _interrupt_once(_signum: int, _frame: Any) -> None:
    """Stop on the first Ctrl-C, and let the clean-up it starts finish undisturbed."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt


@contextlib.contextmanager
def _interrupts_held() -> Iterator[None]:
    """Finish the writes inside whole: a Ctrl-C meanwhile is ignored."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def main() -> int:
    """The child: plan with pagekit, map the geometry, write everything, say what happened."""
    signal.signal(signal.SIGINT, _interrupt_once)
    from common.contracts.errors import ContractError
    from operations.triage.pagekit_geometry import MappingError
    from pagekit.project import PrepareError

    try:
        request = _check_request(json.loads(sys.stdin.read()))
        prepared = _plan(request)
    except KeyboardInterrupt:
        print("Stopped before anything was written.", file=sys.stderr)
        return INTERRUPTED_EXIT
    except (PrepareError, MappingError, ContractError, OSError, ValueError) as error:
        print(strip_control_bytes(str(error)), file=sys.stderr)
        return REFUSED_EXIT
    except Exception as error:  # anything else still wrote nothing: refused, by name
        print(f"cannot prepare: {type(error).__name__}: {error}", file=sys.stderr)
        return REFUSED_EXIT
    print(WRITING_MARKER, flush=True)
    try:
        _write_pages(prepared)
    except KeyboardInterrupt:
        print("Stopped; the output folder was left as it was.", file=sys.stderr)
        return INTERRUPTED_EXIT
    except Exception as error:  # pagekit puts the folder back on any failure
        print(
            f"pagekit could not write the pages, and left the output folder as it was: "
            f"{strip_control_bytes(str(error))}",
            file=sys.stderr,
        )
        return REFUSED_EXIT
    try:
        with _interrupts_held():
            _write_triage(prepared)
    except Exception as error:  # the pages are written; the manifest may not be
        print(
            "The prepared pages were written, but the triage manifest for the Door was not: "
            f"{strip_control_bytes(str(error))}",
            file=sys.stderr,
        )
        return INCOMPLETE_EXIT
    for line in summary(prepared):
        print(strip_control_bytes(line))
    return 0


class Prepared:
    """Everything one run will write, settled before anything is written."""

    def __init__(self, request: dict[str, Any], plan: Any, pages: list[dict[str, Any]]):
        self.scans = Path(request["scans"])
        self.out = Path(request["out"])
        self.corpus_id = request["corpus_id"]
        self.overrides = request.get("overrides")
        self.state_dir = request.get("state_dir")
        self.plan = plan
        self.pages = pages  # one entry per prepared page, in pagekit's order
        self.manifest: dict[str, Any] = {}
        self.recipe: dict[str, Any] = {}
        self.uncovered: list[str] = []
        self.folder_refusal: str | None = None
        self.unapproved: list[Path] = []
        # Scans pagekit could not use: no page, no triage row; each with its reason.
        self.skipped: list[dict[str, Any]] = list(getattr(plan, "skipped", []))
        self.pagekit_manifest: dict[str, Any] = {}


def _plan(request: dict[str, Any]) -> Prepared:
    from operations.triage.pagekit_geometry import (
        MappingError,
        door_colour_mode,
        make_manifest,
        make_row,
        map_pages,
    )
    from operations.triage.pagekit_recipe import make_recipe
    from pagekit import __version__ as pagekit_version
    from pagekit.pipeline import DETECTORS
    from pagekit.prepare import plan
    from pagekit.project import PrepareError, load_settings
    from pagekit.project import digest as settings_digest

    scans = Path(request["scans"])
    if not scans.is_dir():
        raise PrepareError(f"the scans folder {scans} is not a folder")
    overrides = request.get("overrides")
    if overrides is not None and not Path(overrides).is_file():
        raise PrepareError(f"the overrides file {overrides} does not exist")
    settings: dict[str, Any] = {}
    if load_settings()["output_format"]["value"] != "tiff":
        settings["output_format"] = "tiff"
    if request.get("crop") is not None:
        settings["crop"] = request["crop"]
    if request.get("no_cache"):
        settings["stage_cache"] = 0
    if request.get("cache") is not None:
        settings["stage_cache_folder"] = request["cache"]
    # The detector pipeline pagekit's own prepare runs: none is chosen here.
    planned = plan(
        [scans], request["out"], None, overrides, detectors=DETECTORS, settings_overrides=settings
    )
    by_source: dict[str, list[Any]] = {}
    for page in planned.pages:
        by_source.setdefault(page.source.relative, []).append(page)
    rows: dict[str, dict[str, Any]] = {}
    named: dict[str, str] = {}
    pages = []
    for source_pages in by_source.values():
        source = source_pages[0].source
        if getattr(source, "undo_tag", None) is not None:
            # The image library turns this file by its tag on opening it, as the Door
            # opens it; with the tag not trusted pagekit turns it back to the stored
            # pixels, so its pages and the Door's would differ.
            raise MappingError(
                f"{source.relative}: its orientation tag is not trusted, so pagekit works on "
                "the stored pixels, but the image library turns this file by the tag on "
                "opening it, and the Door opens it that way. Trust the tag, or save the scan "
                "without one, and run prepare again"
            )
        steps = [page.steps for page in source_pages]
        mapped = map_pages(
            [page.chain for page in source_pages],
            steps[0]["split"]["value"],
            _fills(source, source_pages, planned.settings),
            [door_colour_mode(getattr(page, "mode", None), source.mode) for page in source_pages],
        )
        human = any(
            entry["origin"] in ("manual", "locked") for page in steps for entry in page.values()
        )
        row = make_row(
            corpus_id=request["corpus_id"],
            source_sha256=source.sha256,
            frame=source.size,
            pages=mapped,
            revision=pagekit_version,
            confidence=_confidence(source_pages),
            human_override=human,
        )
        # The Door keys triage rows by the scan's bytes, so identical files share one.
        if rows.setdefault(source.sha256, row) != row:
            raise MappingError(
                f"{named[source.sha256]} and {source.relative} are the same file but were "
                "prepared differently; the Door cuts identical files the same way. Remove "
                "one, or give both the same corrections"
            )
        named.setdefault(source.sha256, source.relative)
        for page, door in zip(source_pages, mapped, strict=True):
            pages.append(
                {
                    "source": source.relative,
                    "scan": source.path.name,
                    "page": page.number,
                    "output": page.output_name,
                    "flags": page.flags,
                    "steps": page.steps,
                    "door": door,
                    "density": getattr(page, "density", None),
                }
            )
    prepared = Prepared(request, planned, pages)
    prepared.manifest = make_manifest(request["corpus_id"], list(rows.values()))
    methods: dict[str, set[str]] = {}
    for page in planned.pages:
        for step, entry in page.steps.items():
            if entry.get("method"):
                methods.setdefault(step, set()).add(entry["method"])
    prepared.recipe = make_recipe(
        revision=pagekit_version,
        settings_sha256=settings_digest(
            {name: entry["value"] for name, entry in planned.settings.items()}
        ),
        detector_methods=methods,
    )
    _check_folder(prepared, set(rows))
    return prepared


def _fills(source: Any, pages: list[Any], settings: dict[str, Any]) -> list[list[int]]:
    """Each page's paper colour, measured as pagekit measures the fill of its own page,
    as sample levels in the scan's stored mode."""
    from PIL import Image

    from operations.triage.pagekit_geometry import fill_levels
    from pagekit.geometry import paper_colour

    image = source.open()
    palette = None
    if source.mode == "P":
        with Image.open(source.path) as stored:
            palette = stored.getpalette()
    long_side = settings["paper_estimate_long_side_px"]["value"]
    return [
        fill_levels(paper_colour(image, page.chain, long_side)[0], source.mode, palette)
        for page in pages
    ]


def _confidence(pages: list[Any]) -> int:
    """pagekit's least confident step on the source's pages, as a triage ordinal 0-4;
    0 when any page is flagged. A value a person set counts as certain."""
    if any(page.flags for page in pages):
        return 0
    least = min(
        1.0 if entry["confidence"] is None else entry["confidence"]
        for page in pages
        for entry in page.steps.values()
    )
    return max(0, min(4, int(least * 4)))


def _check_folder(prepared: Prepared, rows: set[str]) -> None:
    """Note what the Door would refuse: a scan with no triage row, or a folder outside
    the approved storage the data gate allows."""
    from operations.submit import gate, submit

    try:
        roots = gate.approved_storage_roots(gate.load_policy())
    except Exception:  # an unreadable policy is the Door's to refuse, by name
        roots = ()
    for folder in (prepared.scans, prepared.out):
        try:
            gate.require_approved_storage_location(folder, roots, "folder")
        except Exception:
            prepared.unapproved.append(folder)

    try:
        entries, _skipped = submit.inspect_folder(prepared.scans)
    except Exception as error:  # any refusal here is reported, not fatal to preparing
        named = list(getattr(error, "entries", ()))
        listed = ", ".join(named[:_SHOWN]) + (
            f" and {len(named) - _SHOWN} more" if len(named) > _SHOWN else ""
        )
        prepared.folder_refusal = str(error) + (f" ({listed})" if named else "")
        return
    skipped = {entry["sha256"] for entry in prepared.skipped}
    prepared.uncovered = sorted(
        entry["relative_path"]
        for entry in entries
        if entry["sha256"] not in rows and entry["sha256"] not in skipped
    )


def _write_pages(prepared: Prepared) -> None:
    from pagekit.output import execute

    prepared.pagekit_manifest = execute(prepared.plan)


def _write_triage(prepared: Prepared) -> None:
    from common.contracts.canonical import canonical_bytes
    from common.durability import atomic_replace

    prepared.out.mkdir(parents=True, exist_ok=True)
    for name, document in (
        (TRIAGE_MANIFEST_NAME, prepared.manifest),
        (TRIAGE_RECIPE_NAME, prepared.recipe),
    ):
        atomic_replace(prepared.out / name, canonical_bytes(document) + b"\n")
    atomic_replace(prepared.out / NOTES_NAME, "\n".join(_notes(prepared)).encode("utf-8") + b"\n")


def _notes(prepared: Prepared) -> list[str]:
    lines = [
        "How the Door will cut each page from its original scan, where that differs from "
        "pagekit's own page. Produced by verbatus prepare; it is rewritten on every run.",
        "",
    ]
    differing = [page for page in prepared.pages if page["door"].notes or page.get("density")]
    if not differing:
        lines.append(
            "Every page is cut as pagekit cut it (a skewed page to within half a pixel on each axis)."
        )
    for page in differing:
        lines.append(f"{page['output']} (from {page['scan']}, page {page['page']}):")
        lines += [f"  - {note.text}" for note in page["door"].notes]
        if page.get("density"):
            nominal = page["density"]["nominal"]
            lines.append(
                f"  - pagekit tags this page with a nominal density of {nominal[0]:g} x "
                f"{nominal[1]:g} dpi; the Door's page carries no density (its pages hold "
                "pixels only), so the run does not see it"
            )
    return lines


def _quoted(path: Path) -> str:
    return shlex.quote(str(path))


def summary(prepared: Prepared) -> list[str]:
    """What was done, in plain words, and the exact next commands."""
    from operations.triage.pagekit_geometry import NOTE_SUMMARIES
    from pagekit.answer import STEPS

    out = prepared.out
    pages = prepared.pages
    sources = {page["source"] for page in pages}
    flagged = [page for page in pages if page["flags"]]
    lines = [
        f"Prepared {len(pages)} page(s) from {len(sources)} scan(s) into {out}.",
        "The scans were only read; nothing in the scans folder was changed.",
        f"Pages to review: {len(flagged)} of {len(pages)}.",
    ]
    if prepared.skipped:
        lines.append(
            f"Warning: {len(prepared.skipped)} scan(s) were skipped and not prepared, so the "
            "triage manifest has no row for them:"
        )
        lines += [f"  {entry['name']}: {entry['reason']}" for entry in prepared.skipped]
        lines.append(
            "  These scans were not prepared; remove them from the scans folder or fix them "
            "before uploading. Upload refuses a scans folder holding a scan the triage "
            "manifest has no row for."
        )
    neutral = [
        step.replace("_", " ")
        for step in STEPS
        if any(
            str(page["steps"][step].get("method") or "").startswith("pagekit.neutral-default.")
            for page in pages
        )
    ]
    if neutral:
        lines.append(
            f"On some pages pagekit's detector could not decide: {', '.join(neutral)}. "
            "Those steps took a neutral default (no turn, one page, no skew, the whole "
            "page), and their pages are flagged until a person sets the values."
        )
    from pagekit.review import REVIEW_NAME

    sheet = out / REVIEW_NAME
    if sheet.is_file():
        lines.append(f"Review sheet: {sheet}")
    else:
        lines.append(
            "pagekit wrote no review sheet. Each page's flags are in "
            f"{out / 'pagekit-prepare.json'}; the pages are the .tif files beside it."
        )
    exact = [page for page in pages if not page["door"].notes]
    lines.append(
        f"Triage manifest for the Door: {out / TRIAGE_MANIFEST_NAME}. The Door will cut "
        f"{len(pages)} page(s) from the original scans, {len(exact)} as pagekit cut them "
        "(a skewed page to within half a pixel on each axis)."
    )
    counts = Counter(note.code for page in pages for note in page["door"].notes)
    for code, count in sorted(counts.items()):
        lines.append(f"  {count} page(s): {NOTE_SUMMARIES[code]}.")
    if counts:
        lines.append(f"  Each page's difference is listed in {out / NOTES_NAME}.")
    if prepared.folder_refusal is not None:
        lines.append(
            "Warning: the Door will not accept the scans folder as it is: "
            f"{prepared.folder_refusal}"
        )
    if prepared.unapproved:
        lines.append(
            "Warning: the Door reads scans and triage documents only from approved storage "
            "(private/ in this checkout), and "
            f"{' and '.join(str(folder) for folder in prepared.unapproved)} "
            f"{'is' if len(prepared.unapproved) == 1 else 'are'} outside it. Move the "
            "folders there and run this again before submitting."
        )
    if prepared.uncovered:
        shown = ", ".join(prepared.uncovered[:_SHOWN])
        more = len(prepared.uncovered) - _SHOWN
        lines.append(
            f"Warning: {len(prepared.uncovered)} file(s) in the scans folder were not "
            f"prepared, and the Door refuses a folder with a scan the manifest does not "
            f"cover: {shown}{f' and {more} more' if more > 0 else ''}. Move them out, or "
            "into a folder of their own."
        )
    lines.append(
        "Next: open the flagged pages. To correct one, write its values in an overrides "
        "file (pagekit/README.md, Corrections) and run this again with --overrides FILE; "
        "it continues the same project and keeps every earlier correction."
    )
    manifest = out / "submission-manifest.json"
    triage_flags = (
        f"--triage-decision-manifest {_quoted(out / TRIAGE_MANIFEST_NAME)} "
        f"--triage-producer-recipe {_quoted(out / TRIAGE_RECIPE_NAME)}"
    )
    verbatus = (
        "verbatus"
        if prepared.state_dir is None
        else f"verbatus --state-dir {_quoted(Path(prepared.state_dir))}"
    )
    lines += [
        "When the pages are right, seal the scans with the triage documents and let the "
        "Door check them on this computer:",
        f"  {verbatus} upload --source {_quoted(prepared.scans)} --manifest-out "
        f"{_quoted(manifest)} {triage_flags}",
        f"  {verbatus} run --run-id prepared-check --submission-folder {_quoted(prepared.scans)} "
        f"--submission-manifest {_quoted(manifest)} {triage_flags}",
        "For a pod run, upload the same way with --sealed-manifest and --network-volume; "
        "operations/pod/README.md (the hand route's launch) says how the pod's run reads "
        "the triage documents.",
    ]
    return lines


if __name__ == "__main__":
    sys.exit(main())
