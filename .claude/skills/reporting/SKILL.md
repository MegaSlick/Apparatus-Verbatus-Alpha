---
name: reporting
description: How a session writes to Tyrel — point form, one question batch, no buried answers. Read at session start and kept in context for every reply.
disable-model-invocation: true
---

# Reporting

Tyrel is not a programmer. He manages and engineers this project and he often works from
his phone. **Every reply must be easy to scan and act on** — a reply he cannot scan is a
reply he cannot act on, and the work behind it is wasted.

**Read this at session start and keep it in context.** It governs every message, not the
final report.

## The shape of a bullet

A bullet is a **fragment**, about ten words. One idea. No second sentence.

> - `git config --file` evades the hooks-off refusal

Not this — a paragraph wearing a bullet:

> - **`git config --file` evades the hooks-off refusal.** The check looks at `-c` and
>   positional writes, but with `--file` the path lands first in the values list, so the
>   comparison is false. Same spelling family the check exists to close.

If a point needs three sentences, it is not a bullet. Make it a short paragraph under a
heading, and let it be the only one in the message.

## Rules

1. **Lead with the answer.** First line says what happened or what to do. Never a
   preamble, never a restatement of the question.
2. **Cap a list at five.** More than five means the list is really two topics, or you
   are thinking out loud.
3. **Bold only the fact he must act on.** Bolding a topic sentence is decoration and it
   trains him to skip bold.
4. **No filler.** No "great question", no recap of what he just said, no closing
   pleasantry, no offer of further help.
5. **Time in minutes.** "About forty minutes", never "a while" or "shortly".

## Questions

**One batch, at the end, numbered, three at most.**

- Never ask a question in the middle of a message.
- Never carry a question forward by reference — "still open from earlier" is not a
  question he can answer. Restate it in full or drop it.
- **Never stack unanswered batches across turns.** If he answered two of three, the
  third is dead unless it still blocks you; then ask it again, whole.
- Each question restates its own context in one line. He should not scroll to decode it.
- If nothing genuinely blocks you, ask nothing. Say what you are doing next instead.

A question that needs a paragraph of setup is two things: put the setup in the body as a
finding, and ask the short question at the end.

## State

Say where things stand in one short block, every turn, near the top:

> 40 commits, gate green, nothing pushed. Fable paused.

He is often away between messages. Restating state costs four words and saves him
reconstructing it.

## When to go long

He sometimes asks for an explanation, a walkthrough, or a design argument. Then prose is
correct and this page yields — but still: no preamble, no closer, and the answer in the
first line before the reasoning that supports it.

## The test

Read the message back as though you had not written it, on a phone, having last read
this conversation an hour ago. If you cannot tell in five seconds what happened and what
he is being asked, rewrite it.
