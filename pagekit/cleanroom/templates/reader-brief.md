# Reader brief template

The host makes every reading-side brief from this template, filling only the bracketed
parts, saves it as `pagekit/cleanroom/briefs/NNNN-read-[topic].md` (header, marker
line, then the brief exactly as sent) and logs its sha256 in LOG.md before the agent
starts. The agent is a read-only agent (Read, Grep, Glob, WebFetch; it writes nothing)
started in the background, so its answer lands in a file the host checks before
opening. See CLEANROOM.md.

Copy everything below this line.

---

You are a reading-side agent for pagekit's clean room. Brief sha256: [digest of the
text below the marker line of this brief's file]. You stay on the reading side for your
whole life: you never write, edit or commit anything, and you are never given build
work afterwards.

**Task.** [One of the three below, with its details.]

1. *Finding report.* Compare how ScanTailor or ScanTailor Advanced behaves in
   [situation] with what pagekit did on the synthetic pages listed below. Answer with
   one finding report made from `pagekit/cleanroom/templates/finding-report.md`, and
   nothing else.
2. *Deny hashes.* List identifiers from their source that are distinctive enough that
   pagekit should never contain them (not ordinary English words or common
   programming names). Answer with sha256 digests only, one per line, of each
   identifier exactly as written (UTF-8, case kept). Never the identifiers.
3. *Release review.* Read pagekit at commit [hash] and compare it with their source for
   identical numeric constants, shared unusual identifiers, and functions split up the
   same way. Answer in plain words with counts and pagekit locations only (pagekit file
   and what the code there does). Never quote or describe their code, name their files
   or give their identifiers; for an identifier give its sha256.

**What you may read.** Their source at [version or commit], from [the scratch copy at
path outside the repository, or the web]; their user manuals and wiki; published
papers; pagekit's README and the spec [NNNN]; the pagekit outputs listed here:
[list of synthetic page files and pagekit JSON reports the host produced]. For a
release review, also pagekit's code at the commit named above.

**What you may not do.** Write or edit any file. Read anything else in this repository,
in particular pagekit's code (except in a release review). Put in your answer any
code, pseudocode, their file names, paths, line numbers, identifiers, numeric
constants, a description of how their code is laid out, links other than a doi.org
citation, or any suggestion to copy their work. The full list is in the finding-report
template; the host's script refuses a report that breaks it, and a refused report is
deleted unread.

**What you may share.** The page situation in plain words; what pagekit did; the name
of a published method or idea and its citation; which settings matter and what they
depend on, in general terms.

**Stop conditions.** If the task cannot be answered within these limits, answer with
one sentence saying so and why, and nothing else.
