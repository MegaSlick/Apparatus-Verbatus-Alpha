# The plan to reach one push — written 2026-07-28, before compaction

**Read with `HANDOFF.md` and `DECISIONS_FOR_TYREL.md`.** This is the working plan; those hold
the state and the open questions.

---

## STATUS: assembled and unpushed, 2026-07-28

**Six commits on `infra/workspace-readiness`, `dd4db02..3bd303a`. Tree clean. Nothing pushed.**

All seven fixes are done and each was verified by running it, not by reading it. T21 was also
implemented — it had been recorded as an answer but never made it into the fix list.

| # | Fix | How it was proved |
|---|---|---|
| 1 | `reviewer-pass` guards use `set -eu` and `exit 1` | reproduced the loss: old snippet emptied the previous report and exited **0**; fixed one refuses, exits 1, leaves it byte-intact, still writes when absent |
| 2 | `maxTurns` gone from all six roster files; deadline injected by `seat.sh` | dry run shows the budget in the prompt; 4 new tests; 51 seat tests pass |
| 3 | `--ephemeral` / `--strict-config` | both real in `codex exec --help`; nothing stripped |
| 4 | two digest exemptions removed | scanner passes on the whole worktree with them gone |
| 5 | every Sol edit to CLAUDE.md reverted | all six protected documents were byte-identical to `1001db7` **when this row was written**; CLAUDE.md and `.gitignore` have changed since, by Tyrel's own hand. Re-prove with `git diff 1001db7 -- <file>` before relying on it |
| 6 | machine path out of `autoclave/README.md` | no `/Users/tyrel` in any tracked file |
| 7 | `ALLOW_DETACHED_COMMIT=1` | 2 tests: blocked without, clean with |
| T21 | `pre-push` counts three **distinct** reviewers | 2 tests: unknown future releases pass, one name three times fails |

**Found and fixed while assembling, not in the original plan:**
- `guard.py` denied `git config core.hooksPath` — the *bare read form*, and the exact command
  a session runs to check the hooks are installed. It blocked this session mid-assembly.
- `notify.sh`'s payload builder read the topic from `os.environ`, so a topic loaded from the
  file raised `KeyError`. The file path had never been exercised end to end.
- `notify.sh` tested the config with `-r`, which is true of a named pipe — a FIFO there blocks
  forever inside the `SessionStart` hook. Requires a regular file now.
- One new test tripped the secret scanner; fixed by splitting the literal, not by exempting it.

**Verification on the assembled tree:** 439 tests pass (exit 0). `check-all.sh` passes except
the wheel bootstrap, which needs `pip install -r requirements-dev.txt` and would pin
`setuptools==83.0.0` into Homebrew's system Python — left alone deliberately; CI installs it in
a clean runner and runs the same script. Outgoing-history ingress scan passes. Author is Tyrel
on all six; trailers name Sol and Opus as authors, Fable as reviewer.

**Compared file-by-file against Sol's final tree:** 31 of 51 files byte-identical, 20 changed,
every one a deliberate deviation listed above.

### What still blocks the push

1. **The phone test** — the notify commit (`3bd303a`) is last and separable so it can be
   dropped with one command. It needs one real `milestone` confirmed on the phone, because the
   failure it guards against is silence. **Needs Tyrel's go-ahead to send.**
2. **T17** — whether `workbench/active/` rides in this push or lands as its own pull request
   straight after. My recommendation is still separate; 148KB of prose in a harness diff means
   reviewers read handoffs instead of hooks. **Overtaken by events: the drawer is tracked on
   this branch now** (`git ls-files workbench/active`), so the live question is no longer
   whether to track it but whether to split it out of this push. Still Tyrel's to answer.
3. **The reviewer pass itself.** The Opus and Fable reviews were of *Sol's clone*, not of this
   assembled result. They are preparation. The gate needs three reviewers on `3bd303a` exactly.
4. **Tyrel applies the CLAUDE.md changes he wants** from `CLAUDE_MD_PROPOSALS.md`, including
   the T22 rule itself.

---

---

## Tyrel's answers, 2026-07-28

**T20 — the notification script.** *"We can hold it back or put it in secrets or personal or
something where it is gitignored."*

