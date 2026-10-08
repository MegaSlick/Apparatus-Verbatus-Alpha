# chairs

Every model the pipeline ever calls sits in a **chair**: a named role, resolved
through pinned configuration (`config/models.toml`) to an exact artifact, fetched
and verified by digest, served behind a receipt. Repinning a chair's model to
another revision of the same vendor is a configuration change. Putting another
vendor's model in a chair is not: the facts each vendor needs (its witness
adapter, answer bound, decoding rule, wire flags and model-store layout) are
keyed by chair or adapter in code, and `model_store.py` names the vendor
artifacts it stores.

| File | What it settles |
|---|---|
| `models.py` | the typed values: `ChairIdentity`, `AbsentChair`, the digest manifest, `VerifiedSnapshot`, `ServingDetails`, `ServingReceipt`, `ModelsConfig` |
| `config.py` | the one schema `config/models.toml` must match, and every refusal a malformed pin earns |
| `manifests.py` | building, writing, reading and verifying the per-file digest manifest |
| `model_store.py` | validation of the host's durable model store, its derived seven-chair inventory, licence snapshots, and carried DAI prompts; versions canonical download records immutably and publishes promoted manifests once, never overwriting evidence |
| `registry.py` | resolution and verification against the filesystem and Hugging Face |
| `receipts.py` | what a serving receipt must carry before it is one |
| `filesystem.py` | the bounded control-file read and the APFS name key (case and Unicode normalization folded) the other modules share |
| `errors.py` | the closed refusal taxonomy: every refusal the package raises, each naming the chair |
| `protocol.py` | the caller-visible shape, and the contract exerciser the tests run against each implementation |

## Four things worth knowing before you change anything here

**Nothing here substitutes.** Every refusal names the chair and the concrete
difference, and stops. No code path resolves, fetches or receipts a chair other
than the one asked for. A chair declared as an adapter of another (`adapter_of`)
is refused when the roster is parsed, so no base can ever answer in its place.
A registry that fell back from one chair to a close-enough one would be a picker
wearing an ops hat, and the closed taxonomy (every `raise` in the package is
checked against it) plus `test_chairs_no_substitution.py` are what keep one out.
That test drives each way a chair can fail to be served through the *real*
registry and asserts, on a call log kept *inside* the registry rather than in
front of it, that no other configured chair was resolved, fetched or receipted
while each refusal was handled.

**A pin is a constant the artifact must match.** Never a value the artifact
supplies. A cache that holds a different revision than the pin is
refused rather than believed; the pin is never quietly updated to agree with
whatever turned up. `digest_manifest` is the digest of the *manifest artifact's
exact canonical bytes*, not of a structure that happens to parse the same way,
and the artifact goes through `common/contracts/canonical.py` so a chair manifest
and a run tree can never drift on what "canonical" means.

**`resolve` is pure and offline.** It reads `config/models.toml` and returns an
identity, an explicit absence, or a refusal — no network, no filesystem walk
beyond the one file. `ensure` is the only place a fetch may happen, and only for
a `huggingface` chair; a `local-repository` chair never touches the network even
there. `huggingface_hub` is a declared dependency used by the production fetcher.

**A `ServingReceipt` is a run receipt, never a stage artifact.** It carries a
timestamp and a live endpoint — honestly non-deterministic — so it is written
under the run root through `RunTree.write_run_receipt`, content-addressed, and
`StageContext.publish` refuses one outright. A stage payload carries the
receipt's digest-checked reference plus the immutable resolved identity and
revision, never the timestamp or the endpoint. That is what keeps provenance
travelling with every record without breaking the guarantee that repeating an
identical command leaves every stage byte unchanged.

## Absence is a value, not a gap

A chair configured `state = "absent"` resolves to an `AbsentChair` — not an
exception, and not a silent omission from the roster. It stays in `run.json`'s
`witness_chairs`, it earns a visible `dead` record for every act, and it is one
fewer configured witness against a floor that does not shrink to match. `dead`
means unavailable before any attempt reached the region; `not-run` remains the
separate record for a configured chair that was never attempted. A run short a
witness is therefore visibly short one, all the way into the export.

## What this system does not own

Lifecycle and health belong to the serving manager (`operations/serving/`). This package
produces identity and verification; it does not start a process. `receipt()`
accepts the serving details the serving manager observed, and
`refuse_recipe_start()` is how a failed start is represented — as a refusal
naming the chair, never as a second route under the same role name. Both are
integration doors for that manager; neither chooses how a stage obtains
its serving details.

