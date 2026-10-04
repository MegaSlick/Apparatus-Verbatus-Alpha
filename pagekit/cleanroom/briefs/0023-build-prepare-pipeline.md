# Brief 0023: build the prepare pipeline and the review sheet (spec 0005)

- Role: build side.
- Issued by the host session to a fresh Claude Opus 5.5 build-side agent at
  2026-10-04T13:20:40Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  931a7ee1bbc1b1cf089629941ee1079102d17877c59ea862d45aa434e716c769
- Saved by the host before the agent started.

----- brief below this line, exactly as sent -----
You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in /home/user/verbatus-worktrees/pk-integrate on branch work/pk-integrate. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** Build spec 0005: connect the detectors already on this branch (orientation and split from `orient.py` and `split.py`; skew, page box and content box from `skew.py`, `pagebox.py` and `content.py`) to the preparation core (`prepare.py`, `project.py`, `geometry.py`, `output.py`) so `python -m pagekit prepare` runs every step in order on a folder of scans; add the volume-wide checks, the self-contained `review.html` review sheet, the `python -m pagekit measure` command, the output defaults in spec 0005 (lossless TIFF by default, source colour mode kept, no shrinking by default), and a `prepare` option that also writes the grey tone view of spec 0006 beside each page once `pagekit/tone.py` exists on this branch (if it does not exist yet, leave a clearly named hook and say so). The detectors return plain answers with value, confidence, evidence and flags; adapt them to the core's detector interface without changing what they decide. The person using this is not a programmer: every message, the review sheet and the README must be plain and short, and the review sheet must work offline in any browser on a laptop or phone. It must run on macOS (Intel and Apple silicon) and Linux with Python 3.12 or later: pathlib only, no shell-outs. A full-size page should take a few seconds. pagekit's only dependency is Pillow: build with Pillow and the Python standard library, and do not add numpy or any other package; if a method is too slow without one, choose a simpler method on a reduced working copy and say so in your report. Real scans are about 3000 by 4500 pixels, so every detector must work on reduced copies and finish a page in a few seconds.

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: `pagekit/cleanroom/specs/0005-prepare-pipeline-and-review.md`;
- finding reports: `0011-skew-outliers.md`, `0033-manual-overrides.md` and `0035-measuring-success.md`, all in `pagekit/cleanroom/findings/`; also read specs 0002, 0003, 0004 and 0006 in `pagekit/cleanroom/specs/`, which describe the parts you connect;
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** Any file under `pagekit/` except `pagekit/check.py` (importing from it is fine) and `pagekit/cleanroom/`; you may make small, clearly explained changes inside the detectors' modules only to adapt their interfaces, never to change their decisions. Update `pagekit/pyproject.toml` package data so every thresholds file ships, and `pagekit/README.md`. Tests use
synthetic images built in the test, never real register material.

**Deliverable.** Code, tests that pin every behaviour listed in spec 0005 on synthetic pages, an end-to-end test that prepares a synthetic batch (upright, sideways, upside-down, spread, tilted and blank pages) and checks every decision and the review sheet, and the README. The spec is not changed by the build;
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
