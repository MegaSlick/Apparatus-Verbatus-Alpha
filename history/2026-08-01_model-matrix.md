# Every model at every effort, measured in the chamber

Run 2026-08-01 on Tyrel's 2019 Intel MacBook Pro, three chambers at a time, one
container and one clone per cell. Forty-eight cells: four Claude models against the five
efforts `claude --help` lists, four Codex models against the seven
`operations/codex/seat.sh` allows.

**The task.** A `merge_spans` / `total_covered` module over closed integer intervals,
with edge cases chosen to punish carelessness rather than reward cleverness: touching
spans merge because nothing sits between 3 and 4, the input must not be mutated, tuples
not lists, `ValueError` on an inverted span, distinct-integer counting. Scored by a
held-out suite of thirteen checks that never entered any container.

**The headline, and the caveat that goes with it.** Forty-two of forty-eight cells
scored 13/13. No cell scored anything in between — every run was perfect or absent. So
this measures *speed and reachability*, not capability: the task saturated every seat,
and nothing here says what happens on work that is genuinely hard. Read the times as a
cost ranking for bounded, well-specified units, and do not read them as a quality
ranking at all.

## What would not run

| Seat | Effort | What happened |
|---|---|---|
| `gpt-5.3-codex-spark` | `minimal` | dispatch exited 1, no file produced |
| `gpt-5.3-codex-spark` | `none` | dispatch exited 1, no file produced |
| `gpt-5.3-codex-spark` | `max` | dispatch exited 1, no file produced |
| `gpt-5.6-luna` | `minimal` | dispatch exited 1, no file produced |
| `gpt-5.6-terra` | `minimal` | dispatch exited 1, no file produced |
| `gpt-5.6-sol` | `minimal` | dispatch exited 1, no file produced |

`minimal` is rejected by all four Codex models. `gpt-5.3-codex-spark` additionally
rejects `none` and `max`, so its usable range is `low`–`xhigh`. Claude accepted all five
of its levels. These are the reachability facts a session needs before choosing a seat;
everything else below assumes the cell runs.

## Times, fastest first

Every row below scored 13/13.

| Vendor | Model | Effort | Time | Tokens |
|---|---|---|---|---|
| codex | `gpt-5.3-codex-spark` | `low` | 14s | 21440 |
| claude | `sonnet` | `low` | 15s | not reported |
| claude | `sonnet` | `medium` | 15s | not reported |
| claude | `sonnet` | `high` | 19s | not reported |
| claude | `fable` | `medium` | 23s | not reported |
| claude | `opus` | `low` | 25s | not reported |
| claude | `sonnet` | `xhigh` | 28s | not reported |
| claude | `fable` | `low` | 29s | not reported |
| codex | `gpt-5.6-terra` | `none` | 31s | 28605 |
| claude | `fable` | `high` | 38s | not reported |
| codex | `gpt-5.3-codex-spark` | `xhigh` | 41s | 24276 |
| claude | `haiku` | `low` | 43s | not reported |
| claude | `haiku` | `xhigh` | 43s | not reported |
| codex | `gpt-5.3-codex-spark` | `medium` | 44s | 25425 |
| claude | `haiku` | `max` | 47s | not reported |
| claude | `haiku` | `medium` | 48s | not reported |
| claude | `haiku` | `high` | 53s | not reported |
| codex | `gpt-5.3-codex-spark` | `high` | 55s | 33150 |
| claude | `sonnet` | `max` | 55s | not reported |
| codex | `gpt-5.6-sol` | `none` | 69s | 27378 |
| codex | `gpt-5.6-luna` | `low` | 71s | 22546 |
| codex | `gpt-5.6-terra` | `low` | 76s | 24321 |
| claude | `opus` | `medium` | 78s | not reported |
| codex | `gpt-5.6-luna` | `medium` | 79s | 22934 |
| codex | `gpt-5.6-sol` | `low` | 85s | 28112 |
| codex | `gpt-5.6-terra` | `high` | 89s | 28628 |
| codex | `gpt-5.6-luna` | `none` | 93s | 32696 |
| codex | `gpt-5.6-terra` | `medium` | 93s | 32949 |
| claude | `opus` | `high` | 97s | not reported |
| codex | `gpt-5.6-sol` | `high` | 113s | 32235 |
| codex | `gpt-5.6-sol` | `medium` | 119s | 41221 |
| codex | `gpt-5.6-luna` | `high` | 124s | 37902 |
| claude | `fable` | `xhigh` | 126s | not reported |
| codex | `gpt-5.6-terra` | `xhigh` | 138s | 33768 |
| codex | `gpt-5.6-sol` | `xhigh` | 140s | 33768 |
| claude | `fable` | `max` | 154s | not reported |
| claude | `opus` | `xhigh` | 162s | not reported |
| codex | `gpt-5.6-terra` | `max` | 180s | 47109 |
| codex | `gpt-5.6-luna` | `xhigh` | 188s | 57795 |
| codex | `gpt-5.6-luna` | `max` | 204s | 37151 |
| claude | `opus` | `max` | 228s | not reported |
| codex | `gpt-5.6-sol` | `max` | 281s | 46032 |

