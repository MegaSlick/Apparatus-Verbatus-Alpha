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

Give all three an **identical prompt**, blind to each other. Do not tell any of them that
another is reading the same thing.

- **Claude Opus 5** — a subagent with the model set to `opus`
- **Claude Fable 5** — the same, with the model set to `fable`
- **GPT** — `codex exec --sandbox read-only "<prompt>"`

### When Tyrel reduces it

Three is the standard. He may cut it to one or two, and when he does:

- It must be **explicit and for that request only**. "Just Opus this time" reduces this pass
  and no other.
- It is **never inferred**. Silence, impatience, a tight budget, a small diff, or a previous
  reduction are not permission. Ask rather than assume.
- The next push starts again at three. A reduction never carries forward, and no sequence of
  reductions establishes a new normal.
- Record what actually ran. The gate counts three and does not grade on a curve, so a reduced
  pass needs `ALLOW_UNAUDITED_PUSH=1` — and the receipt should then show the real coverage
  rather than the intended coverage.

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

**Ask each finding to carry what the reviewer would do instead** — a proposal, not a patch.
Reviewers propose and never apply. The session verifies a proposal like any other claim
before adopting it; adopting one is a decision, not a default, and agreement between
proposals settles nothing that is Tyrel's.

## Reporting what they found

**File every reviewer's full report durably first** — under `workbench/active/` or on the
pull request — before recording the receipt. A finding that exists only in a transcript is
lost, whatever produced it (Governance 2).

Report what they agree on and **keep their disagreements** rather than blending them into one
answer. A difference between two models is information, and averaging it away destroys it.

Verdict-level agreement is not agreement. Two reviewers can reach the same conclusion for
incompatible reasons. Report disagreement as findings added, findings overturned, and
governance calls reversed.

**Agreement between reviewers is evidence, not a verdict.** It never settles a governance
question, a permission, or an exclusion. Those are Tyrel's, and unanimity among models does
not stand in for him.

## Recording it

```sh
.githooks/record-audit.sh <auditor> '<what it found>'
```

**The receipt names one commit.** Amend it or add another and the work must be audited again —
an audit is of a state, not of a branch. The receipt also names the model that *actually
answered*, not the one requested: a mis-resolved alias silently replacing one reviewer with
another must be visible in the record.

A session that cannot summon all three records the ones it can, and then needs
`ALLOW_UNAUDITED_PUSH=1` to push: the gate counts three and does not grade on a curve. Record
the reviewers you did run anyway, so the receipt shows what the coverage actually was rather
than what it was meant to be.

## Afterwards

**The automated reviewer on the pull request is Tyrel's to relay.** Do not sit polling it. When
he points at a comment, **verify the claim before acting on it** — some are style, some are
simply wrong, and some are real. Reproduce it first, fix what is real, say plainly why you are
skipping the rest, and record a fresh receipt for the commit that answers it.
