# pagekit clean-room log

This log is append-only: new entries go at the end, and an entry already on main is
never changed (a test enforces it). Each entry is numbered and written in plain words.
An entry's date and time are those of the commit that added it; `git log` on this file
shows them. Dates written inside an entry are dates of the events it describes. What
each entry holds, and why it does not list "sources used", is in CLEANROOM.md, "The
record".

## 0001 — Slice 1 built: the crop check

- **Who:** a build-side agent, Claude Opus 5.5.
- **Brief:** `briefs/0001-build-slice1.md`, sha256
  c0e21f2dc553987cce059de6f4c4e90c0e54f10b9a8523880a0cb71f65c51754. The host issued it
  at 2026-10-02T16:36:21Z. It was not saved when issued; it was recovered word for word
  from the session transcript on 2026-10-02 and saved afterwards.
- **Spec:** none before the code. `specs/0001-crop-check.md` was written afterwards,
  from the built behaviour and its tests (entry 0002).
- **Commits:** 9fe67dbe (the crop checker and its tests), a935c55a (README and NOTICE).
- **What happened:** the agent built the crop check under the clean-room rule in its
  brief, the rule pagekit's README states as its airlock: do not read, search for,
  fetch or reproduce ScanTailor or ScanTailor Advanced source, or any other GPL
  page-processing code. There was no reading-side input: no reading-side agent had
  run and there were no finding reports. Nothing more than this is claimed.

## 0002 — The clean-room process built

- **Who:** a build-side agent, Claude Opus 5.5.
- **Brief:** two messages from the host, saved as `briefs/0002a-build-cleanroom-task.md`
  (sha256 be1a33e72ef5a4060bc286d6b6145d0e93abc80c04559c96f090b8a8b150f5a1) and
  `briefs/0002b-build-cleanroom-decisions.md` (sha256
  920d43d780a80ae81678fa6dfda075afc5d540aeb55144d51fa89558a42bf9f9). Both were copied
  word for word from the session transcript after the agent finished.
- **Spec:** none. This session built the clean-room process and its checks, not
  page-processing code; the spec step applies to pagekit slices.
- **Commits:** ab91e2e5 (finding-report check and leak scan), 1f888ec6 (commit gate in
  the pre-commit hook), db580052 (agent settings that refuse commands naming the
  projects; needs the lead's approval at merge), 586437e0 (the protocol, templates,
  spec 0001 and brief 0001), and the commit that adds this entry (this log and the
  tests of the record).
- **What happened:** the agent wrote the protocol in `CLEANROOM.md`, the brief, report
  and incident templates, the report check, the leak scan, the HOLD rule in the
  commit hook and in CI, and the tests that keep this log append-only and the saved
  briefs true to their digests. It wrote spec 0001 after the fact from slice 1's
  README and tests, and saved brief 0001. It wrote no page-processing code and did not
  open the files named in entry 0003. One limit it could not close: agent settings
  can refuse web fetches only for a whole site, so fetches of the two projects'
  repositories are forbidden by briefs, not refused by settings.

## 0003 — Known pre-existing item: the ScanTailor bridge

- **Who:** recorded by the build-side agent of entry 0002 at the host's direction.
  The agent did not open the files.
- **What:** before pagekit existed, the repository gained a bridge to ScanTailor
  Advanced's project files: `operations/operator/scantailor_worker.py`,
  `operations/triage/scantailor_bridge.py`, `operations/triage/scantailor_project.py`
  and the ScanTailor sections of `pipeline/0_triage/CONTRACT.md`. Their comments say
  they were written from reading ScanTailor Advanced's project-file writer.
- **Standing:** they are not part of pagekit and are outside its allowed sources. No
  build-side agent may open them. Other open pull requests remove them, and they must
  be gone before any reading-side session starts.

## 0004 — The commit gate refused a brief on a false match

- **Who:** recorded by the build-side agent of entry 0002 at the host's direction.
- **What happened:** when the host committed brief 0002a, the commit gate refused it.
  The leak scan's copyright rule had matched a sentence in the brief that describes
  the rule itself, not an actual copyright notice. Nothing from ScanTailor was
  involved, and no incident was opened: this was the rule being too broad.
- **What was done:** the rule was narrowed in commit a1cedf82 to match only real
  notices (the word with (C), the copyright sign or a year, then a project or author
  name), with tests that such prose passes and notices in several formats are caught.
  The brief was then committed unchanged in 434deeb0.
- **Brief:** `briefs/0004-narrow-copyright-rule.md` (sha256
  1b8639a0d5a68880b113f905ddce0ef97fef860a847c46d705033a62d3b9a9ee), copied word for word from the
  session transcript after the agent finished.

## 0005 — A second false match on a brief, and the rule now looks only at headers

- **Who:** recorded by the build-side agent of entry 0002 at the host's direction.
- **What happened:** the commit gate refused the host's commit of brief 0004 for the
  same reason as in entry 0004: a sentence in the brief describing the copyright rule
  looked like a notice to the rule. Nothing from the other project was involved, and
  no incident was opened.
- **What was done:** in commit ebd3d96b the rule was changed to look only at header
  lines, where real copyright notices sit: a line that starts with the notice, perhaps
  after a comment mark. A notice mentioned in the middle of a sentence no longer
  matches. Tests check that both brief lines refused by mistake now pass and that
  notices in several formats are still caught. The brief was then committed unchanged
  in e066edae.

## 0006 — The lead's reading-side working document received

- **Who:** the lead, working with a Claude chat session outside this repository. The
  lead keeps that chat session, so its transcript can be checked later.
- **What:** a working document about ScanTailor Advanced, written in that session
  before this protocol existed. Received by the host session on 2026-10-02 at
  17:16:42 UTC: 2,585 lines, sha256
  f4d912e4caed42d5a1963db47c74547cc461487fd78445e5ce348b61cd331290.
- **What was done:** the host did not open it. The host ran the report check on it
  first. It failed on 920 counts: code marks (362), file names (193), line-number
  references (135), lines ending in a semicolon (129), sections outside the template
  (55), paths (30), braces (7), code tokens (6), and the template's structure. These
  are expected for working notes from a reading session, and they mean the document
  cannot go to the build side as it is. It is kept only in the local, git-ignored
  quarantine folder and is never committed. Neither the host nor any build-side agent
  has read it.
- **Next:** a reading-side agent distils it into finding reports (entry 0007).

## 0007 — Reading side: distil the lead's document into finding reports

- **Who:** a reading-side agent (Claude Opus 5.5), started after this entry was
  committed.
- **Brief:** `briefs/0007-read-lead-document.md` (sha256
  5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d).
- **Outcome:** recorded in a later entry once the host has run the report check on
  each report.
