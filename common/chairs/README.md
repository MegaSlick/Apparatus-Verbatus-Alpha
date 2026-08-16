# chairs

Every model the pipeline ever calls sits in a **chair**: a named role, resolved
through pinned configuration (`config/models.toml`) to an exact artifact, fetched
and verified by digest, served behind a receipt. Swapping a model is a
configuration change plus at most a serving recipe — nothing here names a model
in code.

| File | What it settles |
|---|---|
| `models.py` | the typed values: `ChairIdentity`, `AbsentChair`, the digest manifest, `VerifiedSnapshot`, `ServingDetails`, `ServingReceipt`, `ModelsConfig` |
| `config.py` | the one schema `config/models.toml` must match, and every refusal a malformed pin earns |
| `manifests.py` | building, writing, reading and verifying the per-file digest manifest |
| `model_store.py` | read-only validation of the host's durable model store, its derived seven-chair inventory, licence snapshots, carried DAI prompts, and capacity record |
| `registry.py` | resolution and verification against the filesystem and Hugging Face |
| `receipts.py` | what a serving receipt must carry before it is one |
| `errors.py` | the closed refusal taxonomy — one member per door "Resolution refuses; it never substitutes" names |
| `protocol.py` | the caller-visible shape, and the contract exerciser that names the clause a broken implementation breaks |

## Four things worth knowing before you change anything here

**Nothing here substitutes.** Every refusal names the chair and the concrete
difference, and stops. No code path fetches or receipts a chair other than the one
asked for, and the one place another chair is *resolved* is `_cache_descriptor`
reading an adapter's configured `adapter_of` base — a configuration lookup, so that
an old adapter cache cannot masquerade as compatible with a repinned base. It never
fetches, serves, ranks or substitutes that base. A registry that fell back from one
chair to a close-enough one would be a picker wearing an ops hat (GOVERNANCE 3,
CLAUDE.md hard rule 8), and the closed taxonomy plus `test_chairs_no_substitution.py`
are what keep one out. That test drives all seven doors through the *real* registry
and asserts, on a call log kept *inside* the registry rather than in front of it,
that no other configured chair was resolved, fetched or receipted while each refusal
was handled.

**A pin is a constant the artifact must match.** Never a value the artifact
supplies (harvest #43). A cache that holds a different revision than the pin is
refused rather than believed; the pin is never quietly updated to agree with
whatever turned up. `digest_manifest` is the digest of the *manifest artifact's
exact canonical bytes*, not of a structure that happens to parse the same way,
and the artifact goes through `common/contracts/canonical.py` so a chair manifest
and a run tree can never drift on what "canonical" means.

**`resolve` is pure and offline.** It reads `config/models.toml` and returns an
identity, an explicit absence, or a refusal — no network, no filesystem walk
beyond the one file. `ensure` is the only place a fetch may happen, and only for
a `huggingface` chair; a `local-repository` chair never touches the network even
there. `huggingface_hub` is reached through one function-scoped `importlib` call,
so the whole package imports, parses and tests with the dependency absent.

**A `ServingReceipt` is a run receipt, never a stage artifact.** It carries a
timestamp and a live endpoint — honestly non-deterministic — so it is written
under the run root through `RunTree.write_run_receipt`, content-addressed, and
`StageContext.publish` refuses one outright. A stage payload carries the
receipt's digest-checked reference plus the immutable resolved identity and
revision, never the timestamp or the endpoint. That is what keeps GOVERNANCE 6's
provenance travelling with every record without breaking spec 01's guarantee that
repeating an identical command leaves every byte unchanged.

## Absence is a value, not a gap

A chair configured `state = "absent"` resolves to an `AbsentChair` — not an
exception, and not a silent omission from the roster. It stays in `run.json`'s
`witness_chairs`, it earns a visible `dead` record for every act, and it is one
fewer configured witness against a floor that does not shrink to match. `dead`
means unavailable before any attempt reached the region; `not-run` remains the
separate record for a configured chair that was never attempted. A run short a
witness is therefore visibly short one, all the way into the export.

## What this system does not own

Lifecycle and health belong to the serving manager (spec 04). This package
produces identity and verification; it does not start a process. `receipt()`
accepts the serving details the serving manager observed, and
`refuse_recipe_start()` is how a failed start is represented — as a refusal
naming the chair, never as a second route under the same role name. Both are
integration doors for spec 04's manager; neither chooses how a stage obtains
its serving details.

The durable host model store is intentionally outside this repository. Its
caller-supplied root contains canonical `download_record.json`, `hf/`, `local/`,
`manifests/`, and `staging/`; `model_store.py` only verifies existing bytes and
never fetches. `model_root` in `config/models.toml` remains local-repository
only and relative to that file.
