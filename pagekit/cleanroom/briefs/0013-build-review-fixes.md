# Brief 0013: fix CodeRabbit's four findings on the clean-room checks

- Role: build side.
- Issued by the host session to a fresh Claude Opus 5.5 build-side agent at
  2026-10-02T20:45:39Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  bc68e855d0a78cb1147d4b57e49e592ad819109460ff198f3183f168983abae0
- Saved by the host before the agent started.

----- brief below this line, exactly as sent -----
You are a build-side agent for pagekit, a page-preparation tool licensed Apache-2.0.
Work only in /home/user/rv/pagekit on branch review/12-pagekit. Commit there in focused commits; never
push and never touch other branches or worktrees. No network.

**Objective.** Fix four review findings on the clean-room checks (CodeRabbit on PR #249). Each is a check that fails open or exempts too much; make each fail closed and pin it with a test that fails on the old code.
1. `.githooks/pre-commit` (around line 47): when `pagekit/cleanroom/` exists but `pagekit/cleanroom/gate.py` does not, the hook currently skips the gate silently. Make it print that the gate is missing and refuse the commit. Keep today's behaviour when gate.py is present, and when `pagekit/cleanroom/` does not exist at all (other branches). Test in `.githooks/test_hooks.py`.
2. `pagekit/cleanroom/check_report.py` (around lines 72-76): every line after a `Source:` line, until a blank line or heading, is exempt from the `identifier_shape` and `call_or_assignment` rules, so code can hide on a line under `Source:`. Exempt only the `Source:` line itself. Update `test_a_source_paragraph_may_hold_code_like_names_but_not_other_code` (a code-like line on the line after `Source:` must now fail) and the matching guidance in `pagekit/cleanroom/CLEANROOM.md`. Confirm all accepted finding reports in `pagekit/cleanroom/findings/` still pass the check unchanged; do not edit any finding file. If one fails, stop and report it.
3. `pagekit/cleanroom/gate.py` (around lines 88-95): removing HOLD is accepted when any changed incident note carries a decision. Require every incident note in the after-tree to carry a decision when HOLD is removed, keeping the existing decision detection. Test in `pagekit/cleanroom/test_gate.py`.
4. `pagekit/cleanroom/gate.py` (around lines 139-145): in the incidents-kept check, a failed read of the note on the base commit yields empty text and passes. Check the return code and raise `scan.ScanError` when the base read fails. Also treat an incident note that is empty on base the same as any other: its replacement must still start with the old text (empty old text is fine), but a base read failure must never pass. Test it.

**Clean-room rule (mandatory).** pagekit must be an independent work. You must not
read, search for, fetch or reproduce ScanTailor or ScanTailor Advanced source code, or
any other GPL page-processing code. In this repository that includes the files listed
under "Known pre-existing item" in `pagekit/cleanroom/CLEANROOM.md`: do not open them.
If you recognise something you are about to write as ScanTailor's or ScanTailor
Advanced's (a name, a constant, the way a function is split up), do not write it;
design your own and say so in your report. If you see their source, even by accident,
stop at once, write nothing more, and report what you saw and where.

**Work from these and nothing else:**

- the spec: none for this round (the four findings above are the task);
- finding reports: none;
- published papers and textbooks, cited by author, title and year in the code's
  docstring and in NOTICE when a method follows one;
- pagekit's own code, tests and README, and Pillow (a dependency, never copied in);
- general image-processing knowledge.

**Allowed paths and actions.** `.githooks/pre-commit`, `.githooks/test_hooks.py`, `pagekit/cleanroom/check_report.py`, `pagekit/cleanroom/test_check_report.py`, `pagekit/cleanroom/gate.py`, `pagekit/cleanroom/test_gate.py`, `pagekit/cleanroom/CLEANROOM.md`. Do not edit LOG.md, briefs/, findings/ or incidents/; the host owns records. Tests use synthetic data, never real register material.

**Deliverable.** The four fixes with their tests. The spec is not changed by the build;
if something is wrong or unclear, decide, record the decision and its reason in the
commit message, and name it in your report.

**Checks.** `.venv/bin/python -m pytest -q pagekit .githooks/test_hooks.py` (CI=1 PYTHONSAFEPATH=1 PYTHONPATH=$PWD set) and
`sh .githooks/check-static.sh`; write output to files and check exit codes. Never skip
commit hooks.

**Commit messages** end with:

    Clean-room: build
    Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
    Claude-Session: https://claude.ai/code/session_01JQGcQfwD6ES3eJWJfmkYE5

**Stop conditions.** Stop and report if you see ScanTailor or other GPL page-processing
source, if the work needs anything outside the allowed sources, or if a check fails in
a way you cannot fix inside the allowed paths.

**Report** concisely: commits, what each change does and its limits, check exit codes,
and anything you could not do.
