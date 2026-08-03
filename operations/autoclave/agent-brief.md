# You are in the autoclave

This is a container, not Tyrel's machine. Read this before you decide anything
about how to work, because several habits that are right on the host are wrong
in here, and one of them will waste your whole run.

**Your dispatch is the opt-in, and there is no session to ask.** If your brief says
`ultracode`, or asks you to fan out, that *is* the explicit permission this repository's
rules ask for — a session wrote it, deliberately, before sending you in. Nobody is
watching this run and there is no one to confirm with, so waiting for confirmation means
never orchestrating at all. A seat deadlocked itself exactly that way on 2026-08-02: it
held both the `Agent` and `Workflow` tools, was told to use them, and declined because it
wanted an in-session confirmation that could not exist.

**You may spawn your own agents, and you should when the job is large.** The clone you
are working in carries this repository's rules about agents, and some of those are
about agents running on Tyrel's machine — the spawn-depth cap and "`Agent` is denied to
every role" among them. **They are not about you.** Those bounds exist because a host
agent runs on Tyrel's machine and you do not; the container is the bound instead, and a
clone you wreck is rebuilt in seconds. The cap is lifted in here deliberately; depth is
yours to choose.

**What the container bounds is this machine's checkout, and not much else.** An earlier
edition of this file said nothing you spawn can reach the host, and that was too strong.
`/out` is a real host directory and everything you put there survives you. Your vendor's
configuration volume is writable and outlives the chamber, shared with every later
chamber of the same vendor. Network egress is open. Spend is real and no review returns
it. Read that as a reason to keep what you spawn bounded, not as a licence.

**Before you fan out, read `/work/.claude/agents/README.md`.** It is measured and this
file is not: which seat, which effort, and what each actually costs are there. Give each
agent a bounded unit with named stop conditions, have it land its result **on disk**
rather than only in a reply, and read that result back yourself before you use it. You
delegate the work, never the responsibility for it.

**Your sub-agents share this clone, and there is only one git index in it.** That is the
part the container does not isolate for you. Two agents staging at once corrupt each
other's commit, and one that switches branches moves the tree under every other. So:

- **Give each agent its own result path** and name it in the prompt — one file per
  agent, never a shared one. Two writers on one path is a lost result, not a merge.
- **Name the paths each agent may write**, and keep those sets disjoint. Where two
  units genuinely need the same file, run them in sequence and integrate yourself.
- **No sub-agent touches git.** No `add`, no `commit`, no `switch`, no `checkout`, no
  `stash`, no `reset`. They write files; you stage and commit, after reading what they
  wrote. Lift this only for a unit whose whole task *is* a git operation, and then run
  it alone.

Two things do still hold. You are the only integrator of your own work — a sub-agent's
claim is evidence, not a finding, and you run the gate yourself at the end. And **neither
you nor any agent of yours may edit a governed path**: the root documents, or anything
under `.claude/`. That is hard rule 10 and it does not stop at the container wall — this
said "no agent of yours" until 2026-08-03, which read as a rule about the agents you
spawn and left you out of it. It never did. A branch of yours that changes `.claude/**`
binds every later session exactly as an edit on Tyrel's own machine would. Propose exact
wording in your report; the session applies it, at his direction and after asking.

**This file exists at `/work/AGENTS.md`, `/CLAUDE.md`, `/AGENTS.md` and
`/work/AUTOCLAVE.md`, and all four are byte-identical copies of one another.**
Whichever your CLI looks for, you have found it; there is no fifth file with different
rules and no need to hunt for one. The repository's own rules are separate and live at
`/work/CLAUDE.md` — read those too.

## Where you are

- `/work` — your own clone of the repository, on your own branch. Write in it
  freely, within the task you were given, with one exception that is not yours
  to make: the **governed paths** listed in `/work/CLAUDE.md` under Where notes
  go — the root documents and `.claude/` entire. A spawned agent never edits
  those (hard rule 10), and neither do you. Propose exact wording for them in
  your report instead.
- `/src` — the host repository, mounted **read-only**. Reference only. Writing
  here fails, and the failure is the mount, not a permission you can argue with.
- `/out` — a scratch drawer shared with the host. This is how work leaves.
- `/specs` — the design specs, frozen when this chamber was made and **yours to
  write**. `spec_NN_*.md` is the written source for any system you are asked to
  build; the master plan and the notes beside it are not here, and the brief you
  were given carries whatever else you need. They live outside git on the host,
  which is why they arrive by their own mount rather than inside `/work`. This is
  a copy made for you, not the operator's original, so record what your brief
  tells you to record. Say in your report what you changed here — it does not
  travel back on your branch, so an unmentioned edit is an edit nobody reads.
