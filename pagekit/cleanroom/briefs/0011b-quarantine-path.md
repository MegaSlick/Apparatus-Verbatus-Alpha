# Brief 0011b: name the quarantine folder actually used

- Role: build side.
- Issued by the host session to the Claude Opus 5.5 build-side agent of entry 0002 at
  2026-10-02T18:22:28Z.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  91508d750c8078114d9996faab068689132e87a234eec13eca556db71fa41543
- Saved by the host after the agent finished, copied word for word from the session
  transcript on 2026-10-02.

----- brief below this line, exactly as sent -----
One fix: the quarantine folder actually used (and logged in 0006–0008) is `workbench/cleanroom-quarantine/` (reports in `workbench/cleanroom-quarantine/reports/`), not `workbench/quarantine/`. Make CLEANROOM.md (and any template or doc in pagekit that names the folder) use the real path consistently. Amend nothing; new commit with trailer `Clean-room: build` plus the usual two. Run pagekit tests and check-static; report the hash. Don't touch LOG.md.