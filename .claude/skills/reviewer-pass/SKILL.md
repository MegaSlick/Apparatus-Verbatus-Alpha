---
name: reviewer-pass
description: Review one settled commit with independent model seats and name the reviewers in the commit's Reviewed-by trailers. Runs by default before an initial push; Tyrel reduces the roster in words.
disable-model-invocation: true
---

# Reviewer pass

This procedure prepares evidence for Tyrel's pull-request decision. It never
pushes, merges, edits the reviewed commit, or treats reviewer agreement as
authority.

## 1. Triage and freeze

Review is a standard, not a gate: it happens by default before an initial push,
and Tyrel reduces it in words. Do not ask permission to start one. Tell him the
triage and the roster you are running, and if he wants it thinner he will say so.

Record `reviewed_sha=$(git rev-parse HEAD)` and require a clean tracked and
untracked tree. Materialize that commit into a fresh read-only snapshot with
`git archive`; every reviewer reads that snapshot, not a changing checkout.

Triage the change in one paragraph: behavior changed, likely cost of a defect,
and the coverage you are running. The standing default is three independent
readers, two vendors minimum, with fresh eyes on the change:

- Claude Opus, high effort
- the `audit-sol` seat — GPT Sol, dispatched and captured only through
  `capture-seat-report.sh` per step 3; never a bare `seat.sh` invocation, because
  the capture script's guards are load-bearing and a bare dispatch has none of them

  **Pick the seat by the size of the pass.** `audit-sol` and `judge` are the same
  model at the same effort; they differ only in deadline — 2700 seconds against
  600. A full-diff or whole-branch audit can still be writing its report at ten
  minutes, and the seat kills it there, so a truncated review would arrive
  looking like a complete one. Use `audit-sol` for a full-diff or high-risk
  pass and `judge` only for a genuinely bounded one.
- Claude Fable, high effort — the third seat, and part of the default rather than
  an offer made pass by pass. It is the most expensive reader here, so it is the
  one a reduction usually reaches for first. Say in the triage what the third seat
  is covering, and resist the reduction hardest where the question is hard, a
  defect is expensive, or the change touches money, launch, shutdown or a
  governance rule.

A reduction is Tyrel's, per named push, and is never inferred — not from silence,
not from an outage, not from the last pass. Object once with the coverage at
stake, then follow his clear answer. Report the real coverage, and say plainly
that two agreeing seats are thinner evidence than three.

## 2. Dispatch bounded, blind reviews

Write one neutral prompt and give its exact bytes to every reviewer. Include:

- the exact commit and snapshot path;
- the intended behavior and relevant governing constraints;
- the complete replacement files, not only a diff;
- a request for every finding, its evidence, consequence, and proposed remedy;
- a ban on edits, pushes, merges, external effects, and reproducing secrets.

Reviewers remain blind to one another. Preserve resolved model/effort metadata
when available. A model substitution counts only if Tyrel explicitly accepts
it for this pass. Every evidence seat in a reviewer pass carries a `high` floor —
Codex seats included, not only the Claude roles with floors in their files. A seat
whose **resolved** effort lands under that floor is non-qualifying coverage —
redispatch it, or ask Tyrel for a per-instance override; never write a trailer for
it as if it qualified. When the runtime does not expose resolved effort, record
that it was not exposed: the request stands, and the pass is reported as unverified
on that point — never silently assumed to qualify.

## 3. Preserve and verify

Hold each response in memory until
`python3 .githooks/check_ingress.py --stdin-file` accepts it. Then write the
complete nonempty reports beneath one new
`workbench/raw/<date>_<short-sha>_reviewer-pass/` directory without overwriting
existing evidence. Scan those exact files again with `--file`.

Create that directory before dispatch, then capture each shell-dispatched seat
with `operations/codex/capture-seat-report.sh` rather than relying on the chat
transcript:

```sh
sh operations/codex/capture-seat-report.sh audit-sol "$prompt_path" "$report_dir/gpt-sol.log"
```

The seat name is whatever the triage in step 1 chose; run it once per
shell-dispatched seat, with a different report path each time.

Five things that script enforces are load-bearing, and prose could enforce none
of them: the refusal to overwrite an existing report, the refusal to write
through a dangling symlink (`[ -e ]` follows links, so `[ -L ]` is tested too),
keeping partial output when the seat exits non-zero, writing nothing at all
until the ingress scan clears the text, and aborting rather than resuming on
HUP, INT or TERM. Its header explains each one, and
`.githooks/test_skill_procedures.py` runs the real file against fakes — so an
edit that breaks a guard fails there, while a rewording of this page does not.

Keep disagreements. Verify each proposed fix against the code and governing
documents; reviewers supply evidence, not verdicts. If a real finding changes
the tree, stop: commit the correction and run a new pass on the new commit.

## 4. Record who read it, in the commit

Before recording, require both:

- `git rev-parse HEAD` still equals `reviewed_sha`;
- the worktree is still clean.

Then amend the message to name the seats that actually returned a report:

```sh
git commit --amend --no-edit \
  --trailer "Reviewed-by: <resolved reviewer> <noreply@vendor.example>"
```

**Amending the message does not change the tree.** The commit SHA moves; the
tree SHA does not, so the code the reviewers read is byte-identical to the code
that ships. Check it if you want to — `git rev-parse HEAD^{tree}` before and
after. That is what makes it honest to attach their names to this commit even
though the review happened before the amend.

**Write a trailer only for a seat that actually returned.** Not the roster you
planned, not the seat that errored, not the one Tyrel declined. The standing
roster is three seats, so most passes name three — but a trailer written from
the plan rather than the outcome asserts a review on exactly the commit where
none happened, and that is the commit somebody will one day be reading it on.

**Name a seat by its release, whichever vendor** — "Claude Opus 5", "GPT-5.6 Sol
(OpenAI)". `Codex (OpenAI)` is the fallback *only* when the serving release genuinely
cannot be resolved, and the handoff records that it could not. "An AI reviewed it" ages
badly; "GPT-5.6 Sol reviewed it" does not. This governs `Co-Authored-By:` at commit
exactly as it governs `Reviewed-by:` here.

There is no separate receipt file. `pre-push` reads these trailers back and
prints them as a checklist before pushing; it never refuses, because nothing
here turns on anything but Tyrel's word.

Report the evidence paths, coverage, agreements, disagreements, and unresolved
findings to Tyrel. Stop before pushing. The first push of a branch and the pull
request it becomes are one gate, and it is asked for separately from this pass.

## 5. After the pull request is open

CLAUDE.md, Pushing and merging, states the rule: **the pull request is a working
surface, not his inbox.** This is how it is worked.

**Watch it until CodeRabbit has reported and its threads are settled, then stop.**
Watching past that point is noise; stopping before it leaves a review nobody read.
A push to an open pull request restarts CodeRabbit, so a fix means one more wait.

**Resolve a thread only with a stated disposition** — never silently, never in bulk.
One of two:

- **Fixed**, naming the commit that did it.
- **Declined**, with the reason. A finding you disagree with is declined on the
  record, not left open and not quietly resolved.

**A disposition may correct the reviewer.** Where a finding's conclusion is right but
its stated mechanism is wrong, take the fix and say so — accepting faulty reasoning
because the conclusion suited you puts a false claim in the permanent record of the
pull request. Verify the mechanism against the tree before you either accept or
dispute it.

**Recommend a final pass before he merges.** Squashing and merging is his alone.
