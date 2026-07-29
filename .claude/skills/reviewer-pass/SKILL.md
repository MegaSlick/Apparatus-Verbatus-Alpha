---
name: reviewer-pass
description: Review one settled commit with independent model seats and record the exact-commit checklist receipt. Use only after Tyrel authorizes review.
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
and recommended coverage. Three independent readers across two vendors is the
standard:

- Claude Opus, high effort
- Claude Fable, high effort
- `sh operations/codex/seat.sh judge - < "$prompt_path"`

Tyrel may reduce the standard for this pass. Object once with the lost coverage
and your recommendation, ask about the exact reduced roster, then follow his
clear confirmation. Never infer a reduction or carry it into the next pass.

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

Keep disagreements. Verify each proposed fix against the code and governing
documents; reviewers supply evidence, not verdicts. If a real finding changes
the tree, stop: commit the correction and run a new pass on the new commit.

## 4. Record the exact state

Before recording, require both:

- `git rev-parse HEAD` still equals `reviewed_sha`;
- the worktree is still clean.

Record only reviewers that actually completed:

```sh
.githooks/record-audit.sh --commit "$reviewed_sha" '<resolved reviewer>' '<concise finding>'
```

An amended or later commit is unreviewed by design. Report the evidence paths,
coverage, agreements, disagreements, and unresolved findings to Tyrel. Stop
before push; ask for that exact action separately.
