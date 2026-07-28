# Pre-rebuild intent — what Tyrel has actually said he wants

Assembled 2026-07-27 from three prior session transcripts (`workbench/raw/2026-07-27_session-trawl/`:
`harness-wrapup.md`, `file-cleanup.md`, `airlock-audit.md`) cross-checked against every
canonical document and everything in `workbench/active/`. **This is a note, not a
decision** — nothing here binds anything; it exists so nothing he said gets lost before
the rebuild starts writing code. Where the three extraction files disagree with what is
below, re-read them; this is a summary and summaries are the thing this project distrusts
most.

---

## Unrecorded — said but never written down

- **A layman must be able to use the finished pipeline, at near-commercial grade for
  function and ease of use** — "clear errors, obvious next steps, no incantations." This
  was in the original brief (`workbench/design/REBUILD_PLAN_PROMPT.md`, now marked
  superseded) but never made it into `RUN_PLAN.md`, the document actually governing
  execution, and it appears nowhere in `GOALS.md`, which is where a usability bar for the
  end product would belong. Right now nothing binding says the pipeline has to be usable
  by anyone but the person who built it. (airlock-audit)
- **Large models and files should be fetched once, directly on the pod, never uploaded
  through git or pushed from the laptop** — "no bottleneck ideally from the git or what
  ever." Same story: stated plainly, in the superseded brief only, dropped from
  `RUN_PLAN.md`. Nothing in Phase 2 (pod/launch) or Phase 7 (Perlector serving) currently
  commits to this. (airlock-audit)
- **User-facing settings belong in config, not code** — "we can add user settings in a
  config setting for ease of use." Also only in the superseded brief. `RUN_PLAN.md`
  commits to `config/models.toml` for model identity but says nothing about a broader
  operator-facing config layer. (airlock-audit)
- **A periodic audit habit** — Homebrew packages, Claude-affecting plugins/add-ons, and
  RTK specifically — checked on some recurring cadence for what's genuinely unneeded and
  what's out of date or off best-practice. He asked for this once (file-cleanup session);
  it produced a one-time report (all 30 Homebrew installs in use, six zero-use plugins
  found) and nothing recurring. No document — not `session-start`, not `CLAUDE.md` — makes
  this a habit rather than a one-off. (file-cleanup)
- **When reviewers check a proposed replacement file, they must actually read the new
  file, not spend the review budget diffing against the old one.** His words: "It's fine
  if they look at the old AND the new but why not let them look at what we are trying to
  change it to." This is a real, specific instruction about how to prompt reviewers on a
  rewrite-in-place, and `reviewer-pass/SKILL.md` doesn't contain it. (file-cleanup)
- **Self-hosting or another GPU rental service should be possible later without a
  rewrite** — framed as "would be cool," not a requirement, but a real stated preference.
  `RUN_PLAN.md` carries the mechanical consequence (one `provider.py` seam, no
  multi-provider abstraction now) but the aspiration itself — that this matters enough to
  keep the door open — isn't written down anywhere that survives past the superseded
  brief. (airlock-audit)
