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

## 0019 — Review of orientation and the split; follow-up brief 0021

- **Independent review** by a fresh agent bound by the clean-room rule (it read only
  pagekit and its spec, and saw no GPL source): orientation gave no confident wrong
  turn on any hard case; the split detector gave confident, unflagged wrong cuts on a
  single page with a vertical rule, on dense texture, and on a spread with one blank
  page.
- **Follow-up brief:** `briefs/0021-build-split-review-fixes.md` (sha256
  70d72ffec61605e6dfecee2a62386e8eff96b851ad4032d5f81659ef3c4e0160), sent to the agent of
  brief 0017.

## 0020 — Core fixes done and merged; review of skew and the boxes; follow-up brief 0022

- **Brief 0019 done:** commits efce9d8 and fa4038c on `work/pk-core`. The leak scan
  found no hits. The host merged `work/pk-core` into `work/pagekit-prepare` (cbe73f5).
- **Independent review of skew and the boxes** by a fresh agent bound by the clean-room
  rule (it saw no GPL source): skew is accurate, but in four ways legible writing could
  fall outside the page box or the content box without a flag (faint ink beside dark
  ink, uneven lighting and gutter shadow on the page box, writing inside a border-joined
  dark area, a small mark at the paper edge at low resolution).
- **Follow-up brief:** `briefs/0022-build-skew-review-fixes.md` (sha256
  7338768933221b2ff8cb114702a00f249b367647d53d73b085efffa93a807bcd), sent to the agent of
  brief 0018.

## 0021 — Slices merged; tone view built; brief 0023 issued

- **Brief 0021 done:** commits 9bbb537, c0c1d18 and 3157503 on `work/pk-split`. Leak scan
  no hits. Merged into `work/pagekit-prepare` (8cf2296).
- **Brief 0022 done:** commit 64c8fe0 on `work/pk-skew`. Leak scan no hits. Merged into
  `work/pagekit-prepare`; the only conflict was in NOTICE, where the host kept both
  slices' citation sections word for word. The combined pagekit suite passes and the leak
  scan finds no hits in 108 files. A re-review of 64c8fe0 is running.
- **Brief 0020 (tone view) done:** commits 0a2ab1f and ae63d66 on `work/pk-tone`. Leak
  scan no hits. An independent review is running; not merged yet.
- **Brief 0023:** `briefs/0023-build-prepare-pipeline.md` (sha256
  931a7ee1bbc1b1cf089629941ee1079102d17877c59ea862d45aa434e716c769), for a fresh build-side
  agent, saved before it started.

## 0022 — Reviews of the tone view and of the skew fixes; follow-up briefs 0024 and 0025

- **Independent review of the tone view** (`work/pk-tone`) by a fresh agent bound by the
  clean-room rule (it saw no GPL source): gentle, fast and pixel-deterministic, but paper
  clipped to white near sharp stain edges and on dense pages, the written TIFF was not
  byte-identical on repeat, and the output could overwrite the input.
- **Re-review of 64c8fe0** (`work/pk-skew`), same rule: the four losses are fixed, but
  paper mottling, textured backdrops and targets were read as ink, sending ordinary
  pages to review, and a ruler on a pale backdrop gave a confident wrong page box.
- **Follow-up briefs:** `briefs/0024-build-tone-review-fixes.md` (sha256
  37b02eaad4245533bdbd3faf484c36b444a1fc1d8d58108467110ec27b9c4431), sent to the agent of
  brief 0020; `briefs/0025-build-skew-regression-fixes.md` (sha256
  f67a91369193bf7eb22f141614abe8975448b44c437ceb79ea18b510cb3d9d23), sent to the agent of
  brief 0018. Both saved before they were sent.

## 0023 — Prepare pipeline built and merged; review running

- **Brief 0023 done:** commits 21d0db9, 2d4f714, 7e5f52d, 61d9309 and 5166f20 on
  `work/pk-integrate`. The agent confirmed the brief's sha256 before starting and
  reported it saw no ScanTailor or other GPL source. Leak scan no hits in 114 files.
  Merged into `work/pagekit-prepare` (8c0a2cb); the combined pagekit suite passes.
