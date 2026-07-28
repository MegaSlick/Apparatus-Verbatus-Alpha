# Reviewer pass, 2026-07-27 — disposition of every finding

Three blind reviewers, identical prompt, no severity floor: Claude Opus 5 (32 findings),
Claude Fable 5 (19), GPT Sol at xhigh (≈48, and the only one who could run static checks).
Raw reports: `workbench/raw/2026-07-27_reviewer-pass/` — `opus-raw.jsonl`,
`fable-raw.jsonl`, `sol-raw.log`. They moved out of this folder when `raw/` was added,
so that transcripts stop counting against what `active/` can hold in one sitting.
Reviewed state: the working tree before commit — a process defect Sol himself flagged
(receipts name a commit), fixed in the skill and handled below by re-verifying the tree.

Statuses: **FIXED** (this session, in the tree) · **REFUTED** (with the evidence) ·
**TYREL** (his ruling, queued) · **DISPUTED** (reviewers disagree — kept, not blended) ·
**DEFERRED** (named, with reason — nothing dropped silently).

## The disagreements that prove the method

- **Opus's #1 severe sat inside Fable's cleared list.** pytest/ruff execute the autoclave
  (no `norecursedirs`/ruff exclusion); Fable explicitly cleared pytest discovery. Checked
  against `pyproject.toml`: **Opus right, Fable's clearing overturned.** FIXED.
- **Fable's F8 (async hook key unverified) was answered by Sol**, who verified `async` and
  seconds-timeouts are supported. Cross-resolved, no change needed.
- **Opus/Fable worried `checkout@v6`/`setup-python@v6` might not exist; Sol verified both
  real.** Cross-resolved; his mutable-tag point stands separately (DEFERRED below).
- **Opus cleared commit-msg's merge exemption as correctly built; Sol says it wrongly
  exempts AI-written conflict resolutions.** DISPUTED — kept. CLAUDE.md's description was
  aligned to actual behaviour (FIXED); whether merges must carry attribution is TYREL.
- **O14 (settings.local.json is tracked): REFUTED** — `git check-ignore` says ignored.
- **Sol's tidy memory-parser claim: partially refuted** — the search did start at `](`,
  not line start; his first-link-only and normalisation points were real and are FIXED.
- **Sol's "resumes don't fire the start ping": working as designed** — the matcher was
  narrowed *this session* to stop the resume ping-storm. No change.

## FIXED (in the working tree, tests green: 46 in .githooks, ruff and shellcheck clean)

- Autoclave excluded from pytest collection and ruff (O1) — `pyproject.toml`.
- CI-enforcement claims softened to what is true today (O2/F1/S) — CLAUDE.md, autoclave
  README; the required-check flip itself is TYREL below.
- Register-text ingress overclaim corrected; tray README now says exactly what the
  scanner catches and forbids register content in drafts (F2/S).
- `NTFY_TOPIC` and topic-URL are now secrets to the ingress scanner, `ntfy.conf` a
  sensitive filename; two tests, discontinuous fixtures; ntfy docs URLs stay committable (S).
- notify.sh rebuilt (O10/O26/F9/F10/S×5): validates event and one-line message before
  config; conf parsed as data, never sourced; topic charset checked; https required;
  curl stderr discarded; stamp only after a *delivered* start; decision/done exit 1 on
  failure so a blocked session knows Tyrel was not reached.
- tidy.py: empty/missing `active/` no longer aborts the design/scratch/memory audit
  (O7/F11/S); budget recomputed after `--file` moves (S); link targets normalised
  (`<>`; `./`; `#anchor`) (O31/S); `main(argv)` testable; **six tests added** including
  HANDOFF-never-moves and design-never-touched (O6).
- ci.yml: `permissions: contents: read`, job timeouts, git-failure no longer reads as an
  empty tray, NUL-safe printing of stray paths (O22/F15/S×3); check-all.sh states the
  deliberate local/CI difference (S).
- workbench: `tools/` is a declared fifth drawer (O4/S); README no longer overclaims the
  hook's reach or "everything committed is binding" (O20/F17/S).