- **A research pass grounding agent/effort choices in Anthropic's own prompt guides for
  Sonnet 5, Opus 5, and Fable 5** (and by extension GPT Terra/Sol), so those choices "stop
  being guesses and stop wasting API budget." He asked for this explicitly. What exists
  instead is a compressed paragraph of conclusions in `CLAUDE.md` ("The models, in one
  breath") — useful, but not the grounded research pass he described, and nothing tracks
  the gap. (harness-wrapup)
- **Pruning the superseded local branches `work/t` and `work/t2`.** Named as follow-up
  once `infra/workspace-readiness` lands. Both branches still exist locally (confirmed via
  `git branch`), and nothing in `HANDOFF.md` or elsewhere tracks this as still owed.
  (file-cleanup)

## From this session (2026-07-27 evening) — added by hand

The three readers could only see *past* sessions. These are Tyrel's own words from the
session that assembled this file, with the same status marking.

- **A session should know whether it is being watched, and behave accordingly.**
  "I will be here for the next 1-2 hours so feel free to ping and ask since this is not an
  unmonitored session (this should also be a behaviour for each session)." **UNRECORDED.**
  Nothing in `CLAUDE.md` or `session-start` asks a session to establish whether it is
  attended. It matters in both directions: attended, ask the question rather than guessing;
  unattended, never block on him and route around what cannot be answered.
- **Keep the main session lean on context so it stays on the goal.** Stated as the reason
  for delegating. **UNRECORDED** as a principle — it is followed in the brief, but no
  binding document says an orchestrator's job is to hold the goal rather than the detail.
- **GPT is the session's tool, not a second driver.** "I am envisioning GPT being your
  slave tool that you control more than me driving it." **UNRECORDED** in the documents.
  Consequence worth writing: where Claude and GPT disagree, the Claude session owns the
  synthesis and the final answer.
- **Roles, as he sees them.** Sonnet and Terra are the general workers; Opus and Sol are
  the higher-level auditors and planners; Spark and Haiku are the cheap readers. Fable is
  rare "till it is updated, since Opus benchmarks the same at half the price."
  **CAPTURED** in `NEXT_SESSION_BRIEF.md`, but it belongs in `CLAUDE.md`'s roster
  paragraph, which is the document sessions actually read.
- **Agent memory stays off.** His decision, after the trade-off was put to him: it breaks
  blind review and gives old-code knowledge a route across the quarantine inside an
  agent's head. **CAPTURED** in the brief; should also appear in the roster files as a
  deliberate absence rather than an oversight, so nobody "fixes" it later.
- **Commit is fine; push is not.** He corrected an earlier conflation: "I am okay with you
  making changes and fixing things if that is what committing is, what I don't want yet is
  a push," and approved changing the permissions and hooks to match. **CAPTURED** in
  `.claude/settings.local.json` and three commits — but `CLAUDE.md`'s hard rules still
  read as though committing and pushing are one gate.
- **GPT may write inside the repository, in designated areas, when a session drives it.**
  With the location labelled in the prompt or the permissions, and either a watcher or
  git itself used to see what it touched. **CAPTURED** in `NEXT_SESSION_BRIEF.md`; the
  mechanism is explicitly left to the next session to work out and test.
- **Explain results in the chat, as a layperson would need them** — "clean and concise not
  buried in folders or with half meanings." **CAPTURED** in project memory
  (`explain-results-in-chat-as-layperson`), which is outside the repository — so a fresh
  clone or another machine does not have it.

## Partially recorded

- **`RUN_PLAN.md` §5 is already known-stale by the project's own later work, and hasn't
  been fixed.** It still says Codex self-orchestration (Sol spawning Terra in one run) is
  "unverified" and describes a cascade where a read-only orchestrating seat drives writing
  Terra builders. `ORCHESTRATION_FINDINGS.md` (this same day) verified self-orchestration
  *and* proved the described cascade cannot work as written — a Codex child inherits the
  parent's sandbox, so a read-only parent cannot spawn a writing child. `NEXT_SESSION_BRIEF.md`
  names both corrections explicitly as "known-stale in RUN_PLAN already, before the night
  starts" — but the fix was never applied back to `RUN_PLAN.md` itself. Anyone reading only
  the plan gets the wrong shape.
- **The GPT/Claude model roster's own hardening.** He asked that agent role definitions be
  "refined and hardened... clearly accessible and understood." The roster table in
  `CLAUDE.md` and the six files in `.claude/agents/` exist, but every one of them uses only
  `name/description/tools/model/effort` — none use `isolation: worktree`, `maxTurns`,
  `permissionMode`, `disallowedTools`, `skills`, `hooks`, or `background` (verified by
  reading all six files directly). `NEXT_SESSION_BRIEF.md` names this gap explicitly
  ("isolation: worktree... would make the worker/rebuilder/infra-worker worktree rule real
  rather than asked-for") — worked-in-prose, not mechanically enforced.
- **The two workflow templates** ("Converging audit" and "Bulk mapping at scale") he asked
  be designed and left behind as worked examples. They're described in prose in
  `NEXT_SESSION_BRIEF.md` but no template file exists anywhere in the repository or
  workbench — nothing a future session could actually run.
- **RTK configuration so token-saving doesn't degrade pod/SSH/AWS verification.**
  `CLAUDE.md`'s "The tooling may filter what you see" section covers the general case
  (re-run unfiltered when a count looks suspiciously round) but not his specific ask —
  config settings or written per-tool instructions so the important cases (pod, SSH, AWS)
  don't get filtered in the first place.
- **Disabling the six zero-use plugins** (legal, finance, data, design, product-management,
  cowork-plugin-management). The finding is recorded, but only inside an *archived* handoff
  (`workbench/archive/2026-07-27_doctor-and-notifications/HANDOFF_incoming.md`) — a folder
  nobody re-reads for open work. Distinct from the `productivity`/`engineering` plugins,
  which he did approve disabling and which are recorded (if only in a handoff) as done.
- **Two decision-relevant plan facts don't square with each other.** D2's argued
  recommendation ("prevent leakage outright from commit one") is a reversal of his own
  earlier tolerance — see Conflicts below — and D1/D2 both remain open, so this isn't
  fully resolved either way yet.

## Already captured

**Identity and quarantine** — "rebuild, not migration," used consistently: `CLAUDE.md`
("Quarantine"), `README.md` ("alpha — a rebuild laboratory"), `GLOSSARY.md` (retired-terms
table). Autoclave as a cleanroom bench, not a landing zone, with drafts committed on a work
branch: `CLAUDE.md`, `RUN_PLAN.md` §2.10. Even the one file judged fit to carry across gets
full line-by-line review: `RUN_PLAN.md` §3.

**Pushing and review** — ask-before-push and ask-before-review as separate permissions,
three-reviewer default with explicit per-request reduction only, triage by sense not a
numeric scale, work reaches a PR by default: `CLAUDE.md` ("Pushing and merging"),
`reviewer-pass/SKILL.md`. Reviewers propose a fix, never apply one:
`reviewer-pass/SKILL.md` ("Ask each finding to carry what the reviewer would do instead").

**Agents and orchestration** — roster with model/effort pinned per role, standing approval
to use agents/workflows once declared at session start, model-to-GPT-tier mapping
(Sonnet≈Terra, Opus≈Sol): `CLAUDE.md` ("Agents"), `NEXT_SESSION_BRIEF.md`. The
Opus→Sol→Terra→Sonnet→Opus cascade, with his own hedging preserved: `RUN_PLAN.md` §5,
`DISPOSITION.md` (orchestration ruling). `codex exec -m` verified to accept
`gpt-5.6-terra`/`gpt-5.6-sol`; Sol's self-orchestration verified: `ORCHESTRATION_FINDINGS.md`.

**Workbench and handoff hygiene** — design-note folder built, six-drawer system, moves not
deletes, "map problems, don't enumerate them": `workbench/README.md`,
`session-start/SKILL.md`, `session-end/SKILL.md`. Session-end briefs the next session with
goal/model/effort/size/honest duration: `session-end/SKILL.md` §7. Agents never run
session-start/session-end: `CLAUDE.md`, both skills.

**Attribution and governance** — flexible, per-release attribution
(`Co-Authored-By`/`Reviewed-by`, named by release for every vendor): `CLAUDE.md`
("Attribution"). CLAUDE.md scoped to how sessions work, not the pipeline's own rules:
`CLAUDE.md` opening paragraph. GLOSSARY governs pipeline vocabulary only: `CLAUDE.md`,
`GLOSSARY.md`. **The GOVERNANCE.md amendment he was asked to ratify (a Governance 0
addition, the Governance 2 extension to findings/reviews, and the Governance 10 instrument
rule) is present in the current `GOVERNANCE.md`** — the trawl's uncertainty about whether
he ever said yes is resolved by the document itself: only he can amend it, and it's there.

**Notifications** — four moments only (start/milestone/decision/done), main-session-only,
one line each: `CLAUDE.md` ("Notifications"), rebuilt `notify.sh` per `DISPOSITION.md`.

**Reporting to him** — plain language, a recommendation not a survey, never make him read
code to decide: `CLAUDE.md` ("Reporting"), matches his own words in the airlock-audit
session almost verbatim.

**Effort and shape** — size the session out loud before starting, worked examples for
small/large-straightforward/large-open-ended tasks, re-size when the task changes:
`session-start/SKILL.md` §7, `CLAUDE.md` ("Effort and shape").

**Versions and distribution** — alpha (rebuild lab) → beta (fresh, clean, alpha survivors
only) → 1.0 (public, allowlisted export): `README.md` ("Versions").

## Conflicts

- **Personal data in alpha.** In the airlock-audit session he said plainly: "a little bit
  of personal data leaking in for now is okay as long as we note it... it should work end
  to end but can be clunky till we want to promote to beta." `RUN_PLAN.md` §4 then argues
  the opposite for decision D2 — "prevent leakage outright from commit one" — on the
  grounds that the old repository became permanently unpublishable exactly this way.
  Neither view has been withdrawn. **D2 is still open**, so this is a live disagreement
  between what he said once and what the plan now recommends he decide, not a settled
  question either way.
- **`RUN_PLAN.md`'s cascade description contradicts `ORCHESTRATION_FINDINGS.md`, both
  written the same day.** See "Partially recorded" above — the plan still calls
  self-orchestration unverified and describes a shape the findings proved impossible. This
  isn't a disagreement between him and a document; it's two of his own project's documents
  disagreeing with each other, and whoever reads `RUN_PLAN.md` next will build from the
  stale one unless it's fixed first.
- **CLAUDE.md's length.** He originally wanted it under roughly 100 lines ("based on old
  thoughts... leaner is better") and later approved a further bloat-strip when it crept to
  ~201/204 lines. It is currently 247 lines. Not necessarily a problem — the file has taken
  on real content since (the agent table, the notification rules, the quarantine section)
  — but it sits well outside the number he originally named, and nobody has revisited
  whether that number still applies now that the file does more.

## Open decisions still waiting on him

- **D1 — picker scope**: narrow (8 files) or broad (35)? Gates how every rebuild dossier
  reads the airlock. `RUN_PLAN.md` §4; audit recommends broad.
- **D2 — personal-data stance**: prevent by construction from commit one, or tolerate-and-note
  for alpha? See Conflicts above — his own earlier words and the plan's recommendation
  point opposite ways. `RUN_PLAN.md` §4.
- **The single, fully-reviewed push** he deferred to "the end of the next session or the
  one after" — still not done; the working tree on `infra/workspace-readiness` is still
  unpushed. `HANDOFF.md` ("Needs Tyrel").
- **Flipping `autoclave-empty` to a required check on GitHub** — a one-click action only he
  can take; still unflipped. `HANDOFF.md`, `RUN_PLAN.md` §2.10.
- **The licence email to the handwriting-witness publisher** — costs nothing, takes weeks,
  still not confirmed sent. `RUN_PLAN.md` §4 (Open Question 20), `HANDOFF.md`.
- **Whether he wants a workbench "list of available tools."** He raised it as an open
  question himself and never gave a final answer either way (file-cleanup session,
  flagged there as Uncertain). Not a firm ask — worth a direct question rather than a
  guess.
- **Six queued TYREL rulings from `DISPOSITION.md`** not yet closed out: hard rule 6's
  wording against the committed tray, the three narrow `.claude/settings.json` denies
  (delete or keep as courtesy), merge-commit attribution requirements, and others — full
  list in `workbench/active/reviews-2026-07-27/DISPOSITION.md` under "TYREL — queued
  rulings."
