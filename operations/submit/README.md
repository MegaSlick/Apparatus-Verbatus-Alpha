# submit

The import door: a local folder in, a checksummed and sealed manifest out.

`submit.py` walks a folder through `inventory.py`, hashes every regular file, and writes one
atomic, self-hashed submission manifest. It does not decode or judge image content (that is
admission, in `pipeline/1_exemplar/door.py`), and it transfers nothing
(`operations/pod/transfer.py` uploads the files a sealed manifest names).

| Module | What it does |
|---|---|
| `gate.py` | the data-handling gate: loads `config/data_handling_policy.json` and enforces that real material stays inside the storage roots it names. The pipeline door imports it; nothing here imports the pipeline |
| `inventory.py` | reads a submitted folder without following anything out of it: every open is anchored to a directory descriptor and refuses links |
| `submit.py` | folder to manifest; writes a private refusal report when inventory refuses a source |

## Rules

- **Storage roots are checked mechanically.** The submitted folder, its manifest and any
  refusal report must be inside a storage root the policy names (the shipped local root is
  `private/`), checked before a byte is hashed. Neither the manifest nor a report may sit
  inside the folder it inventories. A submitted folder is never a fixture.
- **No per-run approval record is required.** Real material stays out of git by
  CONTRIBUTING.md's rule; the commit hooks refuse credentials and oversized payloads, and
  CI's history scan can detect a leak after a push but not prevent one.
- **Records are immutable evidence.** The manifest and refusal reports are canonical and
  self-hashed. A later, different inventory alarm gets a content-addressed sibling report
  rather than overwriting the first. The Exemplar door writes its own private report for
  decoder, digest and unreadable-after-transfer alarms. Byte-identical files under two names
  are named in a private duplicate report, and the Door then refuses the whole run.
- **Filenames stay in the records** because they are the citation link; records keep
  filenames, digests, byte counts and fanned page or frame indices, and an export keeps
  those links. Terminals show only counts and a report location, never image bytes.
- **What produced a file decides where it goes, never its extension.** A rule keyed on a
  suffix can be walked past by renaming.
- **Nothing is deleted piecemeal.** Temporary writes are same-directory, flushed and
  `fsync`ed before atomic publication. Every run artifact, working copy, export, ledger and
  Testimonium is kept until the whole run is broken or exported; then only the lifecycle
  owner may destroy the whole run volume. This tool has no deletion command.
- **Sending real images or transcriptions to an external API is disclosure** and needs the
  project lead's approval naming the vendor and pages, recorded as an approval artifact.

## Cleanup drills

None is implemented. The policy's `cleanup_drill` clause, which `gate.py` requires, bounds
what a future drill may claim: on synthetic material only, that declared target and
temporary paths are absent, that declared logs contain no forbidden marker, and that a volume
listing is empty where a volume exists (reporting `None`, never an empty listing, where there
is none: unknown is never zero). It can never claim forensic unrecoverability from storage
media, snapshots or provider backups.