- **First run on the lead's two sample spreads**, locally (the images stay outside the
  repository): both splits correct, pages upright, all writing kept; every page flagged.
  An independent review of the pipeline, bound by the clean-room rule, is running.

## 0024 — Fixes merged; first real-page findings; follow-up briefs 0026 to 0028

- **Brief 0025 done:** commit 352a671 on `work/pk-skew`. Leak scan no hits. Merged into
  `work/pagekit-prepare` (34d5d86); the combined suite passes. On the lead's two spreads,
  faint-mark flags fell from 1317 to 15 on the first page.
- **Brief 0024 done:** commits 8a01a53 and 2131539 on `work/pk-tone`. Leak scan no hits.
  The host's merge conflicted in code (`pagekit/__main__.py`), so the host aborted it and
  gave the merge to a build-side agent.
- **Real-page findings** from the host's local run on the lead's spreads (the images stay
  outside the repository): both splits right, every page upright, all writing kept; a page
  tilted about 2.5 degrees left unlevelled because its two estimates differed by 0.6; the
  book's board edge and page-edge stack kept in the page box; every page flagged for
  orientation and for discarded ink.
- **Follow-up briefs:** `briefs/0026-build-integrate-tone-merge.md` (sha256
  1e137aaab7c94de06c05355ebe92a315105608555da86b65c2891b732cceddb5), to the agent of brief
  0023; `briefs/0027-build-skew-real-pages.md` (sha256
  3f759d37ceaf85be653903ae05fcf85f0d510ead219fddbdc2f4da2a6c890922), to the agent of brief
  0018; `briefs/0028-build-orientation-real-pages.md` (sha256
  6562ca00a4b274ea09f216f5b8e66b06737daf2eb563ba156639991cc941b3ab), to the agent of brief
  0017. All saved before they were sent.

## 0025 — Review of the prepare pipeline; follow-up brief 0029

- **Independent review of 8c0a2cb** by a fresh agent bound by the clean-room rule (it saw
  no GPL source): geometry, outward box scaling, the override round trip and per-page
  failure flags held up; prepared TIFFs differed between processes at a pad byte; the
  reduced-copy code was never tested above 150 dpi; settings and code changes did not
  refresh stored content boxes; `measure` misjudged boxes on tilted pages; the review
  sheet's correction command failed as printed.
- **Follow-up brief:** `briefs/0029-build-prepare-review-fixes.md` (sha256
  b9d754d4a1223124750728759bbc9e53eefdf32889127eba5cd510080976a2f7), to the agent of brief 0023, after brief 0026. Saved before it was sent.
- **Brief 0026 done:** commits 81bc930 (merge of the tone view), 9705990, 05ed98e and
  4e11e63 on `work/pk-integrate`. Leak scan no hits in 123 files. Merged into
  `work/pagekit-prepare` (d6d17b2); the combined suite passes. Prepared pages and tone
  views now share one deterministic TIFF writer.
- **Brief 0027 done:** commit 5b5e7db on `work/pk-skew`. The agent read only numbers from
  the lead's spreads, never viewed or sent them, and built every test from synthetic
  pages. Leak scan no hits. Merged into `work/pagekit-prepare` (31fb320); the combined
  suite passes. On the host's re-run, the page tilted about 2.5 degrees is levelled, the
  page-edge stack is cut away, and the discarded-ink flag remains on one page of four.
- **Brief 0028 done:** commits 99ebaa6 and ee56315 on `work/pk-split`. The agent read only
  numbers from the lead's spreads and built every test from synthetic pages. Leak scan no
  hits. Merged into `work/pagekit-prepare` (1a7e872); the combined suite passes. On the
  host's re-run all four pages are upright with no orientation flag and the gutter
  overhang flag is gone. Because some settings were chosen on these two spreads, an
  independent review of generalisation is running.
