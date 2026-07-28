---
name: reviewer-pass
description: Runs the three-reviewer pass this repository requires before a push — one Opus, one Fable, one GPT, on an identical prompt, blind to each other — and records the receipt. Use only when Tyrel has said the work is settled enough to review.
disable-model-invocation: true
---

# Reviewer pass

**Ask Tyrel before running this, and ask him again before the push it clears.** Reviewing work
that is about to change again wastes the review and stales the finding. He decides when a piece
of work is settled, and separately whether it goes out. These audits are cheap set against a
defect reaching `main`, so the answer is usually yes — it is still his call, twice.

**Every push is reviewed. There is no push without a pass covering that exact commit.**

**The reviewed state is a commit.** Commit before the reviewers start — the receipt names
`HEAD`, and one recorded before the commit exists names the wrong state. If review had to
precede the commit, verify afterwards that the committed tree is byte-identical to what was
read, and record the receipt only then.

**Refuse an already dirty checkout before dispatch.** Record `reviewed_sha` from
`git rev-parse HEAD`, then require `git status --porcelain --untracked-files=all` to be empty.
Ignored evidence and prompt files may remain under `workbench/`; tracked or unignored changes
may not.

Do not ask reviewers to read the live checkout and call that exact-commit coverage. Another
session can change and restore it between point-in-time checks. Materialize `reviewed_sha`
into a new temporary directory with `git archive --output=<temporary-tar> "$reviewed_sha"`
followed by `tar -xf <temporary-tar> -C <empty-review-directory>`. Put `reviewed_sha` and that
directory in the shared prompt, and direct every reviewer to that snapshot. Keep the snapshot
unchanged through the pass. This is an operator procedure, not a repository lock; the receipt
remains an assertion about what was read.

Before dispatching either Claude seat, refuse the pass if
`CLAUDE_CODE_SUBAGENT_MODEL` or `CLAUDE_CODE_EFFORT_LEVEL` is set in the launch environment.
Those variables take precedence over the per-call model and agent-frontmatter effort,
respectively. Name the variable, never its value, and restart from a clean environment; a
requested Opus/high seat that silently resolves differently is not the required seat.

After each Claude seat returns, inspect the runtime metadata when the client exposes it and
record both resolved model and resolved effort in the full report. If either differs from the
approved seat, that response does not satisfy the pass: preserve it as evidence, then rerun
the seat or ask Tyrel to approve that named deviation. Frontmatter records the request, not
what the runtime actually supplied.

**The reviewers audit and report. They never push and never merge.**

## Triage first

Size the push before summoning anyone. One short paragraph: what changed — files and the
nature of the change, not just line counts — what class of work it is, what a defect there
could cost, and the coverage you recommend: which reviewers, at what effort.

There is no scoring scale — use sense, and say the reasoning out loud. A huge diff
deserves heavy coverage and possibly repeated passes; a one-line fix deserves little;
anything touching money, launch, shutdown or a governance rule gets the full set,
sometimes more than once. A vendor sitting near its usage cap is a triage fact, not a
mid-pass surprise: name the substitute or the reduced set now, in the recommendation.
Merge commits with substantive conflict resolutions are part of the reviewed state —
name them in the scope.

**The recommendation decides nothing.** Tyrel approves or overrides it, under the rules in
"When Tyrel reduces it" below. A reduction that keeps two should keep two *vendors*.

## The three

Write the prompt once to a file, then give that file's bytes to all three, blind to each
other. Do not tell any of them that another is reading the same thing. Reconstructing the
prompt three times is not an identical-prompt mechanism.

