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
