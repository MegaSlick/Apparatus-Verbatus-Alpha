# autoclave

**The cleanroom bench. Only code written new, inside this repository, ever sits here.**

This project is a rebuild. Old code never crosses the boundary — not copied, not pasted,
not "ported". A rebuilding model reads the reference where it lies (`Temp_Stage`, the
frozen old repository — read-only, through the window) and writes its best fresh
expression of that system into this tray, line by line, in this project's vocabulary.
The knowledge crosses; the bytes never do.

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