- **Claude Opus 5** — a subagent with the model set to `opus`
- **Claude Fable 5** — the same, with the model set to `fable`
- **GPT-5.6 Sol (OpenAI)** — `sh operations/codex/seat.sh judge - < "$prompt_path"`.
  The tracked seat supplies model, effort, sandbox, working root, and a positive timeout;
  a bare `codex exec` inherits machine policy and has no repository-owned ceiling. Before
  dispatch, set `report_dir` to this pass's exact raw-evidence directory and create it. Capture
  the complete call rather than relying on the chat transcript:

  ```sh
  set -eu
  gpt_report="$report_dir/gpt-sol.log"
  gpt_output=""
  if gpt_output=$(sh operations/codex/seat.sh judge - < "$prompt_path" 2>&1); then
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
  [ ! -e "$gpt_report" ] || {
    unset gpt_output
    echo "GPT report target already exists; refusing to overwrite evidence" >&2
    exit 1
  }
  gpt_temporary=$(mktemp "$report_dir/.gpt-sol.log.XXXXXX") || exit 1
  trap 'rm -f "$gpt_temporary"' EXIT HUP INT TERM
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

  **Run this as a script, not pasted line by line.** `set -eu` is what makes each guard
  stop the pass; a bare `false` only prints and carries on, and the "refusing to overwrite
  evidence" branch then overwrote the previous reviewer's report with an empty file — the
  exact loss hard rule 7 forbids, inside the procedure that produces the push gate's own
  evidence.

  A failed, nonempty call remains evidence only when its output passes the credential
  scan. It never earns a receipt. An empty call has no evidence to preserve.

### When Tyrel reduces it

Three is the standard. He may cut it to one or two, and when he does:

- It must be **explicit and for that request only**. "Just Opus this time" reduces this pass
  and no other.
- It is **never inferred**. Silence, impatience, a tight budget, a small diff, or a previous
  reduction are not permission. Ask rather than assume.
- The next push starts again at three. A reduction never carries forward, and no sequence of
  reductions establishes a new normal.
- Record what actually ran. `pre-push` prints the coverage as a checklist and pushes either
  way, so nothing forces the record straight — which is exactly why it must be written
  honestly. The receipt shows the real coverage, never the intended coverage.

A reduction is Tyrel spending his own safety margin knowingly. Do not spend it for him.

**Two vendors, not one, and that is the point rather than belt-and-braces.** A reader that
shares your blind spots only confirms them. A reader built differently finds what the others
cannot see, which is not a theory — it is the observed reason this rule exists.

## Writing the prompt

**Ask for everything and filter afterwards.** Never write a severity floor, a confidence
budget, or "only report serious issues" into the prompt — those instructions are followed
literally and produce fewer findings rather than better ones. Never tell a reviewer which way
to argue, and never show it another reviewer's verdict; an anchored critique is not a blind
review, and the difference does not show up in the output.

Tell every reviewer not to reproduce a suspected secret value. It reports the path, line and
kind instead. A review that copies a secret into its own report has enlarged the incident.

**When the change replaces a file, give the reviewer the file it is being replaced *with*.**
A prompt that hands over only a diff spends the whole review budget on what moved, and the
question that matters is whether the new file is right — which a diff cannot answer, because
a clean diff of a wrong file still reads as clean. Show the new file whole. Showing the old
one as well is fine and often useful; showing only the old one and the delta is not.

**Ask each finding to carry what the reviewer would do instead** — a proposal, not a patch.
Reviewers propose and never apply. The session verifies a proposal like any other claim
before adopting it; adopting one is a decision, not a default, and agreement between
proposals settles nothing that is Tyrel's.

## Reporting what they found

**Scan every reviewer response before its first write.** Keep the response in the client or
process memory, pipe its complete bytes through
`python3 .githooks/check_ingress.py --stdin-file`, and write it only after that returns 0.
The GPT command above is the reference mechanism. Use the same scan-before-persist order for
both Claude responses; a report is untrusted output, not an exception to the no-secret rule.

**File every reviewer's full, clean report as local evidence first** — under
`workbench/raw/<date>_<short-commit>_reviewer-pass/` — before recording the receipt. This is
the drawer for verbatim reviewer output; putting the reports in `active/` breaks that drawer's
one-sitting budget. Verify that each local report is complete and nonempty.

Before recording any receipt or copying any report to the pull request, scan every new report
explicitly; `raw/` is ignored, so the ordinary worktree scan cannot see it:

```sh
[ -n "${report_dir:-}" ] && [ -d "$report_dir" ] || {
  echo "review report directory is unset or missing" >&2
  exit 2
}
for report in "$report_dir"/*; do
  python3 .githooks/check_ingress.py --file "$report" || exit
done
```

Set `report_dir` to this pass's exact directory first; never point the loop at all retained
evidence. This post-write pass is defense in depth; it does not replace the scan before the
first write. The scanner recognizes known credential forms, not every possible secret. If the
pre-write scan fails or finds one, stop without writing the response, record only its intended
path and scanner classification, and tell Tyrel. Never copy the value. If the defense-in-depth
scan finds something the pre-write scan missed, stop: do not record the receipt or upload the
report, and report the local path and classification so Tyrel can decide the
evidence-versus-secret conflict without enlarging it.

Once the pull request exists, copy each full report into a pull-request comment (split it
across comments if the service's size limit requires it) and verify that the comments are
visible there. Until that happens, say plainly that the reports exist only on this machine
and name the local paths in the handoff. A gitignored local file is not machine-durable, and
a finding that exists only in a transcript is lost, whatever produced it (Governance 2).

Report what they agree on and **keep their disagreements** rather than blending them into one
answer. A difference between two models is information, and averaging it away destroys it.

Verdict-level agreement is not agreement. Two reviewers can reach the same conclusion for
incompatible reasons. Report disagreement as findings added, findings overturned, and
governance calls reversed.

**Agreement between reviewers is evidence, not a verdict.** It never settles a governance
question, a permission, or an exclusion. Those are Tyrel's, and unanimity among models does
not stand in for him.

## Recording it

Before writing any receipt, re-run `git rev-parse HEAD` and
`git status --porcelain --untracked-files=all`. If `HEAD` differs from `reviewed_sha`, or the
status is no longer empty, do not record the pass: the reviewed state changed. Preserve the
reports as evidence, settle the tree, and run a fresh pass on the new commit. These checks
catch visible drift; the commit snapshot above is what keeps the reviewers' input stable.

```sh
.githooks/record-audit.sh 'Claude Opus 5' '<what it found>'
.githooks/record-audit.sh 'Claude Fable 5' '<what it found>'
.githooks/record-audit.sh 'GPT-5.6 Sol (OpenAI)' '<what it found>'
```

**The receipt names one commit.** Amend it or add another and the work must be audited again —
an audit is of a state, not of a branch.

**Record the model that actually answered, not the one you asked for.** The gate counts three
*distinct* names; it does not hold a list of approved ones, because a hook carrying today's
product names dates at the next release and would then force either a bypass or a receipt that
lies. The names above are the current roster, not a required vocabulary — if a seat resolved to
something else, write what it resolved to. Put the resolved release and effort in the full
report as well, where the runtime exposes them. A label is a claim, not proof.

A session that cannot summon all three records the ones it can. `pre-push` will print the
shortfall as unticked boxes and push anyway — it is a checklist, not a gate, and Tyrel judges
the coverage himself. Record the reviewers you did run, so what he reads is what actually
happened rather than what was meant to happen. **Nothing stops you writing a flattering
receipt, which is the whole reason not to.**

## Afterwards

**The automated reviewer on the pull request is Tyrel's to relay.** Do not sit polling it. When
he points at a comment, **verify the claim before acting on it** — some are style, some are
simply wrong, and some are real. Reproduce it first, fix what is real, say plainly why you are
skipping the rest, and record a fresh receipt for the commit that answers it.
