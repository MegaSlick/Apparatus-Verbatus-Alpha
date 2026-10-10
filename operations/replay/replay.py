"""Replay a saved run's Perlector from its recorded replies, with the current code, as a new run.

    python operations/replay/replay.py --source <runs>/<run-id> \\
        --run-root <dir> --run-id <new-id> [--to <stage>] -- <orchestrator arguments>

The new run (`common.replay`) takes the source's stages up to and including the
Attestatores as they were sealed, then the orchestrator runs it from the Perlector on:
every page read again by this checkout's code, every Perlector and Coniector call
answered from the reply the source run retained for exactly that request, and no model
called. Its `run.json` names the source run, the source's commit and this checkout's
commit. The source run is only read.

The orchestrator arguments are those the source run was driven with (the real
configuration files, `--placement-tier`, `--capacity-plan`, `--mechanics-qualification`
and so on); the stages refuse any sealed configuration that differs from the source's.
This program supplies the run root, run id and stage selection itself.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.contracts.errors import ContractError  # noqa: E402
from common.contracts.stages import (  # noqa: E402
    CONIECTOR,
    PERLECTOR,
    SEAL_PREDECESSORS,
    STAGES,
    writing_directory,
)
from common.replay import (  # noqa: E402
    IMPORTED_STAGES,
    SOURCE_ENV,
    replay_block,
)
from common.runtree.store import RECEIPTS_DIR, RunTree  # noqa: E402
from common.stage import verify_stage_seal  # noqa: E402

ORCHESTRATOR = ROOT / "pipeline" / "orchestrator" / "run.py"
# Chosen by this program; a caller naming them would replay into some other run.
_OWN_FLAGS = (
    "--run-root",
    "--run-id",
    "--from",
    "--to",
    "--stage",
    "--all",
    "--mode",
    "--repository-commit",
    "--submission-folder",
    "--submission-manifest",
)


def source_tree(source: Path) -> RunTree:
    """The source run, opened read-only: its authority read and its replayed seals proven."""
    source = source.absolute()
    tree = RunTree(source.parent, source.name)
    tree.read_run()
    for consumer, producer in SEAL_PREDECESSORS.items():
        if producer in (*IMPORTED_STAGES, PERLECTOR) and consumer in STAGES:
            verify_stage_seal(tree, producer, consumer)
    return tree


def checkout_commit() -> str:
    """This checkout's commit, refused when tracked files differ from it."""
    try:
        head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        changed = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise ContractError(
            f"the code's commit could not be read ({error}); name it with --repository-commit"
        ) from error
    if changed:
        raise ContractError(
            "this checkout has uncommitted changes, so no commit names the code that would "
            "replay; commit them, or name the commit with --repository-commit"
        )
    return head


def _clone(source: Path, target: Path) -> None:
    """Copy a directory tree, as a copy-on-write clone where the filesystem offers one."""
    if sys.platform == "darwin":
        cloned = subprocess.run(["cp", "-cR", str(source), str(target)], capture_output=True)
        if cloned.returncode == 0:
            return
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, symlinks=True)


def prepare(source: RunTree, run_root: Path, run_id: str, repository_commit: str) -> RunTree:
    """Create the replay run: its authority, then the source's imported stages and receipts.

    The imported stages' manifests are derived inventories, written again for this
    run; every artifact and blob is the source's byte for byte, and each imported
    seal is proven again in the new tree before anything reads it.
    """
    authority = source.read_run()
    tree = RunTree.create_replay(
        run_root,
        run_id,
        source=authority,
        replay=replay_block(authority),
        repository_commit=repository_commit,
    )
    directories = sorted({writing_directory(stage) for stage in IMPORTED_STAGES})
    for directory in [*directories, str(Path(RECEIPTS_DIR).parent)]:
        found = source.root / directory
        if found.exists():
            (tree.root / directory).parent.mkdir(parents=True, exist_ok=True)
            _clone(found, tree.root / directory)
    for stage in IMPORTED_STAGES:
        tree.write_manifest(stage)
    for consumer, producer in SEAL_PREDECESSORS.items():
        if producer in IMPORTED_STAGES and consumer in STAGES:
            verify_stage_seal(tree, producer, consumer)
    return tree


def orchestrate(source: RunTree, tree: RunTree, to_stage: str, arguments: list[str]) -> int:
    """Run the orchestrator over the new run from the Perlector, answering from the source."""
    named = sorted({flag for flag in _OWN_FLAGS for argument in arguments if argument == flag})
    if named:
        raise ContractError(f"{', '.join(named)} are chosen by the replay itself; leave them out")
    command = [
        sys.executable,
        "-I",
        "-u",
        str(ORCHESTRATOR),
        *arguments,
        "--run-root",
        str(tree.root.parent),
        "--run-id",
        tree.run_id,
        "--from",
        PERLECTOR,
        "--to",
        to_stage,
    ]
    environment = {**os.environ, SOURCE_ENV: str(source.root)}
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path, help="the saved run's directory")
    parser.add_argument("--run-root", required=True, type=Path, help="where the new run goes")
    parser.add_argument("--run-id", required=True, help="the new run's id")
    parser.add_argument(
        "--to",
        default="armarium",
        choices=[stage for stage in STAGES if STAGES.index(stage) >= STAGES.index(CONIECTOR)],
        help="the last stage to run (default armarium; a held Recensor stops the run anyway)",
    )
    parser.add_argument(
        "--repository-commit",
        default=None,
        help="the commit of this checkout's code, when git cannot read a clean one",
    )
    parser.add_argument("orchestrator", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    arguments = args.orchestrator[1:] if args.orchestrator[:1] == ["--"] else args.orchestrator
    try:
        commit = args.repository_commit or checkout_commit()
        source = source_tree(args.source)
        run_root = args.run_root.absolute()
        if (run_root / args.run_id).resolve().is_relative_to(source.root):
            raise ContractError("the replay run may not be written inside its source run")
        tree = prepare(source, run_root, args.run_id, commit)
    except ContractError as error:
        print(f"replay refused: {error}", file=sys.stderr)
        return 2
    print(
        f"replay: run {tree.run_id} replays run {source.run_id} at {commit} from its recorded "
        f"replies; the orchestrator runs it from the Perlector to the {args.to}",
        flush=True,
    )
    return orchestrate(source, tree, args.to, arguments)


if __name__ == "__main__":
    raise SystemExit(main())
