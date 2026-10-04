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

## 0008 — Reading side's reports checked, read and accepted

- **Who:** the reading-side agent of entry 0007 wrote the reports; the host checked
  and read them.
- **The agent's task message** gave it only the brief's path and sha256 (task message
  sha256 8460a83ddb84f66588e164b1ae13b69e3df0d732d0bd0263106e513e78fb9978).
- **What came back:** 36 files written into the local quarantine folder. The agent's
  reply listed only their names and sha256 digests, and the digests matched the files.
- **Check before reading:** the host ran the report check on every file before opening
  any of them. All 35 finding reports passed every rule. The index file
  (`0000-index.md`) is not a finding report: it failed only the template-structure
  rules and broke no content rule.
- **Host's reading:** the host then read all 36 files in full. They name page
  situations, published or textbook methods with citations, and what each setting
  should depend on. The host found no code, no file or function names, no numbers
  taken from the other program, and no description of how that program is built. The
  index records, in plain words, what the reader left out because it could not be
  passed on without carrying the other program's expression: the order of its filters,
  its numbers, its data structures, its specific recipes, its dewarping internals, and
  two behaviours the lead's document calls defects in that code.
- **Accepted:** the 36 files are saved unchanged in `findings/`:
  - `0000-index.md`: 4caaef201dc80a0a5ccbfa96bd74475f46a835ed41192fb0d59d6388b61b4813
  - `0001-orientation-quarter-turn.md`: 2d9cfa3c9dd85583c92e6a137ba79a7ce753b5095044f7278db388fe8985313f
  - `0002-orientation-upside-down.md`: 0c692c5977df252f4135e04f77d1b3fe895c08feeb8eeee9e723e58ab25f0d67
  - `0003-spread-or-single.md`: 51d6a74ccd1051d72dce5fe64bea17f0d64ecf641b5ced9ce9dae039cbd62bfc
  - `0004-gutter-line.md`: f2920efeb6009f6d90cb59805a1c0170282de1635271b87a83cd425ae55b06cb
  - `0005-gap-split.md`: 58cbf15e4712fa4e27f7fe3f1f0328f6427057627b0688740566d99be68b4b27
  - `0006-slanted-split.md`: 49d5b691c17d677762f1b2097955add4b88694605dceda846019780a49ebb156
  - `0007-neighbour-offcut.md`: ec96425da38fbc4cce793fd55f1616c5ee032c5b5a8cb18f283892b61fcd42a0
  - `0008-gutter-marginalia.md`: e5bff586503d075f962a3ade396f58076b5b4afaf2b462148ac7268eebffabbb
  - `0009-deskew.md`: 27950ad52c550b29ce03b16dd8f4f9e62228db4e331e1eccd87f1d16219f435a
  - `0010-deskew-distractors.md`: 4dbe6910f19500052a6cc7e2fab25ef86aeb7cc280366163f543618fa43b2963
  - `0011-skew-outliers.md`: 933de15934ee6ce2572f5a24194b611cea9ae5a61fc61b5a9464a5eba122106a
  - `0012-skew-limits.md`: feb827ea7b761b6eef6a8029d6879398ed36cfc11e40e88f7b4ef8bb9540b3f7
  - `0013-polarity.md`: 56fb677f0dd208693082f13b4223d4151115fc977f7363ba9b83c5eb718eb5d9
  - `0014-resolution-metadata.md`: 75b0ab17ed06be47d8ebed800dab091caf042a01f056ca4985668cc7d8a56aa3
  - `0015-page-box.md`: 437c2471798b9f81fa85efc2e6b1fe901829ca5c516057bf1368006410a77314
  - `0016-content-box.md`: 87659ba056af99fff6dbf5d38321ce44dbd80fbe991e6a494a15ad2ca746ad82
  - `0017-dark-band-with-light-text.md`: 7c09330917e89a50f8c52b7aea274af60a7cb539551247aafc7fd5852f244875
  - `0018-ruled-lines.md`: fc676816b0d5a3ec827ebd15cfea29ed122908ac26197cb23546ea0187a61911
  - `0019-scanning-targets.md`: 9569cd08e898bffa31a5a6e3bc9ea890049de59820b52eab6e9e216c08ae5256
  - `0020-margins-padding.md`: c72b417bd7a252a8dc8514f2ce9c0fd6cb4ebf405e3d788862e8733fdc25f6b2
  - `0021-blank-page.md`: b2fd7bad253710615df6975e1cd21b67e40fffd9640e36c88a1f17714f653351
  - `0022-dewarp.md`: 92d75c6cdeb48bbc724c73ab08a146bdc28c4f6c1d845cd9ba16e6274890809d
  - `0023-uneven-illumination.md`: f28bf5dd3f20f9f54965b81e96dae00cb226adc313b6d1a51a33744bb411db31
  - `0024-paper-colour-cast.md`: de5cf7f2eeb56f921ec70073ce49ea306449cb66cb948d78946f8b55e2340d31
  - `0025-binarisation.md`: a0d176771320e1bff1f5dd4d18da811e5df0cab8c8670746b95ce9e10bdc8e75
  - `0026-stroke-edge-smoothing.md`: f9831fbc4ce1483255cf04f73eda8fcfbe6df57ae4f0104e1df652bc5aa304e6
  - `0027-despeckle.md`: c1d8ae114660dad3cf427cd76b1d8af1a7ad9846315beb3df7aa3ed14bb00607
  - `0028-picture-zones.md`: 7ff21317835ed18e36b699f77a5f79378e3dbba5df68a1dcad53a8a6b50d2c44
  - `0029-colour-reduction.md`: 2c716003f86b8422030a14aec55002347f7aaf440fb92280d624eaa4004a87d3
  - `0030-bleed-through.md`: 0a98a74522be3c30521d364289b7b6306a3adc12c98541277ce8f2096486efb3
  - `0031-resample-once.md`: cd72329cf10bd342d7c8343e294935596c8ec681907a135aa7925fe6a15001c9
  - `0032-faint-ink-tone.md`: 131be000c2e2c67c9be362a2fa6650560bdb1f02a5741adfd639ea23ad533eae
  - `0033-manual-overrides.md`: 9a8d67456d839818f413f67885ab269be1d5b9eb8a85ce4cac18c46d5a7d2b86
  - `0034-coordinate-provenance.md`: 6733e53d78d2e8e41dc8250f98f62c81c3435eb45cc92c1111f8565f88119838
  - `0035-measuring-success.md`: 86bf9eb8e3cde6e4aedd41154cf6c0780c22d513934cc10de17409d68a5bc9eb
