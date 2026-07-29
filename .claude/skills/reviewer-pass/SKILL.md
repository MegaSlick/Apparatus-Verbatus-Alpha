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

**Offer the third seat every pass, and name your recommendation in the triage:**

- Claude Fable, high effort — recommend it outright whenever the question is
  hard, being wrong would be expensive, or the change touches money, launch,
  shutdown, or a governance rule. It is the most expensive reader here, so cost
  and usage limits are a legitimate reason for Tyrel to decline. Offer it again
  next pass regardless.

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
it for this pass.

## 3. Preserve and verify

Hold each response in memory until
`python3 .githooks/check_ingress.py --stdin-file` accepts it. Then write the
complete nonempty reports beneath one new
`workbench/raw/<date>_<short-sha>_reviewer-pass/` directory without overwriting
existing evidence. Scan those exact files again with `--file`.

Set `report_dir` to that directory and create it before dispatch. Capture the
shell-dispatched seat with this snippet rather than relying on the chat
transcript — **run it as a script, not pasted line by line.** `set -eu` is what
makes each guard below fatal, and a pasted line that fails simply carries on to
the next one:

```sh
set -eu
gpt_seat="${gpt_seat:-audit-sol}"   # the seat the triage chose; see step 1
gpt_report="$report_dir/gpt-sol.log"
gpt_output=""
if gpt_output=$(sh operations/codex/seat.sh "$gpt_seat" - < "$prompt_path" 2>&1); then
  gpt_status=0
else
  gpt_status=$?
fi
[ -n "$gpt_output" ] || {
  echo "GPT reviewer returned an empty report" >&2
  exit 1
}
if ! printf '%s\n' "$gpt_output" |
     python3 .githooks/check_ingress.py --stdin-file; then
  unset gpt_output
  echo "GPT reviewer output failed credential scanning and was not written" >&2
  exit 1
fi
if [ -e "$gpt_report" ] || [ -L "$gpt_report" ]; then
  unset gpt_output
  echo "GPT report target already exists; refusing to overwrite evidence" >&2
  exit 1
fi
gpt_temporary=$(mktemp "$report_dir/.gpt-sol.log.XXXXXX") || exit 1
trap 'rm -f "$gpt_temporary"' EXIT
trap 'rm -f "$gpt_temporary"; exit 129' HUP
trap 'rm -f "$gpt_temporary"; exit 130' INT
trap 'rm -f "$gpt_temporary"; exit 143' TERM
umask 077
printf '%s\n' "$gpt_output" > "$gpt_temporary"
unset gpt_output
mv "$gpt_temporary" "$gpt_report"
gpt_temporary=""
trap - EXIT HUP INT TERM
if [ "$gpt_status" -ne 0 ]; then
  echo "GPT reviewer failed with exit $gpt_status; clean evidence remains at $gpt_report" >&2
  exit 1
fi
```

Five things there are load-bearing, and prose could not enforce any of them:
the refusal to overwrite an existing report, the refusal to write through a
dangling symlink (`[ -e ]` follows links, so `[ -L ]` is tested too), keeping
partial output when the seat exits non-zero, writing nothing at all until the
ingress scan clears the text, and aborting rather than resuming on HUP, INT or
TERM. `.githooks/test_skill_procedures.py` lifts this block out of this file and
runs it against fakes, so an edit that breaks it fails there; a rewording does
not.

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