- **Brief 0029 done:** commits 12ad0c5, f18f334, 0d03860, 42dbc38, ffee5a9, cb0607c,
  7aa8578, 595f661 and 34649a8 on `work/pk-integrate` (595f661 was committed with two
  core tests failing; 34649a8 repairs them). Leak scan no hits in 124 files. Merged into
  `work/pagekit-prepare` (786571d); the combined suite passes. A re-run on the lead's
  spreads with `--tone-view` wrote each page and its grey view as lossless TIFF.

## 0026 — Generalisation review of orientation; follow-up brief 0030

- **Independent review** of 99ebaa6 and ee56315 by a fresh agent bound by the clean-room
  rule (it saw no GPL source), on pages from its own generator: handwriting much
  improved and both real spreads right in all four turns, but small printed type gave
  confident wrong half turns with no flag, because the up-down vote depended on one
  core-band share fitted to cursive; and a pen stroke crossing the fold beside the
  backdrop band lost its overhang flag.
- **Follow-up brief:** `briefs/0030-build-orientation-print-and-overhang.md` (sha256
  afe2dda85b5c7dd68558dcfed143927a3c37786a20526762a7d30cce4858d0ae), to the agent of
  brief 0017. Saved before it was sent.

## 0027 — CI on the pagekit pull request; follow-up brief 0031

- The pull request carrying pagekit failed CI: under the check script's settings
  (PYTHONSAFEPATH=1, PYTHONPATH unset) a child Python started by a test could not import
  pagekit, and neither could the correction command the review sheet prints.
- **Follow-up brief:** `briefs/0031-build-ci-safepath.md` (sha256
  9b37693b1c9533cd468d1c7b2fb816e8583ff3819d34e1730b9d906fec5195f1), to the agent of
  brief 0023. Saved before it was sent.
- **Brief 0031 done:** commit 4617b6f on `work/pk-integrate`. Leak scan no hits. Merged
  into `work/pagekit-prepare`; the pagekit suite passes under the check script's settings.

## 0028 — Outside reading-side documents received; host exposure recorded

- **Received** at 2026-10-04T15:23Z from the lead, from outside ChatGPT reading-side
  sessions, and saved in `workbench/cleanroom-quarantine/` (git-ignored, never committed):
  - `63910cd1-PageKit-Reconciliation-Review.md`, 25858 bytes, sha256
    5f6e776d9cda0ee75a20d71778d79c7e7040630c8b64476fca19524b6ebb6645;
  - `68d24890-PageKit-Reconciled-Intake-Candidate.md`, 17902 bytes, sha256
    809c4ef938d5309a003879d6952658b8d1acb7ee5a1a57a7f2671fb73ac43f5c.
- **Departure from step 3 of "Outside reading-side sessions":** the documents were
  attached to the host's conversation, which loaded their full text, so the host read
  both before quarantine and before the report check. The host did not forward them to
  any build-side agent.
- **What the host saw:** no source code, no code blocks and no description of another
  program's internals. The only mentions of the excluded program are the metadata of the
  lead's own four hand-prepared TIFF pages (size, grey 8-bit, LZW, 600 dpi tag). The
  documents state that their authors' earlier research packets carry mixed exposure.
- **Report check:** both refused (no reader-brief digest, free-form sections, file paths),
  as working notes usually are. Nothing has been admitted into `findings/`.
- **Pending the lead's decision:** whether a reading-side agent should distil them into
  finding reports through the quarantine route. Until then no spec or brief draws on them.
- **Brief 0030 done:** commits 0c7f995 and 852c217 on `work/pk-split`. Leak scan no
  hits. Merged into `work/pagekit-prepare` (fd3041a); the suite passes under the check
  script's settings. Since the builder checked its work on the reviewer's set, that
  reviewer is re-testing on a new held-out set.
- **Correction to the entry above:** the lead attached the two documents as files, as the
  host had asked; the host then read them on receipt. The exposure was the host's handling,
  not the lead's. The lead has since approved distillation by a reading-side agent.

## 0029 — Unusable sources are skipped, not fatal; follow-up brief 0032