- **Use:** build-side briefs for later slices may cite these findings by number,
  together with a written spec, and commits that use one carry
  `Clean-room-finding: NNNN`.

## 0009 — A rule of this protocol was broken: reading-side work ran while the old bridge was still on main

- **Who:** recorded by the host after an independent reader of this branch pointed it
  out.
- **The rule:** CLEANROOM.md said the repository's older ScanTailor bridge (entry 0003)
  must be gone before any reading-side session starts.
- **What happened:** the reading-side session of entries 0007 and 0008 ran while those
  files were still on `main`. They are removed by two other open pull requests (#245,
  #248) that had not merged. The host started the session without checking this rule.
- **Did anything cross?** As far as the record shows, no. The reading-side agent was
  told to read only the lead's document, two pagekit files and the report template,
  and its reports passed the check and the host's reading (entry 0008). No build-side
  agent has read the bridge files.
- **What the host itself has seen of the bridge**, checked against this session's own
  transcript on 2026-10-02:
  - The host never opened the bridge files.
  - It ran searches while preparing the clean-up pull requests that delete them. Those
    printed their file names, the import lines in other files that refer to them, and
    one line of the triage stage description naming the bridge as a kind of actor.
- **Status:** a procedural breach, not a suspected leak, so no HOLD was placed. The lead
  is asked to decide whether entries 0007 and 0008 stand, recommended yes. The rule is
  rewritten so it can be kept:
  - no build-side agent may read the bridge files;
  - the bridge must be removed from `main` before pagekit merges.
- **Lead's decision:** recorded in entry 0012.

## 0010 — The independent reader's findings fixed, and two briefs saved

- **Who:** the build-side agent of entry 0002 made the fixes; the host saved the briefs.
- **Briefs saved now:**
  - `briefs/0005-anchor-copyright-rule.md` (sha256 e3bebde3984fe6785c58f3edf2dc5414369b29163227227e858ed3cdfc8f1c37), issued 2026-10-02T17:10:58Z. This is the
    brief behind entry 0005. It had not been saved when 0005 was written.
  - `briefs/0010-fix-independent-review.md` (sha256 df8f81b1c284c6ce1ad4f436aba2f1bbe326783fde3135c760d95536a7acf703), issued 2026-10-02T17:33:58Z.
  Both were copied word for word from the session transcript after the agent finished.
- **What changed:**
  - Slice 1:
    - a page with no detectable ink now goes to review;
    - thin one-pixel pen lines are no longer cleaned away before counting, because only
      lone specks are removed;
    - any failure to read the master exits as "cannot check".
  - Records:
    - incident notes can never be deleted;
    - CI replays the HOLD rule over every commit in a pull request;
    - a test checks every accepted finding's sha256 against this log.
  - Report check: it now refuses programming-style names and call or assignment shapes,
    and the maintainer's account name. The 35 accepted findings still pass.
  - Agent settings: they refuse only download-shaped commands naming the other project.
  - CLEANROOM.md says what still gets through the checks, that the host's own reading
    is the real check, that the gate cannot prove who wrote a decision, the full list of
    old bridge files, and the rewritten rule from entry 0009.
- **Commits:** 5352b0f0, 1cd3a90a, 379f2373, 00228b9e, 2161b90c.

## 0011 — Outside reading-side sessions written into the protocol

- **Who:** the build-side agent of entry 0002 wrote the section; the host saved the
  briefs.
- **Why:** the lead asked that a reading-side session run in a separate chat, as in
  entries 0006 to 0008, become a standard, written path.
- **Briefs:**
  - `briefs/0011a-outside-sessions-section.md` (sha256 5f580f51a90ff47717ea4e4151d89d587baa1c27d383d96f36501a93684d5b80), issued 2026-10-02T18:21:33Z;
  - `briefs/0011b-quarantine-path.md` (sha256 91508d750c8078114d9996faab068689132e87a234eec13eca556db71fa41543), issued 2026-10-02T18:22:28Z.
  Both were copied word for word from the session transcript after the agent finished.
- **What changed:** CLEANROOM.md gained the section "Outside reading-side sessions"
  (3b4736e7). Its quarantine folder name was then corrected to the one actually used,
  `workbench/cleanroom-quarantine/` (4b961c4a).

## 0012 — The lead's decision on entry 0009

- **When:** 2026-10-02, about 20:35 UTC, in the session that recorded entry 0009.
- **Decision:** entries 0007 and 0008 stand. The 35 accepted findings remain valid.
- **Reason the lead gave:** nothing was seen that could leak. The host never opened the
  bridge files, and seeing their names and import lines is not exposure to their code.
  The lead treats the slip as minor.
- **Also decided:** the narrowed agent settings in this pull request (refusing only
  commands that download or copy the other project's source) are approved. If the
  reading side later needs wider access to read the other project or look up how it
  behaves, it may ask for it. Published reference libraries and papers remain the
  preferred source.
- **What stays in force:** the rewritten rule of entry 0009. No build-side agent reads
  the bridge files, and the bridge is removed from `main` before pagekit merges.

## 0013 — Build side: fix the review findings on the clean-room checks

- **Who:** a fresh build-side agent (Claude Opus 5.5), started by the host.
- **Why:** CodeRabbit's review of pull request #249 found four places where a clean-room
  check fails open or exempts too much: the commit hook skips the gate when the gate
  file is missing; lines under a `Source:` line skip the code-shape rules; a HOLD can be
  lifted by a decision in an unrelated incident note; and a failed read of a note on the
  base branch passes.
- **Brief:** `briefs/0013-build-review-fixes.md` (sha256 bc68e855d0a78cb1147d4b57e49e592ad819109460ff198f3183f168983abae0), issued
  2026-10-02T20:45:39Z, saved before the agent started.
- **Sources:** none beyond pagekit's own code and tests; no finding report informs it.

## 0014 — Correction to finding 0021

- **Who:** the host, from CodeRabbit's review of pull request #249.
- **What:** finding 0021 (a blank page) says pagekit raises no crop flags on a blank
  page. That described pagekit before entry 0010. Since entry 0010, a page with no ink
  detected goes to review for that reason alone, as the slice 1 spec says.
- **Why here and not in the finding:** finding files are kept exactly as accepted, with
  their digests in entry 0008. This entry is the correction. A builder citing finding
  0021 must also cite this entry.


## 0015 — Brief 0013 done

- **Commits:** 54102dbe, 7aae664b, edfae7b4, f29545ef, d51341dd.
- **What changed:**
  - The commit hook refuses a commit when the clean room exists but its gate file does
    not.
  - Only the `Source:` line itself is exempt from the code-shape rules.
  - Lifting a HOLD needs a decision in every incident note.
  - A failed read of a note on the base branch stops the check instead of passing.
- **The agent's own account:** it opened only pagekit's clean-room code and tests, the
  report-check part of CLEANROOM.md and the hook files. It saw no ScanTailor or other
  GPL source. All 35 accepted findings still pass the report check unchanged.

## 0016 — Specs 0002 to 0004 written; three build-side briefs issued

- **Who:** the host (Claude Opus 5.5), in the session the lead asked to bring pagekit
  towards a usable page-preparation tool with the two-sided process.
- **Specs:** `specs/0002-prepare-core.md` (the project file, the geometry chain applied
  once, point mapping, the output writer and the `prepare` command),
  `specs/0003-orientation-and-split.md` and `specs/0004-skew-and-boxes.md`. The host
  wrote them from the finding reports accepted in entry 0008 (with the correction in
  entry 0014), spec 0001 and the papers those reports cite. They were committed before
  any brief was issued.
- **Briefs**, each saved before its agent started, for three fresh build-side agents
  working in parallel in separate worktrees:
  - `briefs/0016-build-prepare-core.md` (sha256 ee4b17a61f3991a7a1df8dd46e0a37402ab2992c400090fd4792fbd824bb36d0);
  - `briefs/0017-build-orientation-split.md` (sha256 00ece8d673cd968a239b37012b530b54175235902612d127be3092269ef447c3);
  - `briefs/0018-build-skew-boxes.md` (sha256 6e5f0d64c0da25b49919a6cb9bd6550313876473f5f2dc7972770cdb8a2b92b0).
- **Sources named in the briefs:** the spec and the finding reports listed in each
  brief, published papers, pagekit's own code and Pillow. No reading-side material
  beyond the accepted findings.

## 0017 — Slices of briefs 0016 and 0017 built; review of the core; follow-up brief 0019

- **Brief 0016 (core) done:** commits 9db1582, 892d7db and 4973611 on `work/pk-core`. The
  host's leak scan found no hits in 81 files. The agent reported it saw no ScanTailor
  or other GPL source.
- **Brief 0017 (orientation and split) done:** commits 7eb909f, 1ea95f1 and c88360f on
  `work/pk-split`. The leak scan found no hits in 80 files. The agent reported it saw no
  ScanTailor or other GPL source.
- **Independent review of the core:** a fresh agent bound by the clean-room rule (it
  read only pagekit and its spec, and saw no GPL source) found four blocking problems: a
  black line at the right and bottom edge of default pages, partial output on exit 2, a
  re-applied override re-stamped as fresh, and hand-set values of a dropped page lost.
- **Follow-up brief:** `briefs/0019-build-core-review-fixes.md` (sha256
  596b9e0ecab7d2521d58d281cad149636bf1b737e5a112620d585eda7ce34344), sent to the agent of
  brief 0016. The same message also answered the agent's question on the margin, in
  these words: "the margin is a setting, not a detection. Record it with origin
  "detected" from the setting, confidence 1, evidence naming the setting, and no flag.
  Change the README if needed, and note it as a decision against the spec's wording in
  your commit message."

## 0018 — Spec 0006 written; brief 0018 done; brief 0020 issued

- **Spec 0006** (`specs/0006-grey-tone-view.md`): a gentle grey view for the readers that
  see a page in grey, written by the host from findings 0023, 0024, 0025, 0030 and 0032,
  the papers they cite, and a published study of preprocessing for vision-language
  readers of historical handwriting (Farazi et al., 2026, arXiv:2608.22366). The same
  commit added output defaults to spec 0005: lossless TIFF, the source colour mode
  kept, no shrinking by default. The host drew the readers' needs from the main
  project's own code and public model documentation, through a research agent that
  read no pagekit code and no GPL page-processing source.
- **Brief 0018 (skew and boxes) done:** commits 58663f1, cb5241e, 88e29c3 and 11c1cb1
  on `work/pk-skew`. The leak scan found no hits in 84 files. The agent reported it saw
  no ScanTailor or other GPL source.
- **Brief 0020:** `briefs/0020-build-tone-view.md` (sha256
  f41843953ca62b3a264f68540239ebbd8d13554aa8606682626e34cb885f3760), for a fresh
  build-side agent running Claude Fable 5.1, saved before it started.
