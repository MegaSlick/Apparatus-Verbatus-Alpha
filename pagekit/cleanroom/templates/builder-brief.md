# Builder brief template

The host makes every build-side brief from this template, filling only the bracketed
parts, from the slice's spec and from finding reports that passed the report check.
Nothing else goes in: no notes of the host's own about how another program works. The
host saves the finished brief as `pagekit/cleanroom/briefs/NNNN-build-[topic].md`
(header, then the marker line, then the brief exactly as sent) and logs its sha256 in
LOG.md before the agent starts. See CLEANROOM.md.

Copy everything below this line.

---

You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in [worktree path] on branch [branch]. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** [One paragraph: the slice and what it must do.]

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: `pagekit/cleanroom/specs/[NNNN-name].md`;
- finding reports: [list of `pagekit/cleanroom/findings/NNNN.md`, or "none"];
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** [Paths under pagekit/ the slice may change.] Tests use
synthetic images built in the test, never real register material.

**Deliverable.** [Code, tests, README changes.] The spec is not changed by the build;
if the spec is wrong or unclear, decide, record the decision and its reason in the
commit message, and name it in your report.

**Checks.** `.venv/bin/python -m pytest -q -p xdist -n 2 pagekit` and
`sh .githooks/check-static.sh`; write output to files and check exit codes. Never skip
commit hooks.

**Commit messages** end with:

    Clean-room: build
    [Clean-room-finding: NNNN, one line per finding report that informed the commit]
    Co-Authored-By: [model name] <noreply@anthropic.com>

**Stop conditions.** Stop and report if you see ScanTailor or other GPL page-processing
source, if the work needs anything outside the allowed sources, or if a check fails in
a way you cannot fix inside the allowed paths.

**Report** concisely: commits, what each change does and its limits, check exit codes,
and anything you could not do.