Claude Code does not print a token total in `-p` mode, so those cells are honestly blank
rather than estimated.

## What the numbers actually say

**Effort bought time, not correctness.** `opus` at `max` took 228s to produce exactly
what `opus` at `low` produced in 25s — nine times the wall clock for an identical score.
The same shape holds everywhere: `luna` 71s at `low` against 204s at `max`, `fable` 23s
at `medium` against 154s at `max`. On bounded, well-specified work, raising effort is
spending for nothing.

**Sonnet is the fastest Claude seat, and Haiku is not.** `sonnet` finished in 15s at
both `low` and `medium`; `haiku` never beat 43s at any effort. That is the opposite of
what the price list implies and it is worth remembering: the cheap seat was three times
slower than the mid seat for the same result.

**`gpt-5.3-codex-spark` at `low` was the fastest cell in the matrix**, 14s, and among the
cheapest in tokens at 21,440.

**Luna is slow for its price.** It scored perfectly everywhere it ran, but 71s at `low`
against Terra's 76s and Sol's 85s means the volume tier bought little time here — and
this task's context was one page, which is where Luna is strongest. Its published
long-context weakness is untested by this matrix.

## What this does not measure

- **Hard work.** Nothing separated the seats, so nothing here ranks judgement.
- **Long context.** Every brief was about a page. The one published difference between
  Luna and the others is recall over long inputs, and this cannot see it.
- **Repeatability.** One run per cell. Times are indicative, not distributions.
- **Claude token cost**, which the CLI does not report in `-p` mode.

A second matrix on a genuinely hard task is the obvious follow-up, and it is the one
that would say something about capability rather than speed.

---

## Caveats carried over from the eight-seat audit notes

Moved here verbatim when `workbench/active/AUDIT_FINDINGS.md` was retired: these are the
method caveats for the matrix above and belong with it rather than in a findings file whose
other rows are all closed.

### Read this before trusting the table — the run confounds model with effort

Eight seats, one per model, at the effort each model's roster row recommends. That means
**Spark, Luna, Haiku and Sonnet ran at `medium` while Terra, Sol, Opus and Fable ran at
`high`** — and the four that produced the most findings are exactly the four that ran at
`high`.

So a cross-group comparison cannot separate "this model reads better" from "this model
had more thinking budget". That is a design error, and it lands harder here than in the
building benchmark: round one established effort does not change *correctness* on
well-specified building work, but this was **judging** work, which is the one place the
roster claims effort matters.

- **Clean comparisons:** Terra / Sol / Opus / Fable against each other. Spark / Luna /
  Haiku / Sonnet against each other.
- **Contaminated:** anything crossing those two groups.

**The fix for next time is one line: hold effort constant.** All eight at `high` for a
reading task, or all sixteen cells if the effort question itself is worth answering.
Until then, treat the ranking below as indicative and the two verdicts flagged
`EFFORT-CONFOUNDED` as unproven.

| Seat | Time | Words | Verdict |
|---|---|---|---|
| **Sol** | 276s | 1390 | Best. Five verified contradictions, each with both sides cited, plus the sharpest recommendations — "make the inventory one checked fact", "apply validation-before-Docker to every subcommand". Ran the test suite rather than reasoning about it. |
| **Terra** | 238s | 1149 | Nearly equal to Sol at a fraction of the price. Same five findings, citations to exact lines, correctly noticed the operator-facing consequence of each. |
| **Opus** | 161s | 1285 | Best *prioritised* list — ordered by consequence and readable by a non-programmer, which is what was asked. Slightly fewer verified findings than Sol or Terra. |
| **Fable** | 188s | 1081 | Solid. Found the seed and several real items; one entry recorded a *passing* consistency check so it would not be re-checked, which is a genuinely thoughtful habit. |
| **Luna** | 207s | 715 | Good value. Found the seed, the red gate, and the recovery-cap duplication that nobody else did. |
| **Sonnet** | 71s | 336 | Fast and thin. Found the seed and stopped, using a seventh of its budget. **EFFORT-CONFOUNDED** — ran at `medium`; this may be the effort rather than the model, and it is the weakest claim in this table. |
| **Haiku** | 142s | 834 | **Wrong.** Reported "no contradictions" and "alpha is in a good state to build on" while four other seats independently verified real ones, including a live safety hole. Confidently clean is the worst possible failure for an auditor. **EFFORT-CONFOUNDED** — ran at `medium`, so the *depth* is not proven; but asserting that no contradictions exist is a judgement error rather than a depth one, so the verdict probably survives a rerun. Worth confirming before it hardens into doctrine. |
| **Spark** | 124s | **0** | Produced no report at all. |

