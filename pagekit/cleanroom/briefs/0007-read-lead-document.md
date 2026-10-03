# Brief 0007: read the lead's working document and distil it into finding reports

- Role: reading side.
- Saved by the host before the reading-side agent started, on 2026-10-02. The agent's
  task message gives it this file's path and this sha256 and nothing else.
- sha256 of the brief (the bytes after the marker line below, to the end of this file):
  5312f3531b6e1a561c1b5d4d572977aaf3a50b2aa5bcefb8917946d977930c1d
- Departure from the reader template, and why: the agent may write new report files
  into the local, git-ignored quarantine folder, because the session harness returns
  an agent's reply to the host directly. Writing the reports to files lets the host run
  the report check before opening any of them. The agent's reply is only file names
  and sha256 digests.

----- brief below this line, exactly as sent -----
You are a reading-side agent for pagekit's clean room. You stay on the reading side for your whole life: you never edit or commit anything in the repository, and you are never given build work afterwards.

Task: distil the lead's reading-side working document into finding reports.

The document is /home/user/Apparatus-Verbatus-Alpha/workbench/cleanroom-quarantine/eaa55b66-folioprep_cleanroom_spec.md (sha256 f4d912e4caed42d5a1963db47c74547cc461487fd78445e5ce348b61cd331290). The lead wrote it with a Claude chat session that read ScanTailor Advanced. It may contain their code, file names, line numbers, identifiers and constants. Those must never pass to the build side.

Read it in full. Then write one finding report for each distinct page situation or processing step it covers that a page-preparation tool needs: orientation, splitting spreads, deskew, content and margin selection, dewarping, colour and black-and-white output, despeckling, the failure cases it records, and anything else it covers. Use the template at /home/user/rv/pagekit/pagekit/cleanroom/templates/finding-report.md and follow its "Never put in a report" list exactly.

In each report:
- "Reader brief sha256:" carries the digest given in your task message.
- Pagekit's observed behaviour: pagekit so far only checks crops for lost ink, cut writing, a split off the gutter, and resolution. It does not detect or change geometry. Read /home/user/rv/pagekit/pagekit/README.md and /home/user/rv/pagekit/pagekit/cleanroom/specs/0001-crop-check.md for what it does. Do not read pagekit's code. Where pagekit does nothing yet, say "not built yet".
- General technique: the published method or textbook idea, in your own words, with a Source: line (author, title, venue, year, or "general knowledge"). If the document describes a method only as their code does it, and you cannot name a published or general method, say what the step must achieve in plain words and write "Source: general knowledge"; never describe their code's steps.
- Settings in general terms: what each setting should depend on, never the numbers they use.

Also write one extra file, 0000-index.md, listing each report's file name and one-line situation, plus one plain-language paragraph on anything in the document you judged could not be expressed without carrying their expression, so you left it out.

What you may read: that document; ScanTailor's public user manual or wiki on the web if you need to understand a user-facing control; published papers; the two pagekit files named above; the template.

What you may not read: anything else in the repository, in particular pagekit's code and any file whose name contains the other project's name.

Where to write: only new files in /home/user/Apparatus-Verbatus-Alpha/workbench/cleanroom-quarantine/reports/, named NNNN-short-topic.md starting at 0001. Write nothing anywhere else.

Your final reply must be only the list of files you wrote, each with its sha256, and the count. No report content, no summary, no quotations. The host checks every report with a script before opening it, and a refused report is deleted unread.

Stop condition: if the task cannot be done within these limits, write no files and reply with one sentence saying why.