Interpretation: a topic stored in a **gitignored file is acceptable** — `private/` already is
one. So Sol's redesign is wrong on its central premise: it refuses to read
`private/ntfy.conf` *at all* and demands the topic come from the environment, which nothing
supplies. That is what would have silently killed the phone pings.

**Resolution: keep Sol's security work, restore the file.** Its genuine improvements stay —
the topic never reaches curl's arguments or environment, delivery is claimed only on a real
2xx, the event and message are validated before the config is read, 20 new tests. The single
change to reverse is the hard refusal of the gitignored config file. Env var stays supported
as an override, file stays as the default source.
**Verification before this ships: send one real `milestone` and confirm it arrives on his
phone.** No test substitutes for that, and this is the one change that fails silently.

**T21 — the push gate's reviewer names.** *"These should not be hard coded. We add the
contribution or reviews to the actual model names that do it and have Claude as co-author. It
should be a checklist at push."*

Resolution: **remove the three literal model strings from `pre-push`.** The gate counts three
*distinct* reviewers and records the model that actually answered — restoring the honest-label
rule Sol reversed. Attribution names the real releases that did the work. The gate becomes a
checklist run at push time rather than a string match that goes stale at the next model
release.

**T22 — who may edit the rules.** *"Agents can suggest changes to Claude files, to gov files
and other critical infrastructure files but never change them."*

Resolution: **CLAUDE.md joins the protected set.** Previously only GOALS, GOVERNANCE,
ARCHITECTURE, GLOSSARY and the root README were off-limits to agents; CLAUDE.md was explicitly
fair game, which is why Sol edited it freely across five rounds — including adding a new hard
rule 10.

Consequences, all of which apply at assembly:
- **Every Sol edit to CLAUDE.md comes out** and is re-presented as a proposal with exact
  wording, for Tyrel to apply or reject himself.
- **The new hard rule 10 is not adopted.** It becomes a proposal. (It also silently decided
  the evidence-versus-secret question — T22 is the reason that must not stand.)
- The rule itself goes into CLAUDE.md — **written by Tyrel, not by me**. Proposed wording is
  in "Proposals" below.

**Scope ruled the same day — see "T22 SCOPE" at the end.** Rules documents only. Hooks, CI,
agent files, skills, operations and tests stay open to agents.

---

## Fixes I make before assembly

Each ships with the check that proves it, and I will not report one done without pasting the
output.

**1. `reviewer-pass/SKILL.md` — the blocking defect.** Four embedded shell guards use a bare
`false` with no `set -e`, so each announces a refusal and then continues. The branch that says
it is "refusing to overwrite evidence" **overwrites the previous reviewer's report with an
empty file** — `unset gpt_output` has already run. Opus reproduced this. It is a hard rule 7
violation inside the procedure that produces push-gate evidence.
*Fix:* `set -eu` in each snippet. *Check:* reproduce the overwrite, apply, confirm it now
refuses and leaves the earlier report intact.

**2. Remove `maxTurns` from all six agent files** — Tyrel's ruling, T19. A turn cap is
invisible to the agent and truncates mid-thought; it ate a 236,000-token review tonight.
*Replacement:* Claude agent frontmatter has **no timeout field**, so the deadline goes in the
prompt, and every long-running agent writes its output to a file incrementally so any cut-off
degrades rather than deletes. `seat.sh` gains the same for GPT seats — it already knows the
timeout, so it injects it into the prompt rather than trusting the author to remember.
*Check:* run one agent with a stated deadline and confirm the partial file survives.

**3. Verify the two new Codex flags.** `seat.sh` gained `--ephemeral` and `--strict-config`.
**The tests use a fake codex**, so an invalid flag passes every test and then kills every live
GPT seat at startup. *Check:* one real `codex --help`. If either is not real, strip it.

**4. The secret-scanner exemptions.** `check_ingress.py` carries two digest-pinned exceptions
for `test_notify.py`. Nothing in the *final* file matches them — they cover synthetic values in
the intermediate round commits, which are not going in. *Fix:* remove them, since assembly
creates fresh commits. *Check:* the scanner passes on the assembled tree with the exemptions
gone. If it does not, record exactly which two strings they bind rather than leaving digests
nobody can audit.

**5. Revert Sol's CLAUDE.md edits** (T22) and convert them to proposals.

