# Immutable review candidates

A consequential review targets one clean commit, not a moving index. The candidate SHA is
the reviewed object; a report that cannot name it is advice, not review evidence.

```sh
export GIT_NO_REPLACE_OBJECTS=1
python3 operations/review/candidate.py prepare --base origin/main
# give the printed candidate and base SHAs to every reviewer
python3 operations/review/candidate.py receipt \
  --candidate <sha> --base <sha> --reviewer <name> --report <path>
```

Each reviewer reads the committed diff with `GIT_NO_REPLACE_OBJECTS=1` from the printed base
through the candidate and writes exact `Candidate: <full SHA>` and `Base: <full SHA>` lines.
`receipt` refuses a moved `HEAD`, dirty tree, empty report, symlink, wrong SHA, or either
missing identity. It also distinguishes an unknown/non-commit base from a base that exists
but is not an ancestor of the candidate. It snapshots the safely-read report bytes and writes
the snapshot plus its JSON receipt under `workbench/raw/reviews/<candidate>/` with durable,
no-replace publication; the receipt points at that retained snapshot, not the mutable source
path.

Fixing a finding creates a new candidate. Every earlier receipt is then stale; repeat the
reviews warranted by the change's risk. Run the final gate while `HEAD` is that reviewed
candidate, and push that exact commit without amending it. Review reports are local evidence
and stay out of Git. A `Reviewed-by:` trailer may be added only after that reviewer reports;
because amending changes the commit SHA, the amended result is a new candidate and must pass
the proportionate, risk-warranted review again before push.

## Bounded CodeRabbit procedure

CodeRabbit is advisory evidence. It never approves a merge: `request_changes_workflow` stays
off, CLAUDE.md hard rule 14 and the final gate decide readiness, and a resolved CodeRabbit
thread is not an automated approval.

1. Finish one coherent branch change and commit it. Prepare the candidate against the complete
   `origin/main` baseline as above. Run at most one routine local baseline review:

   ```sh
   coderabbit review --agent --committed --base origin/main --config operations/review/README.md
   ```

   Record every finding as fixed or declined, with the concrete trigger, impact, evidence, and
   any uncertainty. A declined optional improvement needs its reason. Do not manufacture a
   correction merely to close a thread.

2. If that review leads to a material correction, commit the correction and prepare its new
   immutable candidate. Run one narrow follow-up against the preceding candidate:

   ```sh
   coderabbit review --agent --committed --base-commit <preceding-candidate> \
     --config operations/review/README.md
   ```

   This is a review of the material correction, not a new whole-branch baseline. Add
   `--dir <changed-directory>` only when the complete material correction is contained in that
   directory. There are at most two routine local passes. A further pass requires consequential
   evidence, such as a failing required gate, a changed trust boundary, a new material surface,
   or a concrete newly demonstrated behaviour failure. `coderabbit review --light` is for active
   local development feedback, never a substitute for the committed baseline.

3. On GitHub, let the opening review establish the full PR baseline. The configured pause is a
   threshold of two **reviewed commits**, not two review requests, so it can pause before a
   desired later correction. For every later material commit after that pause, manually request
   `@coderabbitai review` and confirm that the current HEAD was reviewed. Request
   `@coderabbitai full review` only when the origin/main baseline materially changes; do not use
   a final gate alone as a reason for another full review.

4. Use additional CodeRabbit capabilities only on demand. Planning remains manual: comment
   `@coderabbitai plan` on a GitHub issue when a plan is needed. For a named test gap, comment
   `@coderabbitai generate unit tests` on the PR; inspect any generated commit or PR as a new
   candidate subject to the same receipts and gates. The enabled Fix CI finishing touch is used
   only for a concrete CI failure through the action shown in the CodeRabbit walkthrough; do not
   guess an unverified PR command. If the available action is unclear, use `@coderabbitai help`.
   Neither generated tests nor a CI fix may auto-merge, call live services or pods, or edit
   governed files.

5. Preserve reviewer evidence with `candidate.py receipt` after the proportionate independent
   review. Report dispositions honestly; `@coderabbitai resolve` may close a thread but does not
   establish correctness or waive hard rule 14. Run the final gate on the exact reviewed
   candidate and push it without amendment.

The repository YAML is the shared GitHub configuration; validate edits locally with
`coderabbit config validate .coderabbit.yaml`. After publication, request
`@coderabbitai configuration` and inspect the effective settings and their sources, including
any organization overrides. For public repositories where opening reviews do not trigger,
request `@coderabbitai review` manually and verify its completion. A configured tool or a
passing schema validator is not evidence that a remote review actually ran.
