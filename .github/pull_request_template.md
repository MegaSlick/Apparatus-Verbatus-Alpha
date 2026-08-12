## What changed

<!-- Plain English. One or two sentences. No code. -->

## What it touches

<!-- Which stage, or which part of the harness. -->

## Why it is here

<!-- If this rebuilds old behaviour: what is it for, and what was deliberately left
     behind? If you cannot say what every line is for, it is not ready. -->

## Rebuild record, when applicable

<!-- Name the coherent legacy system read before this was written. For every rebuilt
     piece: the old path read, the new path written, and what was deliberately left
     behind. Identify carried implementation in both the commit message and report,
     including its third-party source and confirmation that the licence permits use here.
     Record engineering decisions and reasons. -->

## How to undo it

<!-- One line. Revert the commit, delete the file, flip the setting back. -->

## What proves it works

<!-- Tests, a check that runs, or an honest "nothing yet — this is scaffolding". -->

## Review candidate

<!-- Full Candidate and Base SHAs reviewed; reviewers and receipt paths. The PR tip must match
     Candidate and the diff must begin at Base. -->

## Review findings

<!-- List every finding from the named review reports and mark it fixed or declined with a
     short reason. Do not omit low-severity findings silently. -->

---

- [ ] Written new — anything carried from the reference or a third party is named as
      carried, with its source and a licence that permits use here
- [ ] Legacy system understood before its pieces were rebuilt
- [ ] Engineering findings were fixed, declined, or decided; no ordinary engineering
      questions or TODOs were handed to the reviewer
- [ ] Checked against GOALS.md and GOVERNANCE.md
- [ ] If this adds stage code, it includes an executable import-boundary test
- [ ] No status, dates, pod IDs or hashes added to a rules document
- [ ] The cleanroom is empty — or this PR is still under review and says why it is loaded
- [ ] CI green
