## What changed

<!-- Plain English. One or two sentences. No code. -->

## What it touches

<!-- Which stage, or which part of the harness. -->

## Why it is here

<!-- If this is imported code: what is it for, and what was removed from it on the way in?
     If you cannot say what every line is for, it is not ready. -->

## Import record, when applicable

<!-- Name the coherent legacy system reviewed before files were selected. For every
     admitted file: old path, new path, and what was cut. For every deferred item:
     what it was, why it stayed behind, and what would change that decision. -->

## How to undo it

<!-- One line. Revert the commit, delete the file, flip the setting back. -->

## What proves it works

<!-- Tests, a check that runs, or an honest "nothing yet — this is scaffolding". -->

---

- [ ] Read line by line, not copied
- [ ] Legacy system understood before individual files were selected
- [ ] Imported and deferred paths recorded, including reconsideration conditions
- [ ] Checked against GOALS.md and GOVERNANCE.md
- [ ] If this adds stage code, it includes an executable import-boundary test
- [ ] No status, dates, pod IDs or hashes added to a rules document
- [ ] CI green