- **The lead decided** that one unusable source file must not stop a batch. The host
  changed spec 0002: such a file is skipped and named with its reason in the manifest, the
  review sheet and the output, with exit status 1; exit status 2 stays for problems that
  stop the whole run.
- **Follow-up brief:** `briefs/0032-build-skip-unusable-sources.md` (sha256
  9001e468ff317016a552d8166fdb0c7b0eb105b6f05a5b5dea4f187ee022a0c3), to the agent of
  brief 0023. Saved before it was sent.

## 0030 — Reading side: distil the two outside documents into finding reports

- **Who:** a fresh reading-side agent, started after this entry was committed.
- **Brief:** `briefs/0033-read-outside-documents.md` (sha256
  bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2).
- **Outcome:** recorded in a later entry once the host has run the report check on each
  report.
- **Brief 0032 done:** commit 54b960e on `work/pk-integrate`. Leak scan no hits. Merged
  into `work/pagekit-prepare`; the suite passes under the check script's settings. The
  host corrected spec 0002's wording: a missing output folder is created as before; only
  one that cannot be created or written stops the run.

## 0031 — Held-out re-test of orientation; follow-up brief 0034

- **Independent re-test** of 0c7f995 and 852c217 on a new held-out set of 74 pages the
  builder had not seen: small-print half turns gone; no regression; wrong-without-flag
  runs 42 under the old code, 12 now, all on pages of printed figures only, which the
  old code also got wrong. The overhang fix could still excuse a pen stroke touching a
  thick mark. The sign guard is 1.0 standard errors everywhere, untested at its boundary.
- **Follow-up brief:** `briefs/0034-build-orientation-figures-and-overhang.md` (sha256
  85d65656c9baaadb1dda822a473bafa296e2ee20412065278a35d36e6c009f11), to the agent of
  brief 0017. Saved before it was sent.

## 0032 — The reading side's reports checked, read and accepted

- **Who:** a fresh reading-side agent ran brief 0033 after an earlier agent stopped on a
  permission refusal; the lead then approved reading pagekit's README and specs. The
  agent's task message gave it only the brief's path and sha256.
- **What came back:** 35 files in the local quarantine folder; the agent's reply listed
  only their names and sha256 digests, which matched the files.
- **Check before reading:** 34 finding reports and one index. 32 reports passed every
  rule. Two were refused and deleted unread: `0026-colour-profiles-modes.md` (sha256
  0593168ba85410fc480891f797cdc526f576238c24826e7dfa3f311bc4fc7c9e; a line-number
  reference) and `0031-platform-qualification.md` (sha256
  74c200d23d319970e2d73abc3152c914178e620eb2db9b78281629b268012f20; an identifier shape).
  The index (sha256 b004da9e2c5d2c1749f53a00752a4e4c50a82c1991c7f629258b3128a643724a)
  failed only the template-structure rules.
- **Host's reading:** the host read the index and all 32 passing reports. They describe
  requirements and published or general methods in plain words, with pagekit's current
  behaviour from its README and specs; none describes another program's code or internals.
