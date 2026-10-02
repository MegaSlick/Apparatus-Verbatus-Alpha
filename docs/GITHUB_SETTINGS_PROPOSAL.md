# GitHub settings proposal

Settings that only the repository owner can change on GitHub, recommended by the
2026-10-02 project review. None of these is applied by any commit; the lead applies them
by hand under **Settings** on GitHub. Each item says where it is, what to set and why.
Delete this file once it has been applied or declined.

The repository is **public**, which decides several of the items below: GitHub's
secret scanning and push protection are free for it, and anything committed is
readable by anyone.

## 1. Landing page (Settings → General, and the ⚙ beside "About")

- **Description**, shorter so it reads whole on a phone:
  "Reads handwritten historical registers, mainly Quebec parish records, with vision
  models as witnesses and a reader model that establishes the text from the ink.
  Every reading traces to its image region; uncertainty is flagged, never guessed."
- **Topics**: keep the eight already set; add `historical-documents`, `transcription`
  and `vllm`.
- **Website**: leave empty until there is one.
- **Include in the home page**: untick *Packages* and *Deployments* (none exist), keep
  *Releases* off until the first release.
- **Features**: turn off *Projects* (unused). Wiki is already off; keep *Issues* on.

## 2. Pull requests (Settings → General → Pull Requests)

- **Automatically delete head branches: on.** A branch then deletes itself when its pull
  request merges, unless a protection rule on that branch forbids deleting it. Branches of closed or superseded pull requests still need deleting by
  hand, as in issue #242.
- **Allow merge commits: on; squash and rebase: off.** The history is built from merge
  commits and the review rules assume them.
- **Always suggest updating pull request branches: on.**

## 3. Protect `main` (Settings → Rules → Rulesets, or Branches → `main`)

`main` is protected today; check that the rule includes all of these:

- **Require a pull request before merging**, with **0 required approvals** (the lead is
  the only maintainer and cannot approve their own pull requests; CodeRabbit comments
  but does not approve).
- **Require status checks to pass**: the single check **`check`**. It is the summary job
  in `.github/workflows/ci.yml` and passes only when every Python version's test job
  passed. Do not list `test (3.12)` or `test (3.14)` themselves: their names change
  whenever the version matrix does.
- **Require branches to be up to date before merging: on.** CI tests the branch head,
  so this is what guarantees the tested code is the merged code.
- **Require conversation resolution before merging: on.**
- **Block force pushes** and **restrict deletions** of `main`.
- **Nobody may bypass it**, so the rule also binds sessions that push with the lead's
  token. For a ruleset, leave the bypass list empty; for classic branch protection, tick
  *Do not allow bypassing the above settings*, because administrators can bypass it by
  default.

## 4. Security (Settings → Code security)

- **Secret scanning: on**, with **push protection: on.** It blocks a push containing a
  credential of a format GitHub recognises, as a second line behind the local commit
  hook. It is not a guarantee: it misses formats it does not know, and anyone with write
  access can choose to bypass a block, which on a public repository publishes the
  secret.
- **Dependabot alerts: on** (already) and **Dependabot security updates: on.**
- **Private vulnerability reporting: on**, so a stranger can report a problem without
  opening a public issue.
- **The 17 open Dependabot alerts** (13 high, 4 moderate, as of 2026-10-02) need a look.
  CI's `pip-audit` passes, and it audits the runtime dependencies plus the `test` and
  `audit` groups but not the `pod` group, so the alerts most likely sit in the pod group
  (vLLM, torch, transformers), which only runs on a rented GPU machine. Check each
  alert's package before deciding. Dismiss each with a reason, or plan an update with
  a paid pod qualification run.

## 5. Actions (Settings → Actions → General)

- **Actions permissions**: *Allow MegaSlick, and select non-MegaSlick, actions*, listing
  `actions/checkout` and `actions/setup-python` (the only two used, both pinned by
  commit).
- **Workflow permissions**: *Read repository contents* (the workflow already asks for
  no more), and leave *Allow GitHub Actions to create and approve pull requests* off.
- **Fork pull request workflows**: *Require approval for all external contributors*, not
  only first-timers, so no outsider's code runs in CI until the lead approves it.

## 6. Apps (Settings → GitHub Apps)

- **CodeRabbit**: keep. Once #253 merges, it reviews only pull requests carrying the
  label `review-ok` (configured in `.coderabbit.yaml`).
  - Create the label under **Issues → Labels**. Only people with triage or write access
    can add labels, so outsiders cannot add it themselves.
  - The lead adds it to an outsider's pull request to allow a review. The lead's own
    sessions add it to theirs.
  - Test the lock once: from an account that is not a collaborator, comment
    `@coderabbitai review` on a test pull request. If a review starts, the comment lock
    does not cover this personal repository. Then turn off CodeRabbit's chat for this
    repository in CodeRabbit's own settings (app.coderabbit.ai), or accept the risk:
    a stranger's comment can then use review allowance.
- **Claude**: keep while AI sessions push branches and open pull requests.
- Remove any other installed app that nothing in this repository uses.

## 7. Stale branches

Issue #242 lists 43 stale branches, each checked as merged or superseded, with the
command to delete them. Keep `main`, `work/dai-own-detector` and
`work/witness-floor-census` until the review's pull requests settle them, and
`review/progress` until the review's train has merged.
