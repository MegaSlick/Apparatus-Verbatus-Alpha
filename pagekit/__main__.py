"""`python -m pagekit check ...` and `python -m pagekit prepare ...`.

check: `--master PATH --crop x0,y0,x1,y1 [--crop ...] [--split-x N]`. Exit status 0
when nothing is flagged, 1 when the page should go to review, 2 when the inputs cannot
be checked.

prepare: `[SOURCE ...] --output DIR [--project FILE] [--overrides FILE]
[--report-stale]`. Exit status 0 when no page is flagged, 1 when any page needs review
(with --report-stale: when any step is stale), 2 when the input cannot be used, and then
nothing is written.
"""

from __future__ import annotations

import argparse
import sys

from pagekit.check import CheckError, check, parse_box, report_json


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
    return parser


def _prepare(arguments: argparse.Namespace) -> int:
    from pagekit.output import execute
    from pagekit.prepare import plan
    from pagekit.project import PrepareError

    try:
        prepared = plan(
            arguments.sources,
            arguments.output,
            arguments.project,
            arguments.overrides,
            dry=arguments.report_stale,
        )
        if arguments.report_stale:
            for item in prepared.stale:
                page = "" if item["page"] is None else f" page {item['page']}"
                print(f"{item['source']}{page} {item['step']}: {item['why']}")
            if not prepared.stale:
                print("nothing is stale")
            return 1 if prepared.stale else 0
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
    if not manifest["thresholds_measured"]:
        print(f"note: {manifest['thresholds_note']}")
    return 1 if any(page["flags"] for page in manifest["pages"]) else 0


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare":
        return _prepare(arguments)
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
