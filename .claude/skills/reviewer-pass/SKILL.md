---
name: reviewer-pass
description: Review one settled commit with independent model seats and name the reviewers in the commit's Reviewed-by trailers. Use only after Tyrel authorizes review.
disable-model-invocation: true
---

# Reviewer pass

This procedure prepares evidence for Tyrel's push decision. It never pushes,
merges, edits the reviewed commit, or treats reviewer agreement as authority.

## 1. Ask and freeze

Ask Tyrel before starting a paid or time-consuming review pass. Review and push
are separate permissions.

Record `reviewed_sha=$(git rev-parse HEAD)` and require a clean tracked and
untracked tree. Materialize that commit into a fresh read-only snapshot with
`git archive`; every reviewer reads that snapshot, not a changing checkout.

Triage the change in one paragraph: behavior changed, likely cost of a defect,
and recommended coverage. The standing default is two independent readers across
two vendors, with fresh eyes on the change:

- Claude Opus, high effort
- `sh operations/codex/seat.sh audit-sol - < "$prompt_path"` — GPT Sol

  **Pick the seat by the size of the pass.** `audit-sol` and `judge` are the same
  model at the same effort; they differ only in deadline — 2700 seconds against
  600. A full-diff or whole-branch audit can still be writing its report at ten
  minutes, and the seat kills it there, so a truncated review would arrive
  looking like a complete one. Use `audit-sol` for a full-diff or high-risk
  pass and `judge` only for a genuinely bounded one.

**Recommend the third seat when the change earns it, and let it go otherwise:**

- Claude Fable, high effort — recommend it when the question is hard, being wrong
  would be expensive, or the change touches money, launch, shutdown, or a
  governance rule. Say in the triage which of those applies. Outside those
  classes, do not offer it: `CLAUDE.md` is explicit that "a seat offered every
  pass and declined most of them trains him to skim the offer," and an offer he
  skims is worth less than no offer at all. It is also the most expensive reader
  here, so cost is a legitimate reason for him to decline one you do recommend.

  This page used to say to offer it every pass regardless, which contradicted
  `CLAUDE.md` outright — a reviewer found the conflict. The governing document
  wins, and the criteria above are its words.

Tyrel decides the roster for this pass. Object once with the coverage at stake
and your recommendation, ask about the exact roster, then follow his clear
confirmation. Never infer a reduction or carry one into the next pass. Report the
real coverage, and say plainly that two agreeing seats are thinner evidence than
three.

## 2. Dispatch bounded, blind reviews

Write one neutral prompt and give its exact bytes to every reviewer. Include:

- the exact commit and snapshot path;
- the intended behavior and relevant governing constraints;
- the complete replacement files, not only a diff;
- a request for every finding, its evidence, consequence, and proposed remedy;
- a ban on edits, pushes, merges, external effects, and reproducing secrets.

Reviewers remain blind to one another. Preserve resolved model/effort metadata
when available. A model substitution counts only if Tyrel explicitly accepts
it for this pass. A seat whose **resolved** effort lands under its role's floor
is non-qualifying coverage — redispatch it, or ask Tyrel for a per-instance
override; never write a trailer for it as if it qualified.

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
roster is Opus and Sol, so most passes name both — but a trailer written from
the plan rather than the outcome asserts a review on exactly the commit where
none happened, and that is the commit somebody will one day be reading it on.

There is no separate receipt file. `pre-push` reads these trailers back and
prints them as a checklist before pushing; it never refuses, because nothing
here turns on anything but Tyrel's word.

Report the evidence paths, coverage, agreements, disagreements, and unresolved
findings to Tyrel. Stop before push; ask for that exact action separately.
