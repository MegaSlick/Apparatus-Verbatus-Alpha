"""`python -m pagekit check ...`, `python -m pagekit prepare ...`,
`python -m pagekit measure ...` and `python -m pagekit tone ...`.

check: `--master PATH --crop x0,y0,x1,y1 [--crop ...] [--split-x N]`. Exit status 0
when nothing is flagged, 1 when the page should go to review, 2 when the inputs cannot
be checked.

prepare: `[SOURCE ...] --output DIR [--project FILE] [--overrides FILE]
[--report-stale] [--format tiff|png] [--max-dpi N] [--dpi N] [--tone-view]`. Runs every detector
in order and writes the pages, the manifest, the project file and `review.html`. Exit
status 0 when no page is flagged, 1 when any page needs review (with --report-stale:
when any step is stale), 2 when the input cannot be used, and then nothing is written.

measure: `--prepared DIR --gold FILE [--json]`. Compares a prepared batch with a
hand-checked answer file. Exit status 0 when compared, 2 when the input cannot be used.

tone: `--in PAGE --out VIEW.tif [--force]`. Exits 0 when the view was written and its
record printed, 2 when the page cannot be read or the view cannot be written.
"""

from __future__ import annotations

import argparse
import sys

from pagekit.check import CheckError, check, parse_box, report_json
from pagekit.tone import GREY_RULES, ToneError, record_json, tone_file, write_view


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pagekit")
    commands = parser.add_subparsers(dest="command", required=True)
    checker = commands.add_parser("check", help="check declared crops against their master")
    checker.add_argument("--master", required=True, help="the uncropped scan; only read")
    checker.add_argument(
        "--crop",
        action="append",
        required=True,
        metavar="x0,y0,x1,y1",
        help="a crop box in master pixels, right and bottom exclusive; repeat for each page",
    )
    checker.add_argument("--split-x", type=int, help="the declared split column of a spread")
    checker.add_argument(
        "--min-short-side-px", type=int, help="override the shortest acceptable side"
    )
    checker.add_argument("--json", action="store_true", help="print the full JSON report")
    preparer = commands.add_parser("prepare", help="prepare pages from source images")
    preparer.add_argument("sources", nargs="*", help="source images or folders of them; only read")
    preparer.add_argument("--output", required=True, help="the folder prepared pages go to")
    preparer.add_argument(
        "--project",
        help="the project file to continue from and update "
        "(default: OUTPUT/pagekit-project.json, continued from if it exists)",
    )
    preparer.add_argument("--overrides", help="a pagekit-overrides.v1 file of corrections")
    preparer.add_argument(
        "--report-stale",
        action="store_true",
        help="list the steps that would change, and why, without running or writing",
    )
    preparer.add_argument(
        "--format",
        choices=("tiff", "png"),
        help="the lossless format of prepared pages (default: tiff)",
    )
    preparer.add_argument(
        "--max-dpi",
        type=float,
        help="shrink pages above this resolution to it (default: keep the source resolution)",
    )
    preparer.add_argument(
        "--output-mode",
        choices=("source", "grey"),
        help="the mode of the prepared pages of the sources in this run: source (as "
        "scanned) or grey; kept for them on later runs (default: source)",
    )
    preparer.add_argument(
        "--grey-rule",
        choices=("luminance", "red", "green", "blue"),
        help="how a colour page chosen grey is made grey (default: luminance)",
    )
    preparer.add_argument(
        "--dpi",
        type=float,
        help="the resolution, in dots per inch, of sources that carry none "
        "(kept in the project as an override)",
    )
    preparer.add_argument(
        "--tone-view",
        action="store_true",
        help="also write the grey tone view of each page beside it (needs pagekit/tone.py)",
    )
    measurer = commands.add_parser(
        "measure", help="compare a prepared batch with a hand-checked answer file"
    )
    measurer.add_argument("--prepared", required=True, help="the output folder of prepare")
    measurer.add_argument("--gold", required=True, help="a pagekit-gold.v1 answer file")
    measurer.add_argument("--json", action="store_true", help="print the full JSON report")
    toner = commands.add_parser(
        "tone", help="write the grey tone view of a page as lossless TIFF and print its record"
    )
    toner.add_argument("--in", dest="page", required=True, help="the page image; only read")
    toner.add_argument("--out", required=True, help="the view to write; .tif or .tiff")
    toner.add_argument("--grey-rule", choices=GREY_RULES, help="how a colour page becomes grey")
    toner.add_argument("--lift-strength", type=float, help="faint-ink lift, 0 (off) to 0.4")
    toner.add_argument("--sharpen", action="store_true", help="apply the mild unsharp mask")
    toner.add_argument(
        "--force", action="store_true", help="write over an existing output (never the input)"
    )
    return parser


