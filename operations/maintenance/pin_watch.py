"""Report upstream movement for configured vendor model and source pins."""

from __future__ import annotations

import argparse
import ast
import json
import tomllib
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from operations.notify import client

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Pin:
    repo: str
    revision: str
    host: str


def _get_json(url: str) -> Mapping[str, object]:
    request = urllib.request.Request(url, headers={"User-Agent": "verbatus-pin-watch"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _pins(config_paths: tuple[Path, ...], vendor_path: Path) -> list[Pin]:
    pins: list[Pin] = []
    for path in config_paths:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        for _chair, row in raw.get("chairs", {}).items():
            if row.get("state") == "configured" and row.get("source") in {"huggingface", "github"}:
                pins.append(Pin(row["repo"], row["revision"], row["source"]))

    tree = ast.parse(vendor_path.read_text(encoding="utf-8"), filename=str(vendor_path))
    constants: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            try:
                constants[node.target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    for repository, revision in (
        ("CHANDRA_CODE_REPOSITORY", "CHANDRA_CODE_COMMIT"),
        ("CHURRO_CODE_REPOSITORY", "CHURRO_CODE_COMMIT"),
    ):
        repo = str(constants[repository]).removeprefix("github.com/")
        pins.append(Pin(repo, str(constants[revision]), "github"))
    return pins


def _upstream(pin: Pin, get_json: Callable[[str], Mapping[str, object]]) -> tuple[str, str]:
    if pin.host == "huggingface":
        data = get_json(f"https://huggingface.co/api/models/{pin.repo}")
        return str(data["sha"]), str(data["lastModified"])
    data = get_json(f"https://api.github.com/repos/{pin.repo}/commits/HEAD")
    commit = data["commit"]
    if not isinstance(commit, Mapping):
        raise ValueError("GitHub response has no commit object")
    committer = commit["committer"]
    if not isinstance(committer, Mapping):
        raise ValueError("GitHub response has no committer object")
    return str(data["sha"]), str(committer["date"])


def report(
    pins: list[Pin], get_json: Callable[[str], Mapping[str, object]]
) -> tuple[list[str], list[str], list[str]]:
    """Every pin's status line, then the moved lines and the unreachable lines."""
    lines: list[str] = []
    moved: list[str] = []
    unreachable: list[str] = []
    for pin in pins:
        try:
            head, date = _upstream(pin, get_json)
            if head == pin.revision:
                lines.append(f"{pin.repo}: unchanged ({pin.revision})")
            else:
                lines.append(
                    f"{pin.repo}: upstream-moved (pinned {pin.revision}, upstream {head}, {date})"
                )
                moved.append(lines[-1])
        except Exception as error:  # noqa: BLE001 -- every public API failure is an unreachable pin
            lines.append(f"{pin.repo}: unreachable ({type(error).__name__})")
            unreachable.append(lines[-1])
    return lines, moved, unreachable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notify",
        action="store_true",
        help="send one decision notification if any pin moved or could not be checked",
    )
    args = parser.parse_args()
    configs = (ROOT / "config/models.toml", ROOT / "config/models-real.toml")
    try:
        pins = _pins(configs, ROOT / "common/test_vendor_parity.py")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, SyntaxError, KeyError) as error:
        print(f"pin-watch: could not read pin configuration: {error}")
        return 1
    lines, moved, unreachable = report(pins, _get_json)
    print("\n".join(lines))
    if args.notify and (moved or unreachable):
        parts = []
        if moved:
            parts.append("Vendor pins moved: " + "; ".join(moved))
        if unreachable:
            parts.append("Vendor pins not checked: " + "; ".join(unreachable))
        outcome = client.send("decision", " | ".join(parts))
        if not outcome.delivered:
            print(outcome.line())
            return 1
    return 1 if unreachable else 0


if __name__ == "__main__":
    raise SystemExit(main())
