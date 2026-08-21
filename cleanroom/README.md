# cleanroom

**The rebuild bench. What sits here is written new in this repository — or carried
under the citation rule below, named and justified, never silently.**

Called `autoclave/` until 2026-08-01. The containers agents work inside took that name
instead — they are the thing that actually sterilizes — and one word for two boundaries
made every sentence about either of them ambiguous. `operations/autoclave/` is the
chamber; this is the tray a draft waits in, tracked, so reviewers read it raw.

This project is a rebuild. A rebuilding model reads the reference where it lies —
`Temp_Stage`, an analysis output, and the frozen old repository — and writes its best
fresh expression of that system into this tray, line by line, in this project's
vocabulary. Both reference locations are intended to be read-only, but repository settings
are not an operating-system write barrier. Understanding crosses freely; bytes do not,
unless they earn it under the rule below.

## Citing the window, and citing a library

CLAUDE.md's Quarantine section names this file as the procedure. The standard is academic:
**read it, reason past it, and cite what you take.** The offence is the silence, not the
borrowing.

**The window is closed by default, and the rule below is what governs it if it reopens
(Tyrel, 2026-08-20).** Chambers no longer mount the old pipeline: the rebuild is planned
from the design notes now, and a session that wants the old code must set
`AUTOCLAVE_WINDOW` **while running `new`** for that one chamber — mounts are fixed when
the container is created, and the launcher refuses the variable at `dispatch` rather than
letting it read as a window that is not there. This section is therefore mostly dormant rather
than retired — it still binds the session reading the reference on the host, where both
locations remain readable, and it binds any chamber that is deliberately given a window.

**Reason first, then look.** Work out what the stage needs on its own terms, then read the
old code to see how it was solved before, then build the better version. Reading first and
reasoning backwards is how a workaround gets carried forward as though it were a design —
and the old pipeline was messy and broke often, which is why it is reference rather than a
source tree.

**A line carried from the old code** crosses only where it is genuinely the best option
available, is understood well enough to defend line by line, and is **named as carried in
both the commit and the report**. Adapted, renamed and reformatted are the same act as
copied. An unnamed paste is a finding at review, not a shortcut — and it is checkable,
because a reviewer can diff against the reference and nobody can audit whether a model
truly understood something.

**A third-party library is a different question, and the answer is usually yes.** Do not
rebuild what a maintained project already does well. It enters under a licence that
permits the use, with its source and licence recorded beside the code. Where a whole
dependency is disproportionate, a borrowed snippet is allowed on the same terms. The old
pipeline is Tyrel's own work and raises no licence question; a third party's is the
reverse, and that is the one to check before writing the line rather than after.

**Their paths are deliberately not written here.** They sit outside this repository and
differ on every machine, clone, sandbox and pod; a checked-in absolute path is wrong
everywhere except the one laptop it was written on, and it reads as a promise the
repository cannot keep. The session is told where they are — `.claude/settings.local.json`
is gitignored and machine-local, which is the right home for a path — and the handoff names
them when a rebuild is live.

The tray is tracked on work branches so the reviewers and CodeRabbit read the raw draft
exactly as it was written. The ingress check runs at this door like any other commit —
it refuses secrets, undeclared binaries and oversized payloads. **It does not recognise
register text**: transcribed prose is ordinary text to a scanner, and only the
line-by-line review catches it. No register content in a draft, even to test with — use
the synthetic fixtures.

From the tray, the sterilizing pass moves what survives to its proper place in the tree:
a review that can say, for every line, why it exists, what it serves, that it wears this
project's names, and that it squares with the goals and governance. A line that cannot
answer dies in the tray. The placed code is reviewed again in its final form.

**A pull request may carry a loaded tray while under review. It may not merge until the
tray is empty.** CI reports a loaded tray as a failing job; whether that job *blocks* the
merge is a GitHub branch setting, and README.md's status line records which rules are in
force — trust it, not this sentence. Branch history keeps a record of what the tray held;
that is acceptable in alpha, whose history never ships (README, Versions), and the
ingress check walks every reachable commit so no secret can ride along in one.

One folder per system while in use. Empty is this directory's resting state.
