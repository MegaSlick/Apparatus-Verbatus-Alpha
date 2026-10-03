# Brief 0002b: build the clean-room process (added decisions A to H)

- Role: build side.
- Issued by the host session to a Claude Opus 5.5 build-side agent at
  2026-10-02T16:50:24Z. This is the second of two messages that make up brief 0002, sent while the agent was working; where it differs from 0002a, it wins.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  920d43d780a80ae81678fa6dfda075afc5d540aeb55144d51fa89558a42bf9f9
- Saved by the host after the agent finished, copied word for word from the session
  transcript on 2026-10-02.

----- brief below this line, exactly as sent -----
Additional decisions from the second-opinion review (apply alongside your brief; where they differ, these win):
A. Add templates/incident.md with fixed plain-language fields: what was detected, where (in words), why it matters, the three choices (purge / minor breach / false flag) with a recommendation, and a `Decision:` line for the lead.
B. HOLD: the commit that deletes pagekit/cleanroom/HOLD must add an incident file containing a `Decision:` line — test it. The check reads the tree being checked (not branch names or env vars). CI is the real gate; say hooks can be skipped.
C. LOG.md append-only: CI test that origin/main's LOG.md (when it exists there) is a byte prefix of the branch's LOG.md; before LOG.md exists on main, the test passes and says why.
D. CLEANROOM.md must record, plainly: the repository already holds a ScanTailor bridge (operations/operator/scantailor_worker.py, operations/triage/scantailor_bridge.py and scantailor_project.py, and ScanTailor sections of pipeline/0_triage/CONTRACT.md) whose comments say it was written from reading ScanTailor Advanced's project writer; it predates pagekit, is outside pagekit's allowed sources, no build-side agent may read it, and it is being removed by other open PRs before any reading-side session. Log this as a known pre-existing item.
E. Drop per-session "sources used" claims (a model cannot truthfully enumerate them); record instead the brief (verbatim + digest) and the spec it worked from.
F. Host conduit: builder briefs are composed only from the builder template plus spec and passed finding-report files; check_report.py runs and is logged before any brief using a report is written; the host never writes pagekit code.
G. Release review replaces any similarity score: a reading-side agent's plain-language review for identical numeric constants, shared non-standard identifiers and mirrored function decomposition, reported without ScanTailor text; logged. Keep scan.py only for the deterministic tree checks (GPL headers, repo URLs, deny-hash matches).
H. Keep your leak scan in pagekit (do not edit .githooks/check_ingress.py — another open PR owns .githooks/ingress files); the pre-commit hook may call pagekit's scan for staged pagekit files and the HOLD check. Keep hook edits minimal and tested.