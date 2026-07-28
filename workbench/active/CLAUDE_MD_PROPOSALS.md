# Proposed edits to CLAUDE.md — yours to apply or strike

Sol made seven edits to `CLAUDE.md` across its five rounds. Under your T22 ruling — *agents
propose, they never amend* — every one of them has been **reverted out of the push** and is
re-presented here as a proposal. Nothing is lost: the exact original diff is preserved at
`workbench/raw/2026-07-28_sol-loops/CLAUDE_MD_PROPOSALS.diff`.

Read these as suggestions from a reviewer. I have marked each with a recommendation and, where
it matters, what breaks if you take it.

---

## 1. A new hard rule 10 on secrets — **recommend: not as written**

> 10. **No secret in any file** — no token, topic, key or credential, including ignored notes,
>     evidence and transcripts. Report a suspected secret by location and kind; never quote it.

The instinct is right and the second sentence is worth having on its own. The problem is the
first: written this way it instantly condemns two credential-bearing evidence logs already
under `history/`, and so it quietly decides a question Sol elsewhere correctly reserved for
you — whether captured evidence containing a secret must be destroyed, redacted, or kept
sealed. A rule should not settle that as a side effect of its wording.

Also worth noticing on its own merits: **an agent wrote a new hard rule into the file that
governs agents.** That is precisely the move T22 exists to prevent, and it is the best
argument for the rule you are adding.

**If you want it, it needs the carve-out** — something like "evidence already captured is
sealed and reported, not quoted, and never newly created".

## 2. Subagents may write in a worktree's autoclave — **recommend: hold, tied to T11**

Rewrites "a worker may write in its own worktree or the autoclave" to allow the worktree's own
autoclave, and adds "subagents never write in the main checkout's live tree."

The added sentence is a genuine tightening and I would take it. The rest depends on the
worktree question (T11) that is still open, so it should move with that decision, not ahead
of it.

## 3. Machine-specific path in the quarantine paragraph — **recommend: no**

Sol wrote `/Users/tyrel/ocr_pipeline` into the rule. It is accurate on your laptop and wrong
in every clone, sandbox and pod — and it reads as a promise the repository cannot keep. I have
already fixed the same mistake portably in `autoclave/README.md`; CLAUDE.md needs no change.

The other half of that edit is worth keeping, though: the honest note that the Claude settings
deny native writes to the reference locations but are **not** an operating-system barrier, so
shell mutation is forbidden by the rule rather than prevented by the machine. That is true, it
was measured, and the file currently implies more protection than exists.

## 4. Branch deletion needs `ALLOW_BRANCH_DELETE` — **recommend: yes**

> Before deleting a remote branch, verify that its work is merged and no other session owns it.
> The pre-push alarm requires `ALLOW_BRANCH_DELETE=<branch>` as that exact-branch assertion; it
> cannot prove either fact from a deletion request.

This one simply documents a guard that **is** in this push. Without it the file describes a
push gate that no longer matches the hook. Straightforward catch-up; take it.

## 5. The roster paragraph and the model-cost description — **recommend: partly**

Sol replaced your "models in one breath" paragraph — Haiku a fifth of Opus, Sonnet half, Fable
twice — with an instruction to verify the current rate card before a large paid fan-out.

It is factually safer, and the underlying point is sound: those ratios are list-price facts
that drift, and a subscription does not burn the way an API bill does. But it deletes guidance
you wrote deliberately, and replaces vivid, usable shorthand with process. Fable flagged the
same thing.

**Recommendation: keep your paragraph, add Sol's warning after it** — the ratios as orientation,
plus one line saying they are approximate and to check the rate card before anything large.

The honest correction inside the same edit *is* worth taking: the roster fields state the
**requested** configuration, and the runtime's resolved model is what must be recorded. That
distinction is load-bearing for the reviewer pass.

## 6. Notifications rewritten around `NTFY_TOPIC` — **recommend: no, and this one is a trap**

Sol rewrote the section to say the topic comes only from the process environment and that
`private/ntfy.conf` is refused without being read.

**This is the change that would have silently switched off your phone.** Nothing currently
sets `NTFY_TOPIC`, so `start` and `milestone` would exit 0 reporting "notifications are off".
Your T20 ruling — a gitignored file is an acceptable home — makes the premise wrong anyway.
The whole notify group is held out of this push until a real phone test.

One line from it is worth keeping whenever notify does ship: `start` and `milestone` do not
fail their caller when delivery is unavailable, so **their exit status is not proof a phone was
reached**. That is a measurement-honesty point of exactly the kind GOVERNANCE 10 is about.

## 7. Session-end wording in a review-only session — **recommend: yes, minor**

Changes "still writes the handoff but moves and sends nothing" to "still refreshes the handoff
and next-session brief, but does no filing moves and sends no phone notification." More precise
about the two artefacts and the two suppressions. Harmless improvement.

---

## The T22 rule itself, for you to write in

Proposed wording, under **Hard rules**:

> **Agents propose; they never amend.** The governing documents — this file, GOALS,
> GOVERNANCE, ARCHITECTURE, GLOSSARY and the root README — are Tyrel's alone. An agent may
> suggest a change to any of them, with exact wording, in its report. It may not make one.
> A rule an agent wrote into the file that binds it is not a rule.
>
> Code is different and stays open: hooks, CI, the agent and skill files, operations and tests
> are written by agents and land through review like everything else. The line is between what
> *governs* and what *executes*.
