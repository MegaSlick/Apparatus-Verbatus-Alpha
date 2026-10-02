"""`python -m pagekit check --master PATH --crop x0,y0,x1,y1 [--crop ...] [--split-x N]`.

Exit status: 0 when nothing is flagged, 1 when the page should go to review, 2 when
the inputs cannot be checked.
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
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    overrides = {}
    if arguments.min_short_side_px is not None:
        overrides["min_short_side_px"] = arguments.min_short_side_px
    try:
        crops = [parse_box(text) for text in arguments.crop]
        report = check(arguments.master, crops, arguments.split_x, overrides)
    except (CheckError, OSError) as error:
        print(f"pagekit: {error}", file=sys.stderr)
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
