# 2026-08-01 — The framework redesign: what was commissioned, what was lost, what survived

Dated evidence, committed so it travels to any clone. The working notes for this sit in
`workbench/design/system_coherence/`, which is gitignored and exists only on the machine
that ran the session. If you are reading this from a fresh clone, that directory is not
here and this file plus the plan beside it are the whole record.

## What was asked for

Tyrel, 2026-07-31, after a week spent building the framework rather than the pipeline:

> "I just want this project to work. Have a guide and set of rules and some hard coded
> thing like sub agents not messing with files outside the task they are doing. It's been
> going around and around and around. We change one thing then we work at another then it
> changes two things then we work around something."

He asked for an end-to-end architecture plan — guards, CLAUDE.md, rules, governance, down
to the small mechanisms — designed top-down so the parts hold each other. Full budget, max
effort, GPT seats first and a Claude workflow reading their output. Unattended overnight.

## The measurement that states the problem

Generated from the tree, not estimated:

| | Lines |
|---|---|
| The harness — `.claude/`, `.githooks/`, `operations/` | **10,905** |
| The pipeline it exists to build — `pipeline/`, `common/` | **6,234** |
| The governing prose — the six documents | **807** |

The scaffolding is roughly **1.75×** the building. Every framework line is something that
can fall out of step with another one, and that drift is what the week went on. Any
proposed design should be measured against this ratio: if it does not move, it has not
answered the complaint.

## The failure mode, demonstrated

One change to one file — `CLAUDE.md` — was reviewed by three independent seats. What they
found is a catalogue of the same defect wearing different clothes:

- A guard constant quoted a rule verbatim; rewording the rule left the quotation stale. A
  test caught it, because someone had built that one binding.
- The hard rules were nearly renumbered. Six live citations across four files would have
  silently pointed at the wrong rule. Nothing would have caught it.
- The term "governing document" became "governed path" in one file. Three role files, a
  roster README, two shell hooks and the guard's refusal strings still say the old thing.
- The document declared `.claude/` governed; the guard enforced six filenames; the roster
  README still said "the six governing documents"; no test inspected the roster README.
- A new hard rule asserted that `README.md` documents how to switch the machinery off.
  `README.md` documents no such thing. The rule shipped false on the day it was written.

None of these are careless. They are what happens when one fact is written in five places
and nothing binds the copies.

A 200-line scanner written during the session found one of these — a hook citing a deleted
section — in about a second, the same defect three max-effort model seats found by reading.
It also produced four false positives for every true one, which is the real lesson: a check
at that precision gets switched off within a week, and a switched-off check is worse than
none, because the prose still claims it runs. The fix is to make citations declarative
rather than guessed at. That belongs in the design.

## What was lost, and why

The unattended run stalled for roughly seven hours on a permission prompt, and the Claude
half of the night did not happen.

The session was told to run with no permission requests. It named the prompt-triggering
actions it would avoid. It then edited `.claude/hooks/guard.py` mid-run, to repair a defect
it had introduced earlier that evening — and writes to that directory are exactly what the
guard prompts the main session about. It judged the fix small and obviously correct and
treated that as the test. The test is not whether an action is small; it is whether it can
prompt.

The first edit removed a constant while a function still referenced it, so the guard raised
`NameError` and failed closed. That was correct behaviour, and it meant every subsequent
attempt to repair the guard was refused by the guard.

**This is the second time an unattended run has stalled this way.** The standing queue file
was opened after the first one. The prose rule did not prevent either. That is itself the
strongest argument in this record for the redesign: a rule that is merely written down has
now failed twice at the same task, and the question the plan has to answer is what would
have made it structurally impossible instead of forbidden.

`guard.py` was restored from its last commit; it imports cleanly and its suite passes.

## What survived

The expensive part. Three GPT design reports, complete — roughly three hours of max-effort
work across two OpenAI models, 4.5 MB of transcripts and 111 KB of digests:

- **Diagnosis and top-down architecture** — why the framework drifts, and what should
  replace it.
- **The enforcement layer** — what can be held mechanically, and the subagent confinement
  Tyrel asked for by name.
- **The operating model** — where his attention actually goes, and where it should.

Two independent Claude designs at max effort followed. The judged synthesis, the drafted
artifacts, the adversarial verification and the assembled plan are recorded in
`2026-08-01_framework-redesign-plan.md` beside this file if the workflow completed.

## The state of the code at this point

Branch `work/claude-md-revision`, on top of `work/spec-01-skeleton` (PR #14, open,
unmerged). Commits: `9a3268e` the CLAUDE.md rewrite, `fd41c84` the harness brought into
line, `8c120b4` two max-effort design seats. Full gate passed at `fd41c84` — 1044 tests.
Nothing pushed.

Findings from the three-seat review that remain **unfixed on purpose**, because fixing them
one at a time is the loop this exercise exists to end:

- The guard's `.claude/` check is a plain substring test, so it matches `~/.claude/` and any
  other repository's `.claude/` on the machine. It will prompt on files unrelated to this
  project. Introduced by `fd41c84`; two-line fix; **still live**.
- `.githooks/pre-push` cites a deleted `CLAUDE.md` section.
- `.claude/agents/README.md` still names six governing documents and no test inspects it.
- Hard rule 11 promises a removal step `README.md` does not contain.
- Hard rule 4 gates opening a pull request but not the first push of a branch, which sits
  in overridable prose and is absent from the Gate list.
- The guard still asks on every push, contradicting the new doctrine.
- `session-start` reads six documents before naming its branch, which the rewritten branch
  rule forbids; and the permission to arm the git hooks from `main` was dropped without
  replacement.

One genuine split between reviewers, unresolved and Tyrel's to settle: closing all of
`.claude/` locks the `infra-worker` role out of the guard's own code and tests. One seat
called that a high-severity defect; two read the same code and called it the decision
correctly implemented.

No `Reviewed-by:` trailers were written. The seats read `fd41c84` and the tree has moved.