def _prepare(arguments: argparse.Namespace) -> int:
    from pagekit.output import execute
    from pagekit.pipeline import DETECTORS
    from pagekit.prepare import plan
    from pagekit.project import PrepareError
    from pagekit.review import REVIEW_NAME

    settings = {}
    if arguments.format is not None:
        settings["output_format"] = arguments.format
    if arguments.max_dpi is not None:
        settings["max_output_dpi"] = arguments.max_dpi
    if arguments.grey_rule is not None:
        settings["grey_rule"] = arguments.grey_rule
    try:
        prepared = plan(
            arguments.sources,
            arguments.output,
            arguments.project,
            arguments.overrides,
            detectors=DETECTORS,
            settings_overrides=settings,
            dry=arguments.report_stale,
            tone_view=arguments.tone_view,
            source_dpi=arguments.dpi,
            output_mode=arguments.output_mode,
        )
        for entry in prepared.skipped:
            print(f"skipped {entry['name']}: {entry['reason']}")
        if arguments.report_stale:
            for item in prepared.stale:
                page = "" if item["page"] is None else f" page {item['page']}"
                print(f"{item['source']}{page} {item['step']}: {item['why']}")
            if not prepared.stale:
                print("nothing is stale")
            return 1 if prepared.stale or prepared.skipped else 0
        manifest = execute(prepared)
    except (PrepareError, OSError) as error:
        print(f"pagekit: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an unexpected failure is "cannot prepare", never a verdict
        print(f"pagekit: cannot prepare: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    for page in manifest["pages"]:
        print(f"{page['output']['name']}: {page['verdict']}")
        for flag in page["flags"]:
            print(f"  [{flag['step']}] {flag['reason']}")
    flagged = sum(1 for page in manifest["pages"] if page["flags"])
    skipped = len(manifest["skipped"])
    print(
        f"{len(manifest['pages'])} page(s) prepared, {flagged} to look at"
        + (f", {skipped} source file(s) skipped" if skipped else "")
        + f". Open {prepared.output_dir / REVIEW_NAME} to see them."
    )
    if not manifest["thresholds_measured"]:
        print(f"note: {manifest['thresholds_note']}")
    return 1 if flagged or skipped else 0


def _measure(arguments: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from pagekit.measure import measure, report_text
    from pagekit.project import PrepareError

    try:
        report = measure(Path(arguments.prepared), Path(arguments.gold))
    except (PrepareError, OSError) as error:
        print(f"pagekit: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an unexpected failure is "cannot measure"
        print(f"pagekit: cannot measure: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if arguments.json:
        sys.stdout.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    else:
        sys.stdout.write(report_text(report))
    return 0


def _tone(arguments: argparse.Namespace) -> int:
    overrides: dict = {}
    if arguments.grey_rule is not None:
        overrides["grey_rule"] = arguments.grey_rule
    if arguments.lift_strength is not None:
        overrides["lift_strength"] = arguments.lift_strength
    if arguments.sharpen:
        overrides["sharpen"] = 1
    try:
        view, record = tone_file(arguments.page, overrides)
        record["output"] = write_view(
            view,
            arguments.out,
            record["input"]["dpi"],
            source=arguments.page,
            force=arguments.force,
        )
    except (ToneError, OSError) as error:
        print(f"pagekit: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an unexpected failure is "cannot view", never a view
        print(f"pagekit: cannot make the view: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(record_json(record))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        return _prepare(arguments)
    if arguments.command == "measure":
        return _measure(arguments)
    if arguments.command == "tone":
        return _tone(arguments)
    overrides = {}
    if arguments.min_short_side_px is not None:
        overrides["min_short_side_px"] = arguments.min_short_side_px
    try:
        crops = [parse_box(text) for text in arguments.crop]
        report = check(arguments.master, crops, arguments.split_x, overrides)
    except (CheckError, OSError) as error:
        print(f"pagekit: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # an unexpected failure is "cannot check", never a verdict
        print(f"pagekit: cannot check: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if arguments.json:
        sys.stdout.write(report_json(report))
    else:
        print(f"{report['input']['master']['name']}: {report['verdict']}")
        for flag in report["flags"]:
            print(f"  [{flag['check']}] {flag['reason']}")
        if not report["thresholds_measured"]:
            print(f"  note: {report['thresholds_note']}")
    return 1 if report["flags"] else 0


if __name__ == "__main__":
    sys.exit(main())
