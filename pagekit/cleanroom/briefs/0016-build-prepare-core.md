# Brief 0016: build the preparation core (spec 0002)

- Role: build side.
- Issued by the host session to a fresh Claude Opus 5.5 build-side agent at
  2026-10-04T12:21:51Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  ee4b17a61f3991a7a1df8dd46e0a37402ab2992c400090fd4792fbd824bb36d0
- Saved by the host before the agent started.

----- brief below this line, exactly as sent -----
You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in /home/user/verbatus-worktrees/pk-core on branch work/pk-core. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** Build spec 0002, the preparation core: the detector-answer shape and its validation, the project file (`pagekit-project.v1`) with origins, inputs hashes, staleness and the overrides file (`pagekit-overrides.v1`), the geometry chain applied to the original in one resampling with forward and inverse point mapping, the output writer and manifest (`pagekit-prepare.v1`), and the `python -m pagekit prepare` command running with neutral defaults where no detector exists yet. Two other build-side agents are building the detectors of specs 0003 and 0004 in parallel in other worktrees; a later slice will connect them, so expose a clear way to call a detector for a step and store its answer. pagekit's only dependency is Pillow: build with Pillow and the Python standard library, and do not add numpy or any other package; if a method is too slow without one, choose a simpler method on a reduced working copy and say so in your report. Real scans are about 3000 by 4500 pixels, so every detector must work on reduced copies and finish a page in a few seconds.

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: `pagekit/cleanroom/specs/0002-prepare-core.md`;
- finding reports: `pagekit/cleanroom/findings/0014-resolution-metadata.md`, `0020-margins-padding.md`, `0021-blank-page.md` (read together with entry 0014 of `pagekit/cleanroom/LOG.md`, which corrects it), `0031-resample-once.md`, `0033-manual-overrides.md` and `0034-coordinate-provenance.md`, all in `pagekit/cleanroom/findings/`;
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** New modules under `pagekit/` for this slice (for example `answer.py`, `project.py`, `geometry.py`, `output.py`, `prepare.py`) and their tests; the `prepare` command in `pagekit/__main__.py`; a new thresholds file `pagekit/thresholds_prepare.toml`; `pagekit/pyproject.toml` (package data only); `pagekit/README.md` (a new section on `prepare`) and `pagekit/NOTICE`. Do not change `pagekit/check.py` or its thresholds except to import from it, and do not touch `pagekit/cleanroom/`. Tests use
synthetic images built in the test, never real register material.

**Deliverable.** Code, tests that pin every behaviour listed in the spec, and a README section on `prepare`. The spec is not changed by the build;
if the spec is wrong or unclear, decide, record the decision and its reason in the
commit message, and name it in your report.

**Checks.** `.venv/bin/python -m pytest -q -p xdist -n 2 pagekit` and
`sh .githooks/check-static.sh`; write output to files and check exit codes. Never skip
commit hooks.

**Commit messages** end with:

    Clean-room: build
    [Clean-room-finding: NNNN, one line per finding report that informed the commit]
    Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

**Stop conditions.** Stop and report if you see ScanTailor or other GPL page-processing
source, if the work needs anything outside the allowed sources, or if a check fails in
a way you cannot fix inside the allowed paths.

**Report** concisely: commits, what each change does and its limits, check exit codes,
and anything you could not do.
