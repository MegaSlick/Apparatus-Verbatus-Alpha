# pagekit's clean room

pagekit is licensed Apache-2.0. ScanTailor and ScanTailor Advanced, the programs whose
workflow inspired it, are licensed GPL-3.0, and GPL code may not be copied into
Apache-2.0 code. pagekit must therefore be an independent work. Ideas, methods and
published algorithms are free to use; another program's code, its structure and its
wording are not. This document is how pagekit is kept apart from that code, and how
the record shows it was. It binds every session and every agent that works on pagekit,
from the repository alone.

## Who does what

- **The lead** approves this protocol and any change to it, decides every incident,
  and may read the record at any time. The lead does not read code, so everything in
  the record is written to be judged without it.
- **The host** is the session that runs the work. It is the gatekeeper between the two
  sides below. It never reads ScanTailor or ScanTailor Advanced source and never
  writes pagekit code. It writes specs, briefs, the record and incident notes, runs
  the checks, and runs pagekit on synthetic pages for the reading side.
- **The build side** writes pagekit's code, tests and README. Each build-side agent
  works in its own git worktree under a brief made from
  [templates/builder-brief.md](templates/builder-brief.md). There is no special agent
  definition for it: this repository's agent roles must be read-only (the roster test
  in `.claude/agents/` refuses a role that can write or run commands), so builders are
  ordinary worktree agents bound by their brief.
- **The reading side** may read ScanTailor and ScanTailor Advanced source, only to
  compare their behaviour with pagekit's. It is a read-only agent (it can read,
  search and fetch web pages, and writes nothing) under a brief made from
  [templates/reader-brief.md](templates/reader-brief.md). It does not read pagekit's
  code (except in the release review below): the host runs pagekit on synthetic pages
  and hands it the outputs.

An agent stays on one side for its whole life. A build-side agent that sees ScanTailor
source, even by accident, stops at once; that is an incident (below).

## How a slice is built

1. **Spec first.** The host writes a short behavioural spec,
   `specs/NNNN-name.md`: what the slice does, its inputs, outputs and limits, in plain
   words. It draws only on manuals, published papers and finding reports that passed
   the check. It is committed before any code. The spec, not the reports, is the
   central document: it is what the build side works from.
2. **Brief.** The host makes the builder brief from the template, the spec and any
   passed finding reports, and nothing else. It saves the brief in `briefs/` and logs
   its sha256 before the agent starts. If a finding report is used, its check is run
   and logged before the brief is written.
3. **Build.** The build-side agent writes code and tests in its worktree and commits.
   Every commit carries a `Clean-room: build` trailer, plus `Clean-room-finding: NNNN`
   for each finding report that informed it. The model that wrote it is named in the
   `Co-Authored-By` trailer.
4. **Check and record.** The host runs the leak scan and the tests, and appends an
   entry to [LOG.md](LOG.md).

### What the build side may use

- the slice's spec and the passed finding reports named in its brief;
- published papers and textbooks, cited by author, title and year (for example Otsu
  1979 for the ink threshold);
- ScanTailor's public user manuals and wiki, which describe what a user sees and
  controls, not code (through the spec, which the host writes from them);
- permissively licensed libraries used as dependencies, never copied in (today only
  Pillow);
- pagekit's own code, tests and README, and general image-processing knowledge.

Every brief forbids reproducing anything the agent recognises as ScanTailor's or
ScanTailor Advanced's.

## The reading side and finding reports

A reading-side session has one of three tasks: a finding report, a list of deny
hashes, or the release review.

- **Where it reads.** ScanTailor source is never put in this repository or any of its
  worktrees. The reading side reads it on the web or from a scratch copy outside the
  repository, deleted after the session. The repository's agent settings refuse shell
  commands that name the projects (see "Automatic checks"), and an allow rule cannot
  override a refusal, so when a scratch copy is needed the lead approves it for that
  session and fetches it from their own shell. The brief records which version or
  commit was read.
- **What it may share.** The page situation in plain words; what pagekit did on the
  synthetic pages it was given; the name of a published method or idea and its
  citation; which settings matter and what they depend on, in general terms. The
  reader brief template lists exactly what may and may not be shared.
- **What it may never share.** Code or pseudocode, their file names, paths, line
  numbers or identifiers, how their code is laid out, numbers taken from their code,
  links other than a doi.org citation, or any suggestion to copy.
- **The report** follows [templates/finding-report.md](templates/finding-report.md):
  situation, pagekit's observed behaviour, general technique with its source, and
  settings in general terms.

### The gatekeeper

The reading-side agent runs in the background, so its answer is written to a file
rather than shown to the host. The host saves that answer as a report file without
opening it and runs

    python3 -m pagekit.cleanroom.check_report REPORT.md

before reading it. The check prints only rule names, report line numbers and the
report's sha256, never the text, so a leaking report never reaches the host.

- **Pass:** the host reads the report, saves it as `findings/NNNN.md`, and logs the
  check with the report's sha256 and the reader brief's sha256.
- **Refused:** the report is deleted unread, never repaired and passed on. Only its
  sha256 and the rules it broke go in the log. A new reading-side session may try
  again.

If the harness can only return an agent's answer into the host's view, this step
cannot keep a leaking report out of the host's context. The host then says so in the
log entry, and the report still has to pass the check before anything reaches the
build side.

### Deny hashes

[deny-hashes.txt](deny-hashes.txt) holds sha256 digests of identifiers that are
distinctive to ScanTailor or ScanTailor Advanced source, never the identifiers
themselves. A reading-side agent produces them, answering with digests only. Both the
report check and the leak scan refuse any word whose digest is on the list. The file
starts empty.

