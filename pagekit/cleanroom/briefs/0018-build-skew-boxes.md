# Brief 0018: build skew and the page and content boxes (spec 0004)

- Role: build side.
- Issued by the host session to a fresh Claude Opus 5.5 build-side agent at
  2026-10-04T12:21:51Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  6e5f0d64c0da25b49919a6cb9bd6550313876473f5f2dc7972770cdb8a2b92b0
- Saved by the host before the agent started.

----- brief below this line, exactly as sent -----
You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in /home/user/verbatus-worktrees/pk-skew on branch work/pk-skew. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** Build spec 0004: the skew detector, the page-box detector and the content-box detector (with blank pages), each working on reduced working copies of one page and returning an answer, never writing a file. Every detector returns a plain dict with exactly the keys value, confidence, evidence and flags, as spec 0002 defines (the preparation core is being built at the same time in another worktree; do not wait for it and do not import from it). pagekit's only dependency is Pillow: build with Pillow and the Python standard library, and do not add numpy or any other package; if a method is too slow without one, choose a simpler method on a reduced working copy and say so in your report. Real scans are about 3000 by 4500 pixels, so every detector must work on reduced copies and finish a page in a few seconds.

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: `pagekit/cleanroom/specs/0004-skew-and-boxes.md`;
- finding reports: `0009-deskew.md`, `0010-deskew-distractors.md`, `0012-skew-limits.md`, `0015-page-box.md`, `0016-content-box.md`, `0018-ruled-lines.md`, `0019-scanning-targets.md`, `0020-margins-padding.md`, `0021-blank-page.md` (read together with entry 0014 of `pagekit/cleanroom/LOG.md`, which corrects it) and `0023-uneven-illumination.md`, all in `pagekit/cleanroom/findings/`;
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** New modules `pagekit/skew.py`, `pagekit/pagebox.py` and `pagekit/content.py`, any helper module of this slice whose name starts with `pagekit/_skew_` or `pagekit/_box_`, their tests, and a new thresholds file `pagekit/thresholds_skew.toml`; add your citations to `pagekit/NOTICE` under a heading for skew and boxes. Do not change `pagekit/check.py` (importing from it is fine), `pagekit/__main__.py`, `pagekit/README.md` or `pagekit/cleanroom/`. Tests use
synthetic images built in the test, never real register material.

**Deliverable.** Code, and tests that pin every behaviour listed in the spec on synthetic pages built in the tests. The spec is not changed by the build;
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
