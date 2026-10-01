"""A real submission meets the Designator.

A genuine real submission -- the synthetic fixture's own two pages copied into an
approved storage root and admitted through `operations/submit/submit.py` -- is
driven by the real Door, Exemplar and Ink Map programs. The catalogue this
module writes keeps the Designator's detectors on their fixture rows, so the
Designator refuses the run by name ("a fixture row answers only a synthetic
run") and writes nothing; the test below drives its program to exactly that
refusal.

Nothing here starts a pod, opens a socket, loads a model or reaches a network.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
ROOT = PIPELINE.parents[0]
ATTESTATORES_DIR = PIPELINE / "3_attestatores"
# The Attestatores' own real-ingress suite imports its stage siblings by bare
# name; its directory has to be importable before that module is loaded here.
for _directory in (PIPELINE, ATTESTATORES_DIR):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

# Two suites' helpers, reused rather than copied.
#
# `test_attestatores_real_ingress` owns the real-submission builder and the
# hand-built Designator layer; `RUN_ID` and `ACTS` come with them, and the
# witness scripts are sized by that same act table, so the two cannot drift
# apart. `test_live_reading_seam_e2e` owns the serving world: the catalogue
# writer that makes every chair live at every tier, the scripted witness and
# reader worlds, the tree snapshotter, and the two stage programs loaded for
# in-process invocation — so a change in how a fake chair is stood up is made
# once for both seams.
from test_attestatores_real_ingress import (  # noqa: E402
    RUN_ID,
    _real_submission,
)
from test_live_reading_seam_e2e import (  # noqa: E402
    TIER,
    snapshot,
    write_live_catalogue,
)

from common.chairs.registry import ChairRegistry  # noqa: E402
from common.stage import (  # noqa: E402
    EXIT_FATAL,
)

MODELS_CONFIG = ROOT / "config" / "models.toml"
DESIGNATOR_CLI = PIPELINE / "2_designator" / "run.py"
# --------------------------------- driving ----------------------------------


def stage_argv(run_root: Path, catalogue: Path, *extra: str) -> list[str]:
    """The flags every stage of this run is given, the Door included.

    Only the roster and the catalogue are named explicitly: every other sealed
    input is `config/`'s own file at `stage_parser`'s default, so the Door and
    each later open recompute the same digest from the same bytes. Naming them
    a second time here would only create a place for the two to disagree.
    """
    return [
        "--run-root",
        str(run_root),
        "--run-id",
        RUN_ID,
        "--models-config",
        str(MODELS_CONFIG),
        "--serving-recipes-config",
        str(catalogue),
        *extra,
    ]


def invoke_stage(program: Path, run_root: Path, catalogue: Path, *extra: str):
    """Run one stage program as a real subprocess, and return the whole result."""
    return subprocess.run(
        [sys.executable, str(program), *stage_argv(run_root, catalogue, *extra)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def run_in_process(module, run_root: Path, catalogue: Path, *, serving_factory):
    """Call one stage's own `main` here, with the serving seam injected.

    `main(serving_factory=…)` is the sanctioned in-process injection point and
    is not what makes a run live: the sealed row kind decides that.
    `sys.argv[0]` is the stage's own program path, as it would be under the
    orchestrator, so a refusal names a file that exists.
    """
    argv = [module.__file__, *stage_argv(run_root, catalogue, "--placement-tier", TIER)]
    original, sys.argv = sys.argv, argv
    try:
        return module.main(serving_factory=serving_factory)
    finally:
        sys.argv = original


# ========================= what a real run cannot do =========================


def test_the_designator_refuses_fixture_detectors_on_a_real_submission_and_writes_nothing(
    tmp_path,
):
    """A fixture row answers only a synthetic run, which declares its boxes.

    The catalogue here keeps the record detector and Surya on fixture rows, so the
    Designator refuses before it publishes anything: the tree it leaves is byte
    for byte what the Ink Map sealed.
    """
    registry = ChairRegistry.from_toml(str(MODELS_CONFIG))
    catalogue = write_live_catalogue(tmp_path / "serving_recipes_live.toml", registry)
    run_root = _real_submission(
        tmp_path, "--serving-recipes-config", str(catalogue), "--models-config", str(MODELS_CONFIG)
    )
    before = snapshot(run_root)

    result = invoke_stage(DESIGNATOR_CLI, run_root, catalogue)

    assert result.returncode == EXIT_FATAL, result.stderr
    assert "a fixture row answers only a synthetic run" in result.stderr
    assert snapshot(run_root) == before, "a refused Designator writes nothing"