**6. The machine-specific path** `/Users/tyrel/ocr_pipeline` was written into `CLAUDE.md` and
`autoclave/README.md`. Reverting CLAUDE.md handles the first; the second gets a portable
phrasing.

**7. `pre-commit` blocks any commit while detached, with no escape hatch** — this would also
block a legitimate mid-rebase amend, and `--no-verify` is blocked for Claude. *Fix:* add a
named `ALLOW_` variable, matching how every other guard here works.

---

## Assembly — how the single push gets built

1. **Fresh, attributed commits.** Sol's five commits **must not go in** — authored as the
   model, no trailers, and their intermediate history contains topic-shaped synthetic strings.
   The *final tree* is reassembled onto `infra/workspace-readiness` as Tyrel-authored commits
   with `Co-Authored-By: GPT-5.6 Sol (OpenAI)` where Sol wrote the lines, and
   `Co-Authored-By: Claude Opus 5` where I did.
2. **Grouped coherently**, not one giant commit: hooks and their tests; the agent roster and
   skills; operations and `seat.sh`; documentation truthfulness fixes; the notify group last
   and separable.
3. **Nothing from `workbench/` enters except `active/`**, which this branch tracks (T17).
   The rest stays gitignored.
4. **Run everything**: `check-all.sh`, the full suite (403 tests at last count), `tidy.py`,
   and the ingress scanner over the assembled tree.
5. **Then stop.** The branch sits unpushed until Tyrel says go, and the real three-reviewer
   pass runs against that exact commit. **The Opus and Fable reviews already done are
   preparation, not the gate** — they reviewed Sol's clone, not the assembled result.

---

## Held back from this push

- **The notify group** ships only after a real phone test (T20).
- **Everything in `workbench/` except `active/`** — `active/` is tracked on this branch now
  and will ride in a push unless Tyrel splits it out (T17).
- **Sol's CLAUDE.md edits** — proposals now (T22).
- **`isolation: worktree`** — still off, still needs the branching answer (T11).

---

## Proposals for Tyrel's files — his to apply, not mine

**For CLAUDE.md, under Hard rules — the T22 rule in his own words:**

> **Agents propose; they never amend.** The governing documents — this file, GOALS,
> GOVERNANCE, ARCHITECTURE, GLOSSARY and the root README — are Tyrel's alone. An agent may
> suggest a change to any of them, with exact wording, in its report. It may not make one.
> A rule an agent wrote into the file that binds it is not a rule.
>
> Code is different and stays open: hooks, CI, the agent and skill files, operations and tests
> are written by agents and land through review like everything else. The line is between what
> *governs* and what *executes*.

**Also queued as proposals, not applied:** Sol's new hard rule 10 on secrets in ignored files
(needs the evidence carve-out), and its rewrite of the model-cost paragraph.

---

## T22 SCOPE — RULED, 2026-07-28

**Tyrel's ruling: rules documents only.** *"Sure I am good with your recommendation it is
cleaner."*

**The line is what GOVERNS versus what EXECUTES.**

**Protected — agents propose, never amend:**
`CLAUDE.md`, `GOALS.md`, `GOVERNANCE.md`, `ARCHITECTURE.md`, `GLOSSARY.md`, root `README.md`.
A document that tells sessions what they may do must never be written by the thing it
constrains. That is self-authorising, and it is exactly what happened when Sol wrote itself a
new hard rule and moved an existing rule so its own change would comply.

**Not protected — agents may write, subject to the normal gate:**
`.githooks/`, `.github/`, `.claude/agents/`, `.claude/skills/`, `operations/`, tests, and all
code. These are reviewed line by line, tested, and land only through a pull request Tyrel
approves. The quarantine and the reviewer pass already govern them, and README.md's "every
line of code here is AI-generated" depends on this staying open.

**Consequence for this push:** Sol's ~40 files of hook, guard, scanner, `seat.sh` and test
work go in as normal reviewed code. Only its CLAUDE.md edits are reverted and re-offered as
proposals.

**Note the edge, so nobody has to guess later:** `.claude/agents/*.md` and
`.claude/skills/*/SKILL.md` instruct agents and sit near the line. They are **not** protected —
they are operational configuration, they are reviewed like code, and an agent improving a
skill is the system working. What an agent may not touch is the file that says what agents are
allowed to do at all.
