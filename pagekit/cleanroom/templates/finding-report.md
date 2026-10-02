# Finding report template

A reading-side agent fills this in and returns it as its whole answer. It describes
behaviour and published ideas in plain words. The host saves it unread, runs
`python3 -m pagekit.cleanroom.check_report REPORT.md`, and opens it only if it passes.

**Never put in a report:**

- code in any language, pseudocode that follows a function step by step, or anything
  set in backticks or a code block;
- names of functions, classes, variables, files or folders from the ScanTailor or
  ScanTailor Advanced source, or any path or file name at all;
- line numbers, or a description of how their code is laid out (which part calls
  which, in what order, how it is split into pieces);
- numeric constants read from their code. Say what a setting depends on ("about the
  height of a line of text"), never the number they use;
- links, except a `https://doi.org/` link in a citation;
- "copy this", "do it the way they do", or anything else that tells the build side to
  reproduce their work.

Write numbers of text lines on a page in words ("the third line"), since "line" with a
digit is refused as a line-number reference. Every rule here is checked by a script,
and the script catches accidents, not every spelling: the rules bind even where the
script would not notice.

Copy everything below this line, replace each bracketed part, and keep the headings
exactly as written.

---

# Finding: [one-line name of the page situation]

Reader brief sha256: [the 64-character digest given at the top of your brief]

## Situation

[The page condition in plain words, for example a margin note written sideways near
the fold, or a page photographed at a slight angle.]

## Pagekit's observed behaviour

[What pagekit did with it, taken only from the pagekit outputs the host gave you for
its synthetic pages. Say what was right and what was missed.]

## General technique

[A method that handles the situation, named as a published idea or textbook method,
in your own words.]
Source: [author, title, where published and year; or "general knowledge" if there is
no single source]

## Settings in general terms

[Which settings matter and what each should depend on, in general terms, without
numbers taken from any program's code.]
