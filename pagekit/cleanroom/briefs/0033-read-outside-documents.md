# Brief 0033: distil two outside documents into finding reports

- Role: reading side.
- Saved by the host before the reading-side agent started, at 2026-10-04T15:34:49Z. The agent's
  task message gives it this file's path and this sha256 and nothing else.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2
- Departure from the reader template, and why: as in brief 0007, the agent may write new
  report files into the local, git-ignored quarantine folder, so the host can run the
  report check before opening any of them. The agent's reply is only file names and
  sha256 digests.

----- brief below this line, exactly as sent -----
You are a reading-side agent for pagekit's clean room. You stay on the reading side for your whole life: you never edit or commit anything in the repository, and you are never given build work afterwards.

Task: distil two outside reading-side documents into finding reports.

The documents are /home/user/Apparatus-Verbatus-Alpha/workbench/cleanroom-quarantine/63910cd1-PageKit-Reconciliation-Review.md (sha256 5f6e776d9cda0ee75a20d71778d79c7e7040630c8b64476fca19524b6ebb6645) and /home/user/Apparatus-Verbatus-Alpha/workbench/cleanroom-quarantine/68d24890-PageKit-Reconciled-Intake-Candidate.md (sha256 809c4ef938d5309a003879d6952658b8d1acb7ee5a1a57a7f2671fb73ac43f5c). The lead wrote them with outside chat sessions whose earlier research read other page-preparation programs. They contain no code, but they may carry that exposure, and they name file paths and outside links. None of that may pass to the build side.

Read both in full. Then write one finding report for each distinct requirement or page situation they raise that pagekit's prepare tool and its grey output should meet. Examples of topics, not a fixed list: a reviewed grey output as a main page rather than only a side view, and when grey loses evidence; manual editing before automatic detection; detector abstention with a zero or manual fallback; checking the decoded output against the accepted page; density and pixel-aspect metadata; keeping uncertain arrangements (inserts, neighbour fragments, recaptures) visible rather than forced into two pages; review outcomes with a next action for each; resource limits that never silently shrink a page; platform qualification on Intel and Apple Macs and Linux; separating development, calibration and untouched evaluation. Leave out anything about model licences, the main pipeline's admission rules, or the clean-room process itself; list those only by topic in the index.

Use the template at /home/user/verbatus-worktrees/pagekit-host/pagekit/cleanroom/templates/finding-report.md and follow its "Never put in a report" list exactly.

In each report:
- "Reader brief sha256:" carries the digest given in your task message.
- Pagekit's observed behaviour: read /home/user/verbatus-worktrees/pagekit-host/pagekit/README.md and the specs /home/user/verbatus-worktrees/pagekit-host/pagekit/cleanroom/specs/0002-prepare-core.md to 0006-grey-tone-view.md for what pagekit does now. Do not read pagekit's code. Say what pagekit already does, what it does differently, and what it does not do yet.
- General technique: a published method or textbook idea in your own words, with a Source: line (author, title, venue, year, or "general knowledge"). Where a requirement is a product or engineering rule rather than a method, say what it must achieve in plain words and write "Source: general knowledge".
- Settings in general terms: what each setting should depend on, never numbers taken from any program's code.

Also write one extra file, 0000-index.md, listing each report's file name and one-line situation, plus one plain-language paragraph naming the topics you left out and why.

What you may read: those two documents; the pagekit README, the specs named above and the template; published papers.

What you may not read: anything else in the repository, in particular pagekit's code and any file whose name contains another page-preparation project's name; no web pages other than published papers.

Where to write: only new files in /home/user/Apparatus-Verbatus-Alpha/workbench/cleanroom-quarantine/reports-0033/, named NNNN-short-topic.md starting at 0001. Write nothing anywhere else.

Your final reply must be only the list of files you wrote, each with its sha256, and the count. No report content, no summary, no quotations. The host checks every report with a script before opening it, and a refused report is deleted unread.

Stop condition: if the task cannot be done within these limits, write no files and reply with one sentence saying why.