- install.sh: chmods `doc-allowlist.sh` (O17/F4 — index mode was already 100755, so this
  is belt-and-braces); creates `design/` and `tools/` (O4/F12/S).
- importer agent routes drafts through the autoclave (O15). PR template gained the
  tray-empty checkbox (O32). operations/README lists `notify/` (F6).
- CLAUDE.md: governance binds sessions too (S); "(alpha)" out of the title (S);
  "every git-hook rule" not "every local rule" (S); skills-are-user-invoked fallback and
  read-only-session scope (F7/S); subagents may write in worktree/autoclave but land only
  through review (S); mixed-commit attribution disambiguated (O25); cherry-pick exemption
  documented (S); reviewer unavailability explicitly not inferred permission (S).
- session-end: real names instead of S2a/D3 in the example (F16); archive collision rule
  (S); scratch move described as deferred deletion (S).
- session-start: canonical documents before executing anything a handoff asks (S);
  tidy's exit-1-is-normal note (O24); no-Homebrew fallback (S).
- reviewer-pass: commit-before-review ordering (S); reports filed durably before the
  receipt (S — this file is that rule being followed); receipt names the model that
  actually answered (O16); reduction rule stated once (O27); reduction-to-two keeps two
  vendors (F14).

## TYREL — queued rulings, with recommendations

