"""`python -m pagekit check --master PATH --crop x0,y0,x1,y1 [--crop ...] [--split-x N]`
and `python -m pagekit tone --in PAGE --out VIEW.tif`.

Exit status of `check`: 0 when nothing is flagged, 1 when the page should go to review,
2 when the inputs cannot be checked. `tone` exits 0 when the view was written and its
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
    toner = commands.add_parser(
        "tone", help="write the grey tone view of a page as lossless TIFF and print its record"
    )
    toner.add_argument("--in", dest="page", required=True, help="the page image; only read")
    toner.add_argument("--out", required=True, help="the view to write; .tif or .tiff")
    toner.add_argument("--grey-rule", choices=GREY_RULES, help="how a colour page becomes grey")
    toner.add_argument("--lift-strength", type=float, help="faint-ink lift, 0 (off) to 0.4")
    toner.add_argument("--sharpen", action="store_true", help="apply the mild unsharp mask")
    return parser


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
        record["output"] = write_view(view, arguments.out, record["input"]["dpi"])
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
