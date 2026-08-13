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
