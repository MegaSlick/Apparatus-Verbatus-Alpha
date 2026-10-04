# Brief 0017: build orientation and the split (spec 0003)

- Role: build side.
- Issued by the host session to a fresh Claude Opus 5.5 build-side agent at
  2026-10-04T12:21:51Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  00ece8d673cd968a239b37012b530b54175235902612d127be3092269ef447c3
- Saved by the host before the agent started.

----- brief below this line, exactly as sent -----
You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in /home/user/verbatus-worktrees/pk-split on branch work/pk-split. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** Build spec 0003: the orientation detector and the page-count-and-split detector, each working on reduced working copies of a source image and returning an answer, never writing a file. Every detector returns a plain dict with exactly the keys value, confidence, evidence and flags, as spec 0002 defines (the preparation core is being built at the same time in another worktree; do not wait for it and do not import from it). pagekit's only dependency is Pillow: build with Pillow and the Python standard library, and do not add numpy or any other package; if a method is too slow without one, choose a simpler method on a reduced working copy and say so in your report. Real scans are about 3000 by 4500 pixels, so every detector must work on reduced copies and finish a page in a few seconds.

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: `pagekit/cleanroom/specs/0003-orientation-and-split.md`;
- finding reports: `0001-orientation-quarter-turn.md`, `0002-orientation-upside-down.md`, `0003-spread-or-single.md`, `0004-gutter-line.md`, `0005-gap-split.md`, `0006-slanted-split.md`, `0007-neighbour-offcut.md`, `0008-gutter-marginalia.md`, `0013-polarity.md` and `0021-blank-page.md` (read together with entry 0014 of `pagekit/cleanroom/LOG.md`, which corrects it), all in `pagekit/cleanroom/findings/`;
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** New modules `pagekit/orient.py` and `pagekit/split.py`, any helper module of this slice whose name starts with `pagekit/_split_` or `pagekit/_orient_`, their tests, and a new thresholds file `pagekit/thresholds_split.toml`; add your citations to `pagekit/NOTICE` under a heading for orientation and split. Do not change `pagekit/check.py` (importing from it is fine), `pagekit/__main__.py`, `pagekit/README.md` or `pagekit/cleanroom/`. Tests use
synthetic images built in the test, never real register material.

**Deliverable.** Code, and tests that pin every behaviour listed in the spec on synthetic pages built in the tests (lines of word-like ink shapes with ascenders and descenders, left-aligned with ragged right ends). The spec is not changed by the build;
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
