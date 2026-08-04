# submit

The import door: a local folder in, a checksummed and sealed manifest out.

`submit.py` walks a folder through `inventory.py`, hashes every regular file, and
writes one atomic, self-hashed submission manifest naming the approval that admitted
the corpus. It does not decode, sniff or judge image content — that is admission,
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
- `submit.py` — the folder-to-manifest tool, and `purge()`, the cleanup drill's
  removal half.
- `cleanup.py` — the drill's verification half: declared, measurable bounds, and a
  refusal when one of them is not met.

## The gate is not optional and not inferable

A folder handed to this tool is never a fixture, by construction: it never goes near
`load_fixture`. So real input needs Tyrel's approval record naming the current
version of `config/data_handling_policy.json`, and the gate is checked before a
single byte is hashed. A missing, stale or edited approval leaves nothing written at
all.

`/out/data_handling_gate.md` is the written policy package awaiting his approval.
Until he approves it, no real image may be submitted through here, and none has
been.

## What the cleanup drill does and does not claim

It checks that declared target paths and temporary paths are absent, that declared
logs contain no forbidden marker, and that a volume object listing is empty where a
volume applies. It is never a claim of forensic unrecoverability from storage media,
snapshots or provider backups, which no filesystem check can establish
(GOVERNANCE 10). Where there is no volume, it reports `None` rather than an empty
listing: unknown is never zero.