- **Accepted** into `findings/` as 0036 to 0067 (reader brief sha256
  bd3e1c50d4f2819416114fdebe2da7d245b3d9485f5f30c6a1589476880d56c2):
  - `findings/0036-reviewed-grey-main-page.md`: 61ea156f51c5a365b6793ab20ca0d2a1236b6fb3fd086706194dd6c570de31ef
  - `findings/0037-grey-loses-colour-evidence.md`: 34dc84ce7c6045d8f382f28ab38b7a1d423011ff5cd98f018aadec9102785134
  - `findings/0038-equal-channel-colour-files.md`: 757cb259e157db3d9c81f9a3f2512efda056d14c508fb4cc62f922c816843fc7
  - `findings/0039-collection-grey-profile.md`: 31cbd661e521ca2e021bd0739a5e5e26760b7bf50e152100b4ce94af1f1b8e69
  - `findings/0040-simple-manual-tone.md`: 58dae6c145ebef2908cd8d47278b612f71b109ee1155400eb07a91e189b52f89
  - `findings/0041-manual-editing-first.md`: bf8596b8065a15c5a76a5d6208ece355fc9cca329e00a73f13073b1d24f4c7f7
  - `findings/0042-detector-abstention.md`: d7152ada7157dc0dc64ebdc81473c6218c50b7b48766d9e7f9443a579abefbb9
  - `findings/0043-review-outcomes-next-action.md`: 562e9ad435f19fd3ec947210ac9431712cf0366d01392543847ee9b7d66e91a1
  - `findings/0044-review-from-final-candidate.md`: 6ab3be5ae516a4e4ca72b8348db0c5097588bfe998eb89286a5be3687097f814
  - `findings/0045-decoded-export-verification.md`: 080bd7d8ca3e7b8f7361ad8aceb4b396f7a2c20964e142a6e311c7b482be9787
  - `findings/0046-canonical-raster-identity.md`: 385d2e9e624e86512e6b83cbddcb51e0da249c0a184df81d5cec90b25eb7aafd
  - `findings/0047-lossless-export-profile.md`: 307ef57840355fbcf74d603a258003f3c4cff0a86a30545cc3abfc8d00f5afa9
  - `findings/0048-atomic-publication.md`: d9a62e127be4a2eee3a3147996588d0819bce03752ea1e3d19f1fdcf68640404
  - `findings/0049-density-pixel-aspect.md`: b29cdd2c8c4c811ac3e1b53070026f1c2efbf30e8ec38dc9652e109f40aa933d
  - `findings/0050-source-scale-explicit-smaller.md`: 22fdfeebe52dab9fe3ebe59417c7c465fc8b47f4077674155b7c0fd0a9ef1c5e
  - `findings/0051-resource-limits.md`: 629147d8145b1c97163f024c5846e76ea3418bac905c08aaca6f197350d7a45c
  - `findings/0052-uncertain-arrangements.md`: 085970671bcbcbd94a924817c1d3ce280298fca4926992fbdcdd3e92630e4173
  - `findings/0053-recaptures.md`: 06c1b481d54c703cfbdc65de432089d15e2eabad413644fb645b5ac813d4646a
  - `findings/0054-source-limited-loss.md`: 73d5139a2f3a2f1397cd94e290de93dfd55fd637d402a4290fdce7d18cee50d9
  - `findings/0055-preservation-checks.md`: 960b48bed5ffb8413a7e6b1edddbdc2c21325eacf1ba45faaa818d2e3c46311a
  - `findings/0056-margins-and-padding.md`: c32e7229c38c6dfb5a101e5916f426507c53f7f8484585658cb97f6a1d275f22
  - `findings/0057-metadata-orientation.md`: a58cc806b1935e4b944f1f35297a3f677bfbf0d51400a76d16b67f2a7742649d
  - `findings/0058-coordinate-spaces.md`: 8b2afbfb703cf52956d8849a069ab0a34b35fc3303f735776c2983ce579fe219
  - `findings/0059-uniform-scale-canvas.md`: c0907f787d4ff15227c9a162410ed94afe8917704ab2313c55e9756ee74b3d65
  - `findings/0060-interpolation-support-fill.md`: 97a4434241856a9e393d62ed2128bfdeac939b1e89085df8b49ab909eab2ff47
  - `findings/0061-source-identity-order.md`: f1f326653af4290124c302224c3dcbde121da0adcb2a67f59196c0d132c2abf0
  - `findings/0062-locks-revisions-scope.md`: 3deae1483bbdc6eabf620087ce432b3ac2c2af1b36cffce0e3c156c881c38bad
  - `findings/0063-shared-headless-operations.md`: 4e8c346b188f90faacd20d011b0d91a5f22ba62ba90aeab64676d5bc9fb0aa35
  - `findings/0064-proposal-authority-access.md`: 5ed21566c3181e16329037cea1e7dff18545a17207809cad14622e232aa020ec
  - `findings/0065-development-calibration-evaluation.md`: ab21645c88121f078c1b0a7f2fdb1965df03a73709344d4fb2c69357e6b55020
  - `findings/0066-review-scope-policy.md`: 01b962ce40de12180d956e94c8822bea293ccdecf4d57a9a1f7d4a4ef9a37ab7
  - `findings/0067-simple-controls-advanced-record.md`: 5b64b005deb8c67012a26249c13786ab6179049f6bfebdeb5eabdc8c0c5a5fd2

