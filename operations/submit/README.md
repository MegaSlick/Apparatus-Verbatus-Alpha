# submit

The import door: a local folder in, a checksummed and sealed manifest out.

`submit.py` walks a folder through `inventory.py`, hashes every regular file, and
writes one atomic, self-hashed submission manifest naming the approval that admitted
the corpus. Both the folder and manifest must be under an approved storage root, and
neither its manifest nor a private refusal report can sit inside the folder it
inventories. It does not decode, sniff
or judge image content — that is admission,
and admission belongs to `pipeline/1_exemplar/door.py` and its one format policy.
It does not transfer anything to a pod either; spec 04 owns "checksummed and
resumable", and no pod exists yet.

## What lives here

- `gate.py` — the data-handling gate as machinery. Loads the policy, hashes it the
  way the rest of the tree hashes, verifies an approval record through its
  digest-checked reference, and refuses real input without a current one. The
  pipeline door imports this and enforces it on its own admission loop; nothing here
  imports the pipeline, so the dependency between the two trees points one way.
- `inventory.py` — reading a submitted folder without following anything out of it.
  Every open is anchored to a directory descriptor and refuses to follow a link.
- `submit.py` — the folder-to-manifest tool. It writes a private refusal report
  that preserves source filenames and reasons when inventory refuses a source;
  a distinct retry receives a content-addressed sibling report rather than losing
  the later alarm to immutable evidence already at the ordinary path;
  `purge()` refuses routine deletion because this tool has no sealed end-of-run
  authority. The Exemplar door writes the corresponding private report for
  decoder, digest, and unreadable-after-transfer alarms. Byte-identical sources
  are admitted as distinct filename links and recorded as a private duplicate fact,
  not a refusal.
- `cleanup.py` — the drill's verification half: declared, measurable bounds, and a
  refusal when one of them is not met.

## The gate is not optional and not inferable

A folder handed to this tool is never a fixture, by construction: it never goes near
`load_fixture`. So real input needs Tyrel's approval record naming the current
version of `config/data_handling_policy.json`, and the gate is checked before a
single byte is hashed. A missing, stale or edited approval leaves nothing written at
all.

This README is the data-handling gate package for his approval.
`config/data_handling_policy.json` is its machine-readable half and the version an
approval names. Until he approves it, no real image may be submitted through here,
and none has been.

Filenames remain in the manifest and private refusal report because they are the
citation link. The terminal reports counts and a private report location; image bytes
never appear there.

## Data-handling package: records, retention, and disclosure

- Real source folders, manifests, and private refusal reports must be inside a
  policy-approved storage root. The shipped local root is `private/`; this does
  not imply a pod or volume root.
- The manifest and private refusal reports are canonical, self-hashed records.
  A changed submission never overwrites evidence. A later distinct inventory
  alarm gets a content-addressed sibling report; the door records decoder,
  digest, and unreadable-after-transfer alarms in its own private run-tree
  refusal-report artifact. It records admitted byte-identical sources in a
  separate private duplicate report instead of treating them as alarms.
- Records retain original filenames, digests, byte counts, and fanned page/frame
  indices. An export retains those links both in its page census and alongside
  every delivered source region. Terminals are presentation only: they report a
  count and private report location, never image bytes.
- **What produced a file decides where it goes, never the file's extension.** The
  old repository ignored personal material by extension and leaked acts as `.md`
  through the gap; a rule keyed on a suffix is a rule anyone can walk past by
  renaming. Storage roots are chosen by the stage that wrote the file.
- **Testimonia survive per-stage cleanup.** They are pipeline records under
  GOVERNANCE 4 — "testimony is always retained" — and remain until the whole run
  reaches its sealed disposal condition; they are destroyed with that whole volume,
  not retained beyond it.
- Temporary writes are same-directory, flushed and `fsync`ed before atomic
  publication. Retain every run artifact, working copy, export, and ledger until
  the whole run is dead/broken or complete/exported. Only the lifecycle owner may
  then destroy the whole run volume; this tool intentionally has no routine
  deletion command.
- The synthetic cleanup drill measures only declared target paths, temporary
  paths, log markers, and a volume listing when one exists. It cannot claim
  forensic unrecoverability from media, snapshots, or provider backups.
- Sending real images or transcriptions to an external API is disclosure and
  needs Tyrel's approval naming the vendor and pages, recorded as an approval
  artifact. Any data-handling testing shortcut belongs in
  `workbench/standing/ALPHA_SHORTCUTS.md`; git remains absolute.

## What the cleanup drill does and does not claim

It checks that declared synthetic target paths and temporary paths are absent, that
declared logs contain no forbidden marker, and that a volume object listing is empty
where a volume applies. It is never a claim of forensic unrecoverability from storage
media, snapshots or provider backups, which no filesystem check can establish
(GOVERNANCE 10). Where there is no volume, it reports `None` rather than an empty
listing: unknown is never zero. Routine deletion is unavailable here: retain every
run artifact until the settled whole-run disposal condition is recorded elsewhere.