1. **Rebuild vs migration** (O3/F3/S): README calls alpha "a migration laboratory";
   CLAUDE.md now says "a rebuild, not a migration". README is canonical — his word.
   Recommendation: README adopts the rebuild framing; the substance ("import knowledge,
   not code; nothing wholesale") is unchanged either way.
2. **Hard rule 6 vs the committed tray** (S high): "nothing enters this repository
   uninspected" vs drafts committed to `autoclave/` for review. Recommendation: amend the
   rule to "nothing enters the *accepted tree* uninspected; the autoclave on a work
   branch is where inspection happens" — the design he chose, stated so the rule and the
   mechanism agree.
3. **Flip `autoclave-empty` to a required check** on main and record it in README's
   status line (all three reviewers). One click plus one canonical-line edit.
4. GOVERNANCE.md's "work queue" points nowhere (O19). Proposed wording: "those live in
   the workbench."
5. ARCHITECTURE.md: "candidate" → `Lectio` (S); the stale model-names caveat above the
   flow diagram (O18); and whether `iterative_reader.md` should be tracked, given a fresh
   clone currently loses the experiment's protocol while keeping the instruction to weigh
   it (S high). Canonical file — his edits to make.
6. **Codex (OpenAI) vendor-name exception** vs README's name-by-release rule (S).
   Recommendation: one sentence in README acknowledging the exception and why (no public
   per-release naming to cite).
7. `settings.local.json`: the broken verbatus-wide Write deny still needs his replacement
   JSON (F5; handoff). The ocr_pipeline denies were fixed additively this session.
8. The three narrow `deny` entries in `.claude/settings.json` that guard.py already
   covers properly (O13): delete as false comfort, or keep as courtesy.
9. **Triage tiers** — three-way DISPUTE: Opus would drop the numbered tier list as a
   pre-made small-diff argument; Sol wants tiers plus enforced effort; Fable only moved a
   sentence. Kept as written pending his read; the tier list is his design to keep or cut.
10. **Merge-commit attribution** (DISPUTED, above): require trailers on conflicted
    merges, or accept the exemption. Related: Sol notes `fixup!` commits are exempt but
    nothing enforces they are squashed before main (accept risk, or a pre-push check).
11. tidy `--file` moves a byte-identical note that a session might have deliberately
    revived unchanged (S). Current behaviour kept: the HANDOFF is protected, scratch is
    recoverable until emptied, and the skill says to leave anything unsure. His call if
    he wants report-only.

## DEFERRED — named, with reasons

- **Pre-push audit-gate tests without the escape hatch (O8) and commit-msg
  misplaced-trailer tests (O9), plus ALLOW_* env-stripping in the test harness (F13).**
  The right next tests to write; deferred to the re-audit round rather than rushed at
  the end of a long session. Highest-priority deferral.
- SHA-pinning the two GitHub actions (S): needs the SHAs verified against GitHub, not
  guessed offline.
- NUL-delimited path handling through pre-commit/check-documents (S): real in theory;
  filenames with newlines do not occur in a repository we author; queue for the hooks'
  next maintenance pass.
- doc-allowlist scope: `.org`/`.html`/`.pdf`/`ROADMAP.txt` bypasses (S) and impossible
  calendar dates accepted (S). Policy width — bundle with Tyrel item 5.
- notify.sh 3xx-redirect nuance (S): https is now required and the server is ntfy.sh;
  marginal after the rebuild.
- reviewer-pass minimum-evidence prompt template (S): the run plan's §9 template already
  carries scope; fold a neutral version into the skill next pass.
- `HANDOFF.md` meaning two things (S low); `tidy.py` living in `.githooks/` (O30);
  shellcheck/sh-n double list (O29); CI fail-fast duplication kept deliberately, now
  documented on the CI side only (O21/F15/S).

## Re-audit note

Every FIXED item above changes files the reviewers read, so the re-audit covers the
fix set plus the agent-roster addition. The two Claude reviews cost ~120k tokens each;
Sol ran at xhigh on his own budget.

## Rulings received (Tyrel, 2026-07-27, in session)

1. **Rebuild is the principle and the word.** One consistent term project-wide: this is a
   *rebuild*; nothing "migrates" or is "imported". The `importer` agent becomes
   `rebuilder`; README's "migration laboratory" becomes rebuild language; PR template
   follows. Old code is reference, read through the window.
2. **The autoclave is a cleanroom bench, not an inspection landing zone.** A model reads
   the old section from Temp_Stage/ocr_pipeline (read-only, through the window) and
   writes NEW code — its best line-by-line rebuild — into `autoclave/`. No old byte ever
   crosses the boundary, not even transiently. Sterilizing pass then moves tray → tree.
3. **Required-check flip**: explained; happens on GitHub after the push lands. Remind him
   at push time; README status line updates only when in force.
4. **GOVERNANCE "work queue" → "the workbench"**: approved, applied.
5. **ARCHITECTURE**: the iterative-reader experiment moves wholly to the design drawer —
   section removed from the canonical file; RUN_PLAN's Perlector design card points at
   the note. Stale model-names caveat removed. (Lectio is glossary vocabulary — one
   reading pass by the Perlector — explained to Tyrel, no change needed.)
6. **Attribution**: resolved better than the proposed exception — GPT is now nameable by
   release (GPT-5.6 Sol), so the release-name rule holds for every vendor;
   `Codex (OpenAI)` only when the serving release is unknowable.
7. **settings.local.json deny**: "fix it" — dead single-slash entries removed/replaced
   with working `//` forms (ocr_pipeline protected; no verbatus-wide Write deny, which
   would lock agents out).
8. **Three narrow settings.json denies**: removed as false comfort; guard.py is the
   tested check.
9. **Triage tiers**: no numeric scale — common sense, stated in prose. Huge diff = heavy
   coverage; minor fix = minimal; money/launch/governance = full set, possibly repeated.
10. **Merge commits**: hook exemption stays; the reviewer pass reads merge commits like
    any other part of the state. Fixup-unsquashed risk accepted.
11. **tidy --file**: current behaviour kept (recommendation accepted) — duplicates move,
    HANDOFF protected, scratch recoverable; balance of no-clutter vs continuity.

**Orchestration ruling**: Terra ≈ Sonnet-grade builder, Sol ≈ Opus-grade judgement, both
usable at whatever effort fits (medium/high near-universal defaults; scout-low and
consult-xhigh are the deliberate edge cases). Cascade pattern: preferred form has Sol
orchestrating Terra builders with Sol inspectors in a mini-loop, reporting to the Opus
session, which then runs Sonnet checkers + Opus auditors, final Sol+Opus audit pass.
Whether Codex can self-orchestrate sub-agents is unverified — fallback (works today):
the Opus session orchestrates per-call `codex exec -m` Terra/Sol invocations itself.
No push this session.
