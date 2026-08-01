## What changed

<!-- Plain English. One or two sentences. No code. -->

## What it touches

<!-- Which stage, or which part of the harness. -->

## Why it is here

<!-- If this rebuilds old behaviour: what is it for, and what was deliberately left
     behind? If you cannot say what every line is for, it is not ready. -->

## Rebuild record, when applicable

<!-- Name the coherent legacy system read before this was written. For every rebuilt
     piece: the old path read, the new path written, and what was left behind. For every
     deferred item: what it was, why it stayed, and what would change that decision. -->

## How to undo it

<!-- One line. Revert the commit, delete the file, flip the setting back. -->

## What proves it works

<!-- Tests, a check that runs, or an honest "nothing yet — this is scaffolding". -->

---

- [ ] Written new — no old byte crossed; the reference was read line by line
- [ ] Legacy system understood before its pieces were rebuilt
- [ ] Rebuilt and deferred paths recorded, including reconsideration conditions
- [ ] Checked against GOALS.md and GOVERNANCE.md
- [ ] If this adds stage code, it includes an executable import-boundary test
- [ ] No status, dates, pod IDs or hashes added to a rules document
- [ ] The cleanroom is empty — or this PR is still under review and says why it is loaded
- [ ] CI green
