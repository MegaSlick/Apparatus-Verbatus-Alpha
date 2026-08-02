---
name: governed-edit
description: The procedure for changing a governed path — CLAUDE.md, the root documents, or anything under .claude/. Read this before proposing or applying any such change, including a one-word one.
---

# Changing a governed path

A governed path binds every later session. That is the whole reason this procedure
exists: the cost of a wrong edit is not paid now, it is paid by a session six weeks out
that reads the file and believes it.

**The paths this covers** are the ones CLAUDE.md lists under Where notes go. Read them
there rather than from a copy: that list is the single source, `guard.py` names it as
such, and a second enumeration here would be the drift this procedure exists to prevent.

**Who may do it.** The main session, at Tyrel's direction and after asking. Never a
spawned agent, by tool or by shell — hard rule 10. An agent proposes exact wording in its
report and stops there. Changing the doctrine is not an override: push back firmly before
agreeing to a change, and make sure he holds what a later session will read it as.

## 1. Read before you propose

Bounded, in this order. The point is to find what depends on the thing you are changing,
not to walk the whole tree.

1. **Read the target file in full.** Not the section — the file. A rule three sections
   away is often the one your edit contradicts.
2. **Read every file the target names as an authority or a procedure owner.** The routing
   table at the top of CLAUDE.md is the list for CLAUDE.md. Follow evidence and
   measurement links only when your proposed claim actually depends on them; a dated
   measurement file is not an authority and reading it does not improve the wording.
3. **Search the tracked tree for what you are about to change** — the file path, the
   heading you are touching, and any rule number in it. Read every hit in its own
   context. This is the step that is skipped, and it is the step that catches the damage.

## 2. The sweep that is always required

Three couplings have already gone stale in this repository, so check all three every
time:

- **Section names.** `.claude/hooks/guard.py` quotes CLAUDE.md section names back at a
  session in its refusal text, and `.claude/skills/session-start/SKILL.md` cites them
  too. Rename or move a section and a refusal at three in the morning points at a section
  that does not exist. `.githooks/pre-push` already cites a CLAUDE.md heading, "The commit
  is the record", that is not there.
- **Hard-rule numbers.** They are cited by number in the guard, its tests, two git hooks,
  the chamber briefs and a pipeline test. A number is never reused and never inserted —
  a new rule is appended.
- **Counts and inventories.** A number stated in two documents is a number that will
  disagree. `README.md` says the guard refuses six things; CLAUDE.md and the guard's own
  `CHECKS` say seven. Prefer making the inventory one checked fact over correcting the
  second copy.

## 3. What the machinery does and does not cover

Do not assume a file is protected because a document says it is. As things stand:

- The guard matches a governed target by **basename** or the literal `/.claude/`. So every
  `README.md` anywhere is protected by accident, and a new governed file at a new path is
  not protected at all until the guard is taught about it.
- The guard refuses the `Write` tool into `.claude/` but **not** an ordinary shell
  redirect. Known, recorded, and deferred by Tyrel.
- `.githooks/doc-allowlist.sh` decides which documentation files may be tracked at all,
  and CI runs it on every pull request. A new document at a new path is refused at commit
  until it is admitted there, one level deep, following the existing bounded patterns.
- `.githooks/check-documents.sh` checks that the six canonical documents exist and
  date-scans five of them. A new governed file is in neither list.

If your change adds a governed file, say plainly which of these it is and is not covered
by. "It is governed" is a claim about the document, not about the machinery.

## 4. Propose, then apply

- **Propose exact wording**, in the session, in words he can act on — never in a file he
  has to open, never as a poll. Name what the change costs as well as what it buys.
- **A structural change gets read first.** More than a sentence or two, or anything that
  moves a rule between documents, goes past review seats before it lands — mixed vendors,
  because agreement between two seats from one vendor is the weakest kind of agreement.
- **Apply only after his clear answer**, and only the change he approved. A gate
  authorizes one exact action and nothing adjacent.
- **Land the coupled changes in the same commit** as the wording. A pointer that lands
  before its target dangles; a target that lands before its pointer is unreachable.
- **Record what actually happened** — if a sweep found nothing, say the sweep ran and
  found nothing. A check that was not run is not a check that passed.
