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
through the candidate and
writes exact `Candidate: <full SHA>` and `Base: <full SHA>` lines. `receipt` refuses a
moved `HEAD`, dirty tree, empty report, symlink, wrong SHA, or either missing identity. It writes a local
JSON receipt under `workbench/raw/reviews/<candidate>/` binding the reviewer, candidate,
base, report path, report digest, and candidate tree.

Fixing a finding creates a new candidate. Every earlier receipt is then stale; run the
required independent reviews again. Run the final gate while `HEAD` is that reviewed
candidate, and push that exact commit without amending it. Review reports are local evidence
and stay out of Git. A `Reviewed-by:` trailer may be added only after that reviewer reports;
because amending changes the commit SHA, the amended result is a new candidate and must pass
the required reviews itself before push.
