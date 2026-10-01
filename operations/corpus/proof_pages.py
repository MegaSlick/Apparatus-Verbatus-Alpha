"""Copy a declared, deterministic list of proof pages into a private submission folder.

The pages come from an admitted RecordGold set (`local_admission.py`'s ledger)
and are chosen one of two declared ways, never by looking at any reading:

- `--page-sha`, repeated: exactly these admitted page digests;
- `--count N --seed TEXT`: the N admitted pages that rank first by
  `sha256(seed + ":" + page sha256)`, so the same ledger, count and seed always
  choose the same pages.

Each chosen image is copied (never moved or linked) and checked against its
digest into `<output>/pages/`, beside a Door-ready `submission-manifest.json`,
the chosen pages' reference truth (`reference-pages.jsonl`, the scorers'
input) and `selection.json`, which records the ledger, the rule and the chosen
digests. Everything stays on this machine: the source must be outside the
repository, and an output inside it must be under `private/`. The chosen
digests are printed, one per line.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from common.contracts.canonical import canonical_bytes, digest_bytes, self_hash
from operations.submit.submit import build_manifest, walk_folder

from . import CorpusRefusal
from .local_admission import load_local_admission_ledger

SCHEMA = "recordgold-proof-pages.v1"
REPOSITORY = Path(__file__).resolve().parents[2]

PROOF_PAGES_REFUSAL_REASONS = frozenset(
    {
        "ambiguous-page",
        "count-out-of-range",
        "digest-mismatch",
        "no-selection-rule",
        "output-not-empty",
        "output-not-private",
        "page-not-admitted",
        "source-inside-repository",
    }
)


class Refusal(CorpusRefusal):
    reasons = PROOF_PAGES_REFUSAL_REASONS


def _rank(seed: str, page_sha256: str) -> str:
    return hashlib.sha256(f"{seed}:{page_sha256}".encode()).hexdigest()


def admitted_pages(ledger: dict[str, Any]) -> dict[str, dict[str, str]]:
    """`{page sha256: {page_id, page_image}}` for every page with an admitted record."""
    pages: dict[str, dict[str, str]] = {}
    for row in ledger["rows"]:
        if row["decision"] != "admitted":
            continue
        page = {"page_id": row["page_id"], "page_image": row["page_image"]}
        if pages.setdefault(row["page_sha256"], page) != page:
            raise Refusal(
                f"ambiguous-page: page sha256 {row['page_sha256']} names more than one image"
            )
    return pages


def choose(
    pages: Sequence[str],
    *,
    page_shas: Sequence[str] = (),
    count: int | None = None,
    seed: str | None = None,
) -> list[str]:
    """The chosen digests, sorted: the declared list, or the first `count` by seeded rank."""
    if bool(page_shas) == (count is not None or seed is not None):
        raise Refusal("no-selection-rule: name pages with --page-sha, or give --count and --seed")
    if page_shas:
        unknown = sorted(set(page_shas) - set(pages))
        if unknown or len(set(page_shas)) != len(page_shas):
            raise Refusal(
                f"page-not-admitted: {unknown[0] if unknown else 'a repeated digest'} is not "
                "one admitted page"
            )
        return sorted(page_shas)
    if count is None or seed is None or not seed:
        raise Refusal("no-selection-rule: --count and --seed go together")
    if not 0 < count <= len(pages):
        raise Refusal(f"count-out-of-range: {count} of {len(pages)} admitted pages")
    return sorted(sorted(pages, key=lambda sha: (_rank(seed, sha), sha))[:count])


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def pick(
    ledger_path: str | Path,
    output_root: str | Path,
    *,
    set_root: str | Path | None = None,
    page_shas: Sequence[str] = (),
    count: int | None = None,
    seed: str | None = None,
) -> dict[str, Any]:
    """Copy the chosen pages into `output_root` and return its `selection.json` body."""
    ledger = load_local_admission_ledger(ledger_path)
    source = Path(set_root if set_root is not None else ledger["set_root"]).resolve()
    output = Path(output_root).resolve()
    if _inside(source, REPOSITORY):
        raise Refusal("source-inside-repository: the RecordGold set must be outside the repository")
    if _inside(output, REPOSITORY) and not _inside(output, REPOSITORY / "private"):
        raise Refusal("output-not-private: an output inside the repository must be under private/")
    if output.exists() and any(output.iterdir()):
        raise Refusal(f"output-not-empty: {output} already holds files")

    pages = admitted_pages(ledger)
    chosen = choose(sorted(pages), page_shas=page_shas, count=count, seed=seed)
    copies = []
    for sha in chosen:
        image = (source / pages[sha]["page_image"]).resolve()
        if not image.is_relative_to(source) or image.is_symlink() or not image.is_file():
            raise Refusal(f"page-not-admitted: page {sha} has no regular image in the set")
        if digest_bytes(image.read_bytes()) != sha:
            raise Refusal(f"digest-mismatch: {image.name} does not hash to {sha}")
        copies.append((sha, image))

    folder = output / "pages"
    folder.mkdir(parents=True, exist_ok=True)
    for sha, image in copies:
        shutil.copyfile(image, folder / f"{sha}{image.suffix}")
        if digest_bytes((folder / f"{sha}{image.suffix}").read_bytes()) != sha:
            raise Refusal(f"digest-mismatch: the copy of {sha} does not hash to it")
    (output / "submission-manifest.json").write_bytes(
        canonical_bytes(build_manifest(walk_folder(folder)))
    )
    references = sorted(
        (page for page in ledger["reference_pages"] if page["page"]["sha256"] in set(chosen)),
        key=lambda page: page["page"]["sha256"],
    )
    (output / "reference-pages.jsonl").write_bytes(
        b"".join(canonical_bytes(page) + b"\n" for page in references)
    )
    selection = {
        "schema": SCHEMA,
        "ledger_self_hash": ledger["self_hash"],
        "split": ledger["split"],
        "rule": (
            {"declared": sorted(page_shas)}
            if page_shas
            else {"count": count, "seed": seed, "rank": "sha256(seed:page_sha256)"}
        ),
        "admitted_pages": len(pages),
        "pages": [{"page_sha256": sha, "page_id": pages[sha]["page_id"]} for sha in chosen],
    }
    selection["self_hash"] = self_hash(selection)
    (output / "selection.json").write_bytes(canonical_bytes(selection))
    return selection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", required=True, type=Path, help="the set's admission ledger")
    parser.add_argument(
        "--set-root", type=Path, help="the RecordGold set (default: the ledger's own set_root)"
    )
    parser.add_argument("--page-sha", action="append", default=[], help="a page to take")
    parser.add_argument("--count", type=int, help="how many pages to draw")
    parser.add_argument("--seed", help="the declared seed the draw ranks pages by")
    parser.add_argument(
        "--output-root", required=True, type=Path, help="a new folder, e.g. private/proof/<name>"
    )
    args = parser.parse_args(argv)
    selection = pick(
        args.ledger,
        args.output_root,
        set_root=args.set_root,
        page_shas=args.page_sha,
        count=args.count,
        seed=args.seed,
    )
    for page in selection["pages"]:
        sys.stdout.write(page["page_sha256"] + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
