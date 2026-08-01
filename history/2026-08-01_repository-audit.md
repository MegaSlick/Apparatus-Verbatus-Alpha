# Eight seats audited this repository, and found a live safety hole

2026-08-01. Eight agents — every model available, one chamber each — were given the whole
repository, five minutes, and an open-ended brief: find contradictions, find
optimizations, say what to tackle first. No score. Seven of eight returned a report; all
finished inside the deadline without being killed.

This file is the durable record. The raw reports and the working assessment live in
`workbench/`, which is gitignored and local to one machine; this is the part that
survives.

## The finding that mattered

**The deletion guard silently stops working on Linux.**

`.claude/hooks/guard.py` treated everything under `/tmp/` as disposable:

```python
DISPOSABLE_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
```

On macOS this never bites. `project_root()` and `working_directory()` both call
`.resolve()`, and macOS resolves `/var/folders/…` to `/private/var/folders/…`, which
matches none of those prefixes — so the refusals fire and the tests pass.

**On Linux there is no such symlink.** A repository checked out under `/tmp` — which is
every chamber, and CI — is judged disposable in its entirety, and a recursive delete of
it passes unchallenged. The refusal that exists to protect the one irreplaceable thing
in this project does nothing, on exactly the platform where the automated checks run.

Found independently by four seats. Confirmed by running the suite inside a chamber: six
tests fail there and pass on the development machine. Nothing had been pushed, so CI had
never seen it; it would have gone red on the first push.

**The lesson is bigger than the bug.** The test suite is not hermetic — it passes on the
host and fails in a container. That undermines the design where a dispatched agent runs
its own tests before handing work back, because the suite it runs is not the suite the
author ran.

## The other five

Each was reported by three or more seats independently and re-verified by the session
against the tree.

1. **The full gate is red in a chamber** — 572 collected, six failures, none reproducing
   on the development machine. Four are the guard hole above; two are item 2.
2. **`login` checks Docker before validating its arguments**, so `login gemini` reports a
   missing engine rather than naming the valid vendors. The same defect had been fixed
   for `dispatch` earlier the same day and not carried across. The instruction that came
   back was the right one: apply validation-before-infrastructure to *every* subcommand,
   not one at a time as each is noticed.
3. **`README.md` and `CLAUDE.md` disagreed on the guard's size** — six versus seven, with
   seven in the code. Seeded deliberately by the session to see who reads across
   documents; seven of eight found it. The recommended fix was better than correcting the
   number: make the inventory one checked fact, so a document cannot drift from the code
   unnoticed.
4. **`operations/autoclave/README.md` contradicted itself on credentials**, describing
   vendors as both signed in and not wired up.
5. **The status line was stale**, still saying the workspace was not yet built after a
   container harness had been driven end to end by two vendors.

## What this says about using agents

- **Corroboration is the signal.** Every item above was found by three or more seats
  independently, and the one seat that contradicted the consensus was the one that was
  wrong.
- **One seat reported "no contradictions" and "alpha is in a good state to build on"**
  while four others were verifying real defects including the safety hole. For an
  auditor, confidently clean is the worst available failure.
- **One seat produced nothing at all** in the time given.
- **Telling an agent its deadline is what makes triage possible.** Every report was
  written incrementally into a bind-mounted directory, so stopping a container would have
  cost only unwritten thought. Nothing needed stopping.

**A caveat on the comparison, recorded because it limits what may be concluded.** The run
gave the four judgement-tier models `high` effort and the four cheaper ones `medium`, so
model quality and thinking budget are confounded for any comparison crossing those
groups. Comparisons within each group are clean. Two verdicts about the cheaper seats are
therefore unproven and are marked as such wherever they are recorded.

## The coding benchmark run the same day

Two rounds, both in `history/2026-08-01_model-matrix.md` and the workbench.

**Round one** — every model at every effort, 48 cells. Forty-two scored full marks and
none scored in between: the task saturated, so it measured speed rather than capability.
Its durable finding is that **effort buys time, not correctness** on well-specified
building work — one seat at maximum effort spent nine times the wall clock of the same
seat at low effort for an identical result.

**Round two** — two harder problems, three replicates each. One seat failed consistently:
a solution that did not scale to a large input, in all three runs. Everything else was
perfect on both tasks in every replicate.

**Round two's first attempt was invalid, and how it failed is worth keeping.** The task
specification contained an arithmetic error by its author — a price stated one way and
the worked examples computed another. Two seats **refused to write code and named the
exact contradictory line**; two others silently resolved the conflict in favour of the
rules and produced correct code without mentioning it. All four were technically right.
That accident produced a better test than the one designed, and it is worth running
deliberately: a knowingly contradictory brief is the sharpest available test of whether
an agent flags a bad specification or papers over it.