**The findings for how we use agents:**

- **Corroboration is the signal.** Every item in the priority list above was found by
  three or more seats independently. The one seat that disagreed with the consensus
  (Haiku) was the one that was wrong.
- **Haiku is suspect for audit or review work** — it said the repository was clean while
  it was shipping a broken deletion guard. Not yet proven, because it ran at `medium`;
  re-run it at `high` before this becomes a rule in the roster.
- **Sonnet may under-use a time budget** — told five minutes, it took 71 seconds. Also
  `medium`, so also unproven. Cheap to settle.
- **Terra is the value seat for reading work** — near-Sol quality, free until the cap.
- **The five-minute limit worked.** Nobody was killed; the slowest finished at 276s of
  330s. Telling the agent its deadline in the prompt is what made triage possible.
- **`/out` being a bind mount is what made this safe.** Every report was on the host as
  it was written, so a kill would have cost only unwritten thought.

---

## Round two of the model benchmark — completed

Two Exercism problems from Aider's polyglot set, three replicates each, eight seats.
Book-store (11 checks, greedy-trap optimisation) and Forth (15 checks, an interpreter
whose definition semantics are the discriminator). Held-out scoring. Raw:
`benchmark/results.tsv`.

**One seat failed, and it failed consistently.** `gpt-5.3-codex-spark` scored **10/11 on
book-store in all three runs**, failing `scales_to_200` every time — its solution does
not handle a 200-book basket. That is the first and only genuine capability difference
either round has produced, and three replicates is what makes it a finding rather than
noise.

**Everything else was perfect on both tasks, every replicate**: Terra, Luna, Sol, Sonnet,
Opus and Fable all 11/11 and 15/15. Haiku scored 15/15 on Forth — including the three
definition-semantics traps — so **Haiku codes competently and audits badly**, which is a
sharper and more useful distinction than "the cheap seat is weaker".

**Haiku has no valid book-store data.** Its five rows are three `CHAMBER_FAILED` and two
`SCORER_ERROR`, all harness faults of mine, not the model's. Do not read its zero as a
score. Re-running those five cells is a ten-minute job for whoever wants the gap closed.

**Median times, valid runs** — the practical ranking, since scores barely separate:
Fable ~58s, Sonnet ~44s, Spark ~25s when it works, Terra ~112s, Haiku ~124s, Opus ~130s,
Luna ~192s, Sol ~238s.

Fable being both fast and perfect is the surprise of round two and contradicts round
one's picture, where Sonnet was fastest. Worth one more look before anything is written
into the roster on the strength of it.

### Known defects in the benchmark harness

Both mine, both should be fixed before the harness is used again.

1. **The scorer had no timeout.** `scales_to_200` measured elapsed time *after* the
   candidate returned, so a non-terminating solution hung it forever. Three orphaned
   scorers were found at 96%, 72% and 61% CPU, one at **7.4 GB after 73 minutes** — this
   is what "the container is eating my CPU" actually was. A per-check `SIGALRM` was added
   and is bounded but only lightly tested; verify it before trusting it.
2. **`wait -n` does not exist in bash 3.2**, which is what macOS ships, so the slot cap
   in `resume.sh` silently did nothing and launched all thirty cells at once. Harmless
   here — see the load numbers below — but it is not doing what it says.
3. **Killing the driver orphans its scorer children.** That is how the runaways above
   survived.

### What the accidental full-concurrency run measured

Thirty cells launched simultaneously, nine chambers concurrent at peak:

- **~110 MB per chamber**, about **1 GB total**
- **VM load 1.40** on 6 CPUs; **7.6 GB of 12 GB still free**
- Host load 5.06 on 16 cores

The machine was never the constraint. Every failure was bookkeeping: stale chambers from
an earlier kill that `new` correctly refused to overwrite, and the scorer orphans above.

**Answering Tyrel's question directly: the environment does not eat CPU when idle.** A
chamber with no agent working in it is a sleeping process. The load he saw was host-side
Python from this harness, not the containers.

## What nobody looked at

No seat examined `pipeline/` in depth, and `workbench/` is gitignored so none of them
could see the handoff, the deferred actions, or the suspension record. Findings that
depend on that context are absent by construction rather than missed.
