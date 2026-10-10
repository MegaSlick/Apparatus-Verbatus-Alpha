# chairs

Every model the pipeline calls sits in a **chair**: a named role, resolved through pinned
configuration (`config/models.toml`) to an exact artifact, fetched and verified by digest, and
served behind a receipt. Repinning a chair to another revision of the same vendor's model is a
configuration change. Putting another vendor's model in a chair is not: the facts each vendor
needs (witness adapter, answer bound, decoding rule, wire flags, model-store layout) are keyed
by chair or adapter in code, and `model_store.py` names the vendor artifacts it stores.

| File | What it settles |
|---|---|
| `models.py` | the typed values: `ChairIdentity`, `AbsentChair`, the digest manifest, `VerifiedSnapshot`, `ServingDetails`, `ServingReceipt`, `ModelsConfig` |
| `config.py` | the one schema `config/models.toml` must match, and every refusal a malformed pin earns |
| `manifests.py` | building, writing, reading and verifying the per-file digest manifest |
| `model_store.py` | the host's durable model store: validation, the derived inventory, licence snapshots, carried DAI prompts, and materialization |
| `registry.py` | resolution and verification against the filesystem and Hugging Face |
| `receipts.py` | what a serving receipt must carry |
| `filesystem.py` | the bounded control-file read and the APFS name key (case and Unicode normalization folded) |
| `errors.py` | the closed refusal taxonomy, every refusal naming the chair |
| `protocol.py` | the caller-visible shape, and the contract exerciser the tests run against each implementation |

## Rules

**Nothing here substitutes.** Every refusal names the chair and the concrete difference, and
stops. No code path resolves, fetches or receipts a chair other than the one asked for. A chair
declared as an adapter of another (`adapter_of`) is refused when the roster is parsed. The closed
taxonomy (every `raise` is checked against it) and `test_chairs_no_substitution.py` keep a
fallback out: that test drives each failure through the real registry and asserts, on a call log
kept inside the registry, that no other chair was touched.

**A pin is a constant the artifact must match**, never a value the artifact supplies. A cache
holding a different revision is refused, and the pin is never updated to agree.
`digest_manifest` is the digest of the manifest artifact's exact canonical bytes
(`common/contracts/canonical.py`).

**`resolve` is pure and offline.** It reads `config/models.toml` and returns an identity, an
explicit absence or a refusal. `ensure` is the only place a fetch may happen, and only for a
`huggingface` chair; a `local-repository` chair never touches the network.

**A `ServingReceipt` is a run receipt, never a stage artifact.** It carries a timestamp and a
live endpoint, so it is written under the run root through `RunTree.write_run_receipt`,
content-addressed, and `StageContext.publish` refuses one. A stage payload carries the
receipt's digest-checked reference plus the immutable resolved identity and revision, so
repeating an identical command leaves every stage byte unchanged.

**Absence is a value.** A chair configured `state = "absent"` resolves to an `AbsentChair`, not
an exception or an omission. It stays in `run.json`'s `witness_chairs`, earns a visible `dead`
record for every act, and counts against a witness floor that does not shrink. `dead` means
unavailable before any attempt; `not-run` is a configured chair never attempted.

**Lifecycle belongs to the serving manager** (`operations/serving/`). This package produces
identity and verification and starts no process. `receipt()` accepts the serving details the
manager observed, and `refuse_recipe_start()` represents a failed start as a refusal naming the
chair.

## The model store

