# Specialists

Specialist agents and guides kept on the sideline. Claude Code never loads this folder by
itself, so nothing here costs context until someone reaches for it on purpose.

**How to use one (two deliberate steps):**

1. When the lead asks about specialists, or a task clearly calls for one, read this sheet.
2. Pick one and read its file. Then either follow it yourself, or start a subagent
   (general-purpose) with the file's text as its instructions plus the task. Say which
   specialist you used.

Each file starts with notes on how it applies here; this project's rules win over the
original text. To add one: put its file here with the same header (source, commit,
licence, project notes) and add a row below. Read anything before adding it.

## Useful now

| Specialist | File | Use it for |
|---|---|---|
| Silent-failure hunter | [silent-failure-hunter.md](silent-failure-hunter.md) | Reviewing a change for errors swallowed, gaps hidden by defaults, or items skipped without being counted: anywhere an act could be lost silently |
| Literature review | [literature-review.md](literature-review.md) | Surveying published work before a big choice (historical handwriting recognition, fine-tuning readers, accuracy measures) |

## For training and refining models (later)

| Specialist | File | Use it for |
|---|---|---|
| ML reviewer | [ml-reviewer.md](ml-reviewer.md) | Reviewing training or evaluation code: training and test pages kept apart, repeatable runs, gates declared before results |
| PyTorch error fixer | [pytorch-error-fixer.md](pytorch-error-fixer.md) | Diagnosing crashes while training or running a model: memory, device, shapes, data loading |
| PyTorch patterns | [pytorch-patterns.md](pytorch-patterns.md) | Background on clean, reproducible training code |

## Pointers (no file)

- **Hugging Face fine-tuning libraries** (Transformers, PEFT, TRL): the likely route for
  fine-tuning our readers. Look up current documentation when the work starts rather than
  relying on memory.

## Sources

- [ECC](https://github.com/affaan-m/ECC), MIT licence (`LICENSE-ECC.txt`). Reviewed and
  rejected for this project: its Python reviewer (insists on running for every change),
  testing skills (coverage targets, against lean tests), vendor-specific training and
  serving skills, and its regex-first text extraction (fills gaps by guessing).