## Automatic checks, and what they cannot do

Every check here catches accidents, not every spelling. None of them proves that
nothing was copied; together with the record they show the process was followed.

- **Report check** (`check_report.py`). Refuses a finding report holding code fences
  or backticks; code tokens (double colons, arrows, include lines, `def` or `class`
  followed by a name and a bracket or colon); braces; lines ending in a semicolon;
  file names with source extensions; paths; links other than doi.org; line-number
  references; or a word on the deny-hash list. It also requires the template's four
  sections, in order and not empty, a source line under the technique, and the reader
  brief's sha256. Plain English "class" is allowed; "line" followed by a digit is not,
  so lines of text on a page are numbered in words.
- **Leak scan** (`scan.py`). The host runs `python3 -m pagekit.cleanroom.scan` over
  pagekit's tracked files. It looks for the wording of a GPL licence header, a
  copyright notice naming either project or their authors, links into their source
  repositories, and words on the deny-hash list. It reports counts and pagekit
  locations only. A test runs it on every pull request and expects no hits.
  *Exception:* NOTICE links to the two projects' front pages as a credit. The link rule
  matches only links that go into a repository (a file, a folder, a clone or raw
  address), so the credit links are allowed.
- **Commit gate.** The pre-commit hook runs `python3 -m pagekit.cleanroom.gate`: the
  HOLD rule below, and the leak scan over the staged pagekit files. A hook can be
  skipped, so CI repeats both on every pull request; CI is the real gate.
- **Agent settings** (`.claude/settings.json`). Refuse shell commands whose text
  mentions the projects' names or the ScanTailor Advanced maintainer's account name,
  in the spellings listed there. A command can be spelled to slip past them. Web
  fetches can only be refused for a whole site, and refusing all of GitHub would stop
  ordinary work, so web fetches of their repositories are not refused by settings:
  they are forbidden by every builder brief, and builders work without network.
- **The record tests.** CI fails if LOG.md's entries on main were changed rather than
  added to, if a saved brief does not match its logged sha256, or if HOLD exists.

### The release review

Before each pagekit release, a reading-side agent reads pagekit at the release commit
and ScanTailor's source, and reports in plain words, with counts and pagekit locations
only: numeric constants that are identical, unusual identifiers that are shared (given
as digests), and functions split up the same way. It never quotes or describes their
code. No similarity score is used; a number would stand in for the judgement. The
report goes through the gatekeeper like any other, and its result is logged. Any hit
that is more than an idiom every image program uses is an incident.

## If a leak is suspected

1. **Pause.** The host commits `pagekit/cleanroom/HOLD`, a short plain-language note
   of what was detected, together with an incident note made from
   [templates/incident.md](templates/incident.md) as `incidents/NNNN.md`. While HOLD
   exists, the commit gate refuses any pagekit change other than HOLD and the incident
   notes, and CI fails, so nothing merges.
2. **Tell the lead.** The host sends the lead a notification
   (`operations/notify/notify.sh`) saying what was detected and where. Agents never
   send notifications.
3. **The lead decides** one of:
   - *Purge:* the affected work is reverted or deleted and rebuilt from the spec by a
     fresh build-side agent that has never seen it;
   - *Minor breach:* the incident note records exactly what was carried over and what
     was done about it;
   - *False flag:* for example an idiom every program uses; the note records why.
4. **Record and resume.** The lead's decision is written on the note's `Decision:`
   line, and HOLD is removed in the same commit. The commit gate refuses to remove
   HOLD without an incident note carrying a decision, and CI fails if any incident
   note lacks one once HOLD is gone.

## The record

- [LOG.md](LOG.md) is append-only. Each entry is numbered and says, in plain words:
  who worked (role and model), the brief (file and sha256), the spec it worked from,
  the commits or report numbers, and what happened. An entry's date and time are those
  of the commit that added it. Entries do not list "sources used": an agent cannot
  truthfully enumerate everything it drew on, so the record keeps what can be checked,
  the brief and the spec.
- `briefs/` keeps every brief exactly as sent: a short header, a marker line, then the
  brief. The sha256 of the text after the marker is in the header and in the log, and
  a test checks they match.
- `specs/`, `findings/` and `incidents/` hold the specs, passed finding reports and
  incident notes. Refused reports are not kept.

## What the record can and cannot show

Current AI models were most likely trained on public code that includes ScanTailor.
The record shows that no agent deliberately read ScanTailor source while building
pagekit and that the code was written from written specs and published methods. It
cannot show that a model has never seen ScanTailor in training. Every brief forbids
reproducing anything the agent recognises as ScanTailor's, and the model behind each
commit is named in its `Co-Authored-By` trailer. Whether a model reproducing
something it memorised would infringe is unsettled law; that is a question for a
lawyer, not for this process.

## Known pre-existing item

Before pagekit existed, this repository gained a bridge to ScanTailor Advanced's
project files: `operations/operator/scantailor_worker.py`,
`operations/triage/scantailor_bridge.py`, `operations/triage/scantailor_project.py`,
and the ScanTailor sections of `pipeline/0_triage/CONTRACT.md`. Their own comments say
they were written from reading ScanTailor Advanced's project-file writer. They are not
part of pagekit, they are outside pagekit's allowed sources, and no build-side agent
may open them. Other open pull requests remove them; they must be gone before any
reading-side session starts. Log entry 0003 records this.

## Credit

pagekit's README and NOTICE thank the ScanTailor and ScanTailor Advanced developers,
link to their projects, say the workflow was inspired by them, and say that no code
from them is used.