- `/window` — **present only when this chamber was given one**, and it is the old
  repository, mounted read-only. It is *reference*, and the rule on it is
  absolute: **no byte of it enters your branch.** Read it to understand what an
  old system did, then write the replacement new and justify it against the
  governing documents. Copying, adapting line by line, or reproducing a file from
  it with the names changed are all the same act and all refused — CLAUDE.md,
  Quarantine. If you cannot justify a line you wrote, delete it. Say in your
  report what you read there and what you took from it in *understanding*, so the
  operator reading your diff knows where to look hardest.

Everything else in this filesystem is the container's and vanishes when it does,
with one exception you should know about. If your vendor has been signed in,
your CLI's own configuration directory — `/home/agent/.claude` or
`/home/agent/.codex`, whichever this chamber was created for — is a named Docker
volume mounted **read-write**, so the one sign-in Tyrel did is not asked for
again. It outlives this container, and every later chamber of the same vendor
mounts the same volume. Treat it as shared state: what you write there reaches
the next agent, so change nothing in it your task did not ask for.

## What you can do freely

- Write code. Install packages. Create files. Delete files.
- **Run the tests.** `python3 -m pytest`, and `ruff check .` — both are on the
  PATH already. Run them on your own work before you hand it back; that is the
  main reason this chamber exists.
- Make as many commits as the work wants. They are yours and nobody reviews
  their granularity. Each one needs a `Co-Authored-By` trailer naming the model
  that wrote it — this clone has the repository's hooks installed, so a commit
  without one is refused, and `collect` names any that got through anyway.
- **Stage only what you touched for your task. Never `git add -A`.** That is
  CLAUDE.md's Branches section and it holds in here for a reason of its own: you
  may not be the only thing writing in this clone. Your own sub-agents are, and a
  bulk stage sweeps their half-finished work into a commit you then report as
  yours. If a file changed under you, re-read it rather than staging over it.

## What you cannot do, and must not try to route around

- **Write the host.** `/src` is read-only by mount. You can read most of it; you
  cannot change any of it. `private/`, `workbench/` and `scriptorium/` are masked
  and will look empty — that is deliberate, not a broken checkout.
- **Push anything.** There are no git credentials in here and there will not be.
  The session that dispatched you does the pushing, after it reads your work.
- **Open a pull request.** Not yours, not from here.
- **Read another vendor's credential.** A chamber carries at most one, and only
  when it was created for that vendor.

Network egress **is** open — the CLI you are running needs its provider. Do not
read that as permission to fetch whatever you like: it is a limit stated honestly
rather than a door held open.

If you hit one of these, **stop and say so in your report.** Do not look for
another spelling, another tool, or another route. A blocked action reported
plainly is a useful result. A blocked action worked around silently is the
failure this whole arrangement exists to prevent.

## How your work gets out

1. Commit to your branch in `/work`. That is the deliverable.
2. If you were asked for a written report rather than code, write it to
   `/out/report.md`. Not to stdout, not only into your final message — a file
   survives you and a message does not.
3. Stop. The session collects the branch, reads every line of the diff, and
   decides what enters the real repository. Nothing you do lands on its own.

## What is expected of the work

The repository's own `CLAUDE.md`, `GOALS.md`, `GOVERNANCE.md` and
`ARCHITECTURE.md` are in `/work` and they bind what you write here exactly as
they bind the host. Read them. Two that catch people out:

- **Nothing is lost silently.** A partial result is reported as partial. A test
  you did not run is named as not run. A thing you could not do is said out loud.
- **Do not build a picker.** Nothing in this pipeline selects among witnesses,
  under any name. If your task seems to require one, stop and say so.

Stay inside your task. You were dispatched for one thing; the diff you hand back
should be that thing and not a tidy-up of whatever else you noticed. Note the
rest in your report and let the session decide.

## If something is missing

The image carries git, Python 3.13 with pytest and ruff, Node 22, ripgrep and a
compiler. If your task needs something else, install it and **say in your report
what you installed and why** — the next session decides whether it belongs in
the image or was a one-off.

If something is missing that you cannot install, say so and stop. Do not
simulate the step, do not stub it out and carry on as though it passed, and do
not report a task complete that you could not finish.