The durable host model store is intentionally outside this repository. Its
caller-supplied root contains canonical `download_record.json`, `records/`,
`hf/`, `local/`, `manifests/`, and `staging/`; `model_store.py` verifies existing
bytes, and its explicit `materialize_real_roster` workflow is the only
acquisition writer. It receives two injected fetchers at pod boot: one for each
Hub repository at its revision, whose network adapter is isolated in
`registry.py`, and one for each local-repository artifact, which writes the
whole tree (Surya's bundle, through `operations/serving/surya_detector.py`).
A local-repository artifact has no revision to fetch at, so
`REQUIRED_ARTIFACTS` names its pinned manifest digest and licence file; a fetch
that measures any other manifest is refused before anything is published, and
the artifact stays `pending-fetch`.
Each canonical record version is immutable at
`records/<sha256>.json`; `download_record.json` is an atomically moved copy
to the active version, so a pending artifact can later become present without
erasing its earlier state. A present artifact cannot return to pending: missing
bytes after acquisition remain visibly fetched-and-lost and fail verification.
`model_root` in a roster remains local-repository only and relative to that
file. On a pod the CHAIR_CACHE step copies each local-repository chair's verified
store snapshot there (`config/real-models/` for the real roster, never committed),
checked against the roster's manifest before it replaces anything.

The store is shared with the future pod, and the two sides key their directories
differently: a store directory is per **artifact** (chandra-ocr-2 fills two
chairs at one revision and is stored once), a `cache_root` entry is per **pinned
manifest digest**, under `cache_root/by-digest/<digest_manifest>`.
Each present snapshot and manifest is held to its artifact-keyed canonical path,
so one roster row cannot claim another artifact's verified directory and pin.
`model_root` is the local-repository half, resolved relative to
`config/models.toml` and never a second cache. `verify_store` refuses a store
snapshot used as a cache entry directly, naming that cause rather than reporting
an extra file.

Each configured Hugging Face role is bound to its exact repository, revision and
manifest when its stage fills its cache. Roles pinned to the same manifest digest
share one cache copy: the Perlector and the Coniector's `reconstructor` read one
copy of their model. The cache's descriptor records only the digest it holds; the
role that asked travels in the returned `VerifiedSnapshot` and in every receipt.
`StoreRoleFetcher` copies a missing snapshot into a candidate directory,
`ChairRegistry` verifies it and promotes it under that digest. The copy hashes
each file as it writes it (`copy_and_digest`: the source opened without following
a link, its size checked before a byte is read, a differing digest refused by file
name), with all files in one thread pool, largest first. The pool's size is the
process's usable CPUs (affinity mask and cgroup `cpu.max`) clamped to 2..32, or
`VERBATUS_IO_WORKERS`; the count and its source are recorded in the verification
receipt. A caller filling several digests at once gives `StoreRoleFetcher` one
`CopyPool`, so every file of every snapshot waits in one queue, largest first, and
no worker idles behind one snapshot's single large file. The returned ledger lets
the registry check the copied tree's structure (missing and extra files, sizes,
links) without reading those bytes again; files carried over from a damaged cache
are hashed again. They are carried by renaming them into the candidate on the
cache's own filesystem, not by copying, and are renamed back if the repair fails,
so an incomplete cache is left as it was.

Within one process, a `ChairRegistry` remembers each snapshot it fully verified,
by manifest digest and root, with every file's device, inode, size, mtime and
ctime at that moment. A later `ensure` of the same snapshot in that process
re-reads the manifest and the cache descriptor and walks the tree, and reads the
bytes again only if any file was added, removed, rewritten or replaced. So
preflight's verification followed by the smoke's `ServingManager.start` hashes a
chair once. A new process remembers nothing and verifies from the bytes. A per-digest lock
(`by-digest/.<digest>.lock`) serialises concurrent fills of one digest, across
processes. When making room, the registry may remove a configured digest's unused
cache and abandoned `.<digest>.candidate-*` and `.<digest>.prior-*` directories,
least recently used first and never while another fill holds that digest's lock;
it leaves all other entries under `cache_root` alone. Cache preparation and
preflight do not fall back to network downloads when retained bytes are missing
or invalid.

A store is materialized one snapshot at a time, so a record entry is either
`present` — snapshot, manifest, pin, licence and carried content — or
`pending-fetch`, which names the artifact, its roster origin, and the reason its
bytes are not there yet. `verify_store` proves what exists and marks the derived
inventory `complete: false` with every pending artifact named, and
`StoreRoleFetcher` refuses to fill a chair whose artifact is not present. A half-fetched store is therefore recordable and visibly partial rather
than unrepresentable. A pending entry also refuses if its
artifact-keyed snapshot or manifest exists, so replaying an older pending record
cannot relabel acquired or lost bytes as “not yet fetched.”

`verify_store(root, bytes_hashed_elsewhere=...)` runs every check on the named
artifacts except reading their bytes (`verify_snapshot_structure`). At pod boot
`materialize_real_roster(..., hashed_at_copy=roles)` passes the artifacts of the
chairs whose cache copies will hash those bytes against the same pinned manifest,
and the artifacts it fetched and measured in the same call; its receipt's
`store_bytes` records where each present artifact's bytes were hashed.
`materialize_real_roster(..., roles=...)` limits fetching and verification to the
artifacts those chairs need (`artifacts_for_roles`); `None` means every chair. Its
receipt then reports `selection_complete` for those artifacts, lists the present
artifacts it left unchecked under `store_bytes.not_verified`, and reports
`real_roster_complete` only when nothing was left unchecked.

When the roster gains an artifact, a store written before it is upgraded rather
than refused: `materialize_real_roster` publishes a new record version that adds
each newly required artifact as `pending-fetch`, then fetches it. It does so
only when every entry the older record names still matches the roster; any
other shape is refused, and every reader other than the materializer still
refuses the older record until it is upgraded.

Every `present` entry also names `required_files`. The digest manifest remains
the exact allow-list used when a chair cache fills, while `required_files` is
the non-negotiable subset that must be present and nonempty. It includes the
licence, any carried DAI prompts, and at least one model payload; a smaller,
self-consistent manifest containing only configuration metadata is refused.
