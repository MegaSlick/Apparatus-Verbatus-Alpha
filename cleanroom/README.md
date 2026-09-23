# cleanroom

**The rebuild bench.** What sits here is written new in this repository, or carried under
the rule below, named and justified, never silently. CLAUDE.md's Quarantine section
points here.

## What may cross

**The standard is academic: read it, reason past it, and cite what you take.** The
offence is the silence, not the borrowing. (Tyrel amended the original absolute ban to
this; his wording, "cite but don't plagiarize", and its date are in the standing ledger.)

**Old code is reference only.** No seat is given it; the rebuild is planned from the
design notes. Where a session reads the old system on the host, work the problem out
first, then look at how it was solved before, then build the better version. A line
carried from it crosses only when it is genuinely the best option, understood well enough
to defend line by line, and **named as carried in both the commit and the report**.
Adapted, renamed or reformatted counts as copied. An unnamed carry is a finding at
review.

**A third-party library is usually the right answer.** Don't rebuild what a maintained
project does well. It enters under a licence that permits the use, with its source and
licence recorded beside the code; a borrowed snippet follows the same terms.

External reference paths are never written into the repository; they differ on every
machine and belong in gitignored local settings.

## The tray

Drafts sit in one folder per system, tracked on work branches so reviewers and CodeRabbit
read them raw. The ingress check refuses secrets, undeclared binaries and oversized
payloads here like anywhere else, but **it does not recognise register text**: never put
register content in a draft, even to test with. Use the synthetic fixtures.

From the tray, what survives review moves to its proper place and is reviewed again
there. **A pull request may carry a loaded tray while under review, but may not merge
until the tray is empty** — the `cleanroom-empty` CI job fails while it is loaded.
Empty is this directory's resting state.
