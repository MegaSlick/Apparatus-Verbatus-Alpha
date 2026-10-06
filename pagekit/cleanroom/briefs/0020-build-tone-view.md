# Brief 0020: build the grey tone view (spec 0006)

- Role: build side.
- Issued by the host session to a fresh Claude Fable 5.1 build-side agent at
  2026-10-04T12:53:08Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  f41843953ca62b3a264f68540239ebbd8d13554aa8606682626e34cb885f3760
- Saved by the host before the agent started.

----- brief below this line, exactly as sent -----
You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in /home/user/verbatus-worktrees/pk-tone on branch work/pk-tone. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** Build spec 0006, the grey tone view: a function that takes a page image (grey or colour) and returns a grey image of exactly the same size with the lighting flattened and faint ink gently lifted, never moving a pixel and never binarising, plus the record of what it did and the `python -m pagekit tone` command writing lossless TIFF. The view is for readers that see the page in grey (the reader model's grey render and an ink measure), so it must be gentle: published work found binarisation, denoising and local histogram equalisation hurt vision-language readers of historical handwriting, and only mild sharpening helped. Correctness first: no stroke darker than its paper may become indistinguishable from it, the tone curve must be monotone without clipping, and the record must say when flattening was skipped. pagekit's only dependency is Pillow: build with Pillow and the Python standard library, and do not add numpy or any other package; if a method is too slow without one, choose a simpler method on a reduced working copy and say so in your report. Real scans are about 3000 by 4500 pixels, so every detector must work on reduced copies and finish a page in a few seconds.

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: `pagekit/cleanroom/specs/0006-grey-tone-view.md`;
- finding reports: `0023-uneven-illumination.md`, `0024-paper-colour-cast.md`, `0025-binarisation.md`, `0030-bleed-through.md` and `0032-faint-ink-tone.md`, all in `pagekit/cleanroom/findings/`;
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** New module `pagekit/tone.py`, any helper module of this slice whose name starts with `pagekit/_tone_`, their tests, a new thresholds file `pagekit/thresholds_tone.toml`, the `tone` command in `pagekit/__main__.py` (add it beside `check`; another agent may add a `prepare` command there at the same time, so keep your change to one new subcommand), and a new heading in `pagekit/NOTICE` for the tone view's citations. Do not change `pagekit/check.py`, `pagekit/README.md` or `pagekit/cleanroom/`. Tests use
synthetic images built in the test, never real register material.

**Deliverable.** Code, and tests that pin every measure listed in the spec on synthetic pages built in the tests, including the colour (brown ink on yellow paper) cases. The spec is not changed by the build;
if the spec is wrong or unclear, decide, record the decision and its reason in the
commit message, and name it in your report.

**Checks.** `.venv/bin/python -m pytest -q -p xdist -n 2 pagekit` and
`sh .githooks/check-static.sh`; write output to files and check exit codes. Never skip
commit hooks.

**Commit messages** end with:

    Clean-room: build
    [Clean-room-finding: NNNN, one line per finding report that informed the commit]
    Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>

**Stop conditions.** Stop and report if you see ScanTailor or other GPL page-processing
source, if the work needs anything outside the allowed sources, or if a check fails in
a way you cannot fix inside the allowed paths.

**Report** concisely: commits, what each change does and its limits, check exit codes,
and anything you could not do.
