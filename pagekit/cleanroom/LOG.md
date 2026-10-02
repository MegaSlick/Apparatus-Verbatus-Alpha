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
- **Brief:** [HOST TO FILL IN: `briefs/0002-build-cleanroom.md` and its sha256. The
  brief came as two messages from the host, the task and a later list of added
  decisions; both belong in the file.]
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
