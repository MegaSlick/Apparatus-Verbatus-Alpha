# You are in the autoclave

This is a container, not Tyrel's machine. Read this before you decide anything
about how to work, because several habits that are right on the host are wrong
in here, and one of them will waste your whole run.

**You may spawn your own agents, and you should when the job is large.** The clone you
are working in carries this repository's rules about agents, and some of those are
about agents running on Tyrel's machine — the spawn-depth cap and "`Agent` is denied to
every role" among them. **They are not about you.** Nothing you spawn can reach the
host, the container is already the bound, and a clone you wreck is rebuilt in seconds.
The cap is lifted in here deliberately; depth is yours to choose.

Two things do still hold. You are the only integrator of your own work — a sub-agent's
claim is evidence, not a finding, and you run the gate yourself at the end. And **no
agent of yours may edit a governed path**: the root documents, or anything under
`.claude/`. That is hard rule 10 and it does not stop at the container wall.

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
