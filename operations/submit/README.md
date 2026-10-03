# submit

The import door: a local folder in, a checksummed and sealed manifest out.

`submit.py` walks a folder through `inventory.py`, hashes every regular file, and
writes one atomic, self-hashed submission manifest. Both the folder and manifest
must be under an approved storage root, and neither its manifest nor a private
refusal report can sit inside the folder it inventories. It does not decode, sniff
or judge image content — that is admission,
and admission belongs to `pipeline/1_exemplar/door.py` and its one format policy.
It does not transfer anything to a pod either: `operations/pod/transfer.py` uploads
the files a sealed manifest names, checksummed and resumable.

## What lives here

- `gate.py` — the data-handling gate as machinery. Loads the policy and enforces
  that real material stays inside the storage roots it names. The pipeline door
  imports this on its own admission loop; nothing here imports the pipeline, so
  the dependency between the two trees points one way.
- `inventory.py` — reading a submitted folder without following anything out of it.
  Every open is anchored to a directory descriptor and refuses to follow a link.
- `submit.py` — the folder-to-manifest tool. It writes a private refusal report
  that preserves source filenames and reasons when inventory refuses a source;
  a distinct retry receives a content-addressed sibling report rather than losing
  the later alarm to immutable evidence already at the ordinary path. There is no
  routine deletion command: this tool has no sealed end-of-run authority. The
  Exemplar door writes the corresponding private report for decoder, digest, and
  unreadable-after-transfer alarms. Byte-identical files under two names refuse the
  whole run at the door, after a private duplicate report names them.

## The storage-root check is mechanical; the approval-record requirement is cut

**No per-run approval record.** A submission needs no signed-off data-gate approval
record, because this material never goes near git: it runs on a GPU host and
`workbench/` is gitignored. The commit hooks refuse
credentials and oversized payloads before anything is committed; CI's full-history scan
runs only after a push, so it detects a leak but cannot prevent one. Keeping real
transcriptions and images out of the repository is the rule CONTRIBUTING.md sets for
what enters the repository, not something a scan guarantees.

What remains, and is unaffected: a folder handed to this tool is never a fixture, by
construction — it never goes near `load_fixture` — and both the submitted folder and
the manifest must sit inside a storage root `config/data_handling_policy.json`
names, checked before a single byte is hashed.

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
  refusal-report artifact. Byte-identical files under two names are named in a
  separate private duplicate report, and the door then refuses the whole run.
- Records retain original filenames, digests, byte counts, and fanned page/frame
  indices. An export retains those links both in its page census and alongside
  every delivered source region. Terminals are presentation only: they report a
  count and private report location, never image bytes.
- **What produced a file decides where it goes, never the file's extension.** A rule
  keyed on a suffix is a rule anyone can walk past by renaming. Storage roots are
  chosen by the stage that wrote the file.
- **Testimonia survive per-stage cleanup.** They are pipeline records — evidence
  is never overwritten — and remain until the whole run
  reaches its sealed disposal condition; they are destroyed with that whole volume,
  not retained beyond it.
- Temporary writes are same-directory, flushed and `fsync`ed before atomic
  publication. Retain every run artifact, working copy, export, and ledger until
  the whole run is dead/broken or complete/exported. Only the lifecycle owner may
  then destroy the whole run volume; this tool intentionally has no routine
  deletion command.
- Sending real images or transcriptions to an external API is disclosure and
  needs Tyrel's approval naming the vendor and pages, recorded as an approval
  artifact. Any data-handling testing shortcut belongs in
  `workbench/standing/ALPHA_SHORTCUTS.md`; git remains absolute.

## What a cleanup drill may claim

No cleanup drill is implemented. The policy's `cleanup_drill` clause, which `gate.py`
requires every policy to carry, bounds what any future drill may claim: run on
synthetic material only, it may show that declared target paths and temporary paths
are absent, that declared logs contain no forbidden marker, and that a volume object
listing is empty where a volume exists. Where there is no volume it must report
`None` rather than an empty listing: unknown is never zero. It can never claim
forensic unrecoverability from storage media, snapshots or provider backups, which no
filesystem check can establish.