The durable host model store lives outside this repository. Its root holds canonical
`download_record.json`, `records/`, `hf/`, `local/`, `manifests/` and `staging/`.
`model_store.py` verifies existing bytes, and `materialize_real_roster` is the only acquisition
writer. At pod boot it receives two injected fetchers: one per Hub repository at its revision
(the network adapter is isolated in `registry.py`), and one per local-repository artifact, which
writes the whole tree (Surya's bundle, via `operations/serving/surya_detector.py`). A
local-repository artifact has no revision, so `REQUIRED_ARTIFACTS` names its pinned manifest
digest and licence file; a fetch that measures another manifest is refused and the artifact
stays `pending-fetch`.

- **Records are immutable.** Each record version is `records/<sha256>.json`;
  `download_record.json` is an atomically moved copy of the active one. A store directory is
  per **artifact** (chandra-ocr-2 fills two chairs and is stored once), and each snapshot is held
  to its artifact-keyed path, so one roster row cannot claim another's directory.
- **Present or pending.** A record entry is `present` (snapshot, manifest, pin, licence and
  carried content) or `pending-fetch` (artifact, roster origin and why its bytes are absent). A
  present artifact cannot return to pending: missing bytes stay visibly fetched-and-lost and
  fail verification. A pending entry refuses if its snapshot or manifest exists.
  `verify_store` marks the inventory `complete: false` naming every pending artifact, and
  `StoreRoleFetcher` refuses to fill a chair whose artifact is not present.
- **`required_files`.** Every present entry names the subset of its manifest that must exist
  and be nonempty: the licence, any carried DAI prompts, and at least one model payload.
- **Upgrades.** When the roster gains an artifact, `materialize_real_roster` publishes a new
  record version adding it as `pending-fetch` and fetches it, provided every entry the older
  record names still matches the roster. Every other reader refuses the older record until it is
  upgraded.
- **Partial verification.** `verify_store(root, bytes_hashed_elsewhere=...)` runs every check
  but the byte read on the named artifacts (`verify_snapshot_structure`).
  `materialize_real_roster(..., hashed_at_copy=roles)` uses it for artifacts whose cache copies
  will hash the same bytes, and for those it just fetched; its receipt's `store_bytes` records
  where each artifact was hashed. `materialize_real_roster(..., roles=...)` limits work to the
  artifacts those chairs need (`artifacts_for_roles`; `None` means all), and the receipt reports
  `selection_complete`, `store_bytes.not_verified` and `real_roster_complete`.

## The chair cache

A cache entry is per **pinned manifest digest**, at `cache_root/by-digest/<digest_manifest>`.
Roles pinned to the same digest share one copy (the Perlector and the Coniector's
`reconstructor`); the role that asked travels in the returned `VerifiedSnapshot` and every
receipt. `model_root` (local-repository chairs only, relative to `config/models.toml`) is never a
second cache; on a pod CHAIR_CACHE copies the verified store snapshot there
(`config/real-models/`, never committed). `verify_store` refuses a store snapshot used directly as
a cache entry.

- **Filling.** `StoreRoleFetcher` copies a missing snapshot into a candidate directory and
  `ChairRegistry` verifies and promotes it. The copy hashes each file as it writes it
  (`copy_and_digest`: no link followed, size checked first, a differing digest refused by file
  name), in one thread pool, largest file first. The pool's size is the usable CPUs (affinity
  mask and cgroup `cpu.max`) clamped to 2..32, or `VERBATUS_IO_WORKERS`, recorded in the receipt.
  Several digests filled at once share one `CopyPool`.
- **Repair.** Files carried over from a damaged cache are renamed into the candidate (not
  copied) and hashed again, and renamed back if the repair fails. A file that cannot be renamed
  back is kept as `.<digest>.unreturned-*`, and the refusal says how to recover.
- **Within one process**, a `ChairRegistry` remembers each snapshot it fully verified (every
  file's device, inode, size, mtime and ctime). A later `ensure` re-reads the manifest and
  descriptor and walks the tree, hashing again only if a file changed, so preflight and the
  smoke's start hash a chair once. A new process verifies from the bytes.
- **Locks and eviction.** A per-digest lock (`by-digest/.<digest>.lock`) serialises fills
  across processes. To make room the registry may remove a configured digest's unused cache and
  abandoned `.<digest>.candidate-*` and `.<digest>.prior-*` directories, least recently used
  first, never while another fill holds that digest's lock; it leaves other entries alone.
- Cache preparation and preflight never fall back to network downloads when retained bytes are
  missing or invalid.