## 0033 — Spec 0007 written; brief 0035 issued

- **Spec 0007** (`specs/0007-orientation-tag-grey-page-padding.md`): the orientation tag
  applied once, a reviewed grey main page with an exact path for equal-channel sources and
  a colour-evidence check, padding apart from the margin, and density ratio kept. Written
  by the host from findings 0036 to 0039, 0045, 0049, 0056 and 0057. Defaults leave what a
  reader receives unchanged; the default margin is left as it is pending the lead's
  decision.
- **Brief:** `briefs/0035-build-tag-grey-padding.md` (sha256
  6801b6788c3e905fb22feed54a4dccc7eb2bb87a43dd0754ad59e99784d6fd45), to the agent of
  brief 0023. Saved before it was sent.
- **Brief 0034 done:** commits fc5226f and 39f511b on `work/pk-split`. Leak scan no hits.
  Merged into `work/pagekit-prepare`. The builder did not run the reviewer's generator,
  so its held-out set stays unseen; the reviewer is re-running it.

## 0034 — Second held-out re-test; follow-up brief 0036

- **Re-test** of fc5226f and 39f511b on the reviewer's held-out set: wrong-without-flag
  runs 12 to 0; no regression; 7 pages right before are now flagged. Two narrower cases
  remain: a printed account page mixing words and figure columns, and a pen stroke running
  along the backdrop band.
- **Follow-up brief:** `briefs/0036-build-orientation-tables-and-band-strokes.md` (sha256
  08f0717868d3e2855ceb780c5270e3bdc27d229ca39073eeb92f3d6306d0c020), to the agent of
  brief 0017. Saved before it was sent.
- **Brief 0035 done:** commits 514e6f0, 490444e, abb602c, 45ba2cc and f94a0d6 on
  `work/pk-integrate`. Leak scan no hits in 168 files. Merged into `work/pagekit-prepare`.
  A test pins that defaults leave prepared pages and manifest values unchanged. An
  independent review is running.
- **Brief 0036 done:** commits 7d4b148 and cdd6dd9 on `work/pk-split`. Leak scan no hits.
  Merged into `work/pagekit-prepare` (2952d93). The builder flags thin dropped pieces that
  cross the cut but not the excused thick backdrop itself, so the real spread's false flag
  stays gone; the host accepted that. The reviewer is re-running its held-out set.

## 0035 — Mac CI failure in the defaults pin; follow-up brief 0037

- The Apple-silicon Mac CI job failed the defaults pin test: it pinned the file hash of a
  synthetic source whose encoded bytes differ by platform.
- **Follow-up brief:** `briefs/0037-build-platform-independent-pins.md` (sha256
  411b6757aa638b12f4054476a8a3f07f01f3eec5f22fc9c00a4ef1b28fdd3c9f), to the agent of
  brief 0023. Saved before it was sent.
- **Brief 0037 done:** commit e7c0922 on `work/pk-integrate`. Leak scan no hits. Merged into
  `work/pagekit-prepare`; the pin compares decoded pixels and regenerated its data from
  2d10a14.

## 0036 — Third held-out re-test; follow-up brief 0038

- **Re-test** of 7d4b148 and cdd6dd9: no regression; figure pages 10 wrong to 0; band
  strokes 71 missed flags to 0; one handwritten account page, flagged before, is now a
  wrong half turn because figure tiles still vote up or down.
- **Follow-up brief:** `briefs/0038-build-orientation-figure-tiles-updown.md` (sha256
  6ed5bf8bad155d6d373bcf6f333f22fd63dda1eab5c28467d41b916115379aea), to the agent of
  brief 0017. Saved before it was sent.
