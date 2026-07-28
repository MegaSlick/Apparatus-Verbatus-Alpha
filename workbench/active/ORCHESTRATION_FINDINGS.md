# Orchestration findings — Codex/GPT side, 2026-07-27

Answers the handoff's unverified items 4 and 5. Everything here was run, not inferred;
where something is a model's claim rather than an observation it says so.

## Verified by execution

**`codex exec -m <model>` works and the model ids are real.** `gpt-5.6-sol`,
`gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.3-codex-spark` all returned clean responses.
codex-cli 0.145.0.

**`-c model_reasoning_effort=<level>` reaches the API.** Proven by passing `banana`,
which the server rejected by name with the enum: `none`, `minimal`, `low`, `medium`,
`high`, `xhigh`, `max`. The flag is not swallowed client-side.

**`--ignore-user-config` gives a fully declared call.** It skips
`~/.codex/config.toml` entirely while still using its auth. Confirmed: a call with
`--ignore-user-config -m <model> -c model_reasoning_effort=<level> -s read-only`
inherits nothing from Tyrel's desktop settings. This is the mechanism for pinning
GPT seats.

> **CONTESTED, 2026-07-28.** A later run asked Sol to self-orchestrate and report its
> delegation metrics. It reported 4 delegates spawned and completed — and its transcript
> contains no collaboration tool call at all, only 24 of its own shell calls covering every
> area it claimed to have delegated. The delegation test below cannot distinguish real
> delegation from a narrated one, because it verified the *answers* rather than the
> *mechanism*. Treat everything in this section as unverified until re-tested in a form
> that leaves an externally observable side effect.
> Full write-up: `workbench/raw/2026-07-28_gpt-experiments/DELEGATION_FINDING.md`.

**Sol self-orchestrates.** Its real tool list (asked twice, two sessions, identical
both times) contains a full collaboration suite:

    collaboration.spawn_agent      collaboration.wait_agent
    collaboration.send_message     collaboration.interrupt_agent
    collaboration.list_agents      collaboration.followup_task

Plus `tool_search`, `exec`/`exec_command`/`write_stdin`, `apply_patch`, `update_plan`,
`request_user_input`, `view_image`, MCP resource tools, `web.run`, `image_gen.imagegen`.

**End-to-end delegation test passed.** One `codex exec` call: Sol at `high` spawned two
concurrent Terra delegates, each counting lines in a different canonical document, and
reported back. Results `103` and `39` — both independently verified against
`wc -l GOVERNANCE.md GOALS.md`. Correct work, not a plausible-looking answer.

**Delegation is NOT gated on `ultra`.** The tool list at `high` and at `ultra` is
byte-identical. `ultra` does not grant the collaboration tools — they are present at
every effort. The catalog's phrase "maximum reasoning with automatic task delegation"
therefore describes how readily Sol *reaches for* delegation, not what it can do.
Consequence: an orchestrating Sol can run at `high` and cost far less than `ultra`.

## The model roster, from `~/.codex/models_cache.json`

The catalog is Codex's own, refreshed from OpenAI. Efforts are per-model.

| slug | role | default effort | efforts | notes |
|---|---|---|---|---|
| `gpt-5.6-sol` | frontier agentic coding | **low** | low→max, ultra | OpenAI's own default is low |
| `gpt-5.6-terra` | balanced, everyday | medium | low→max, ultra | |
| `gpt-5.6-luna` | fast and affordable | medium | low→max | no ultra listed |
| `gpt-5.3-codex-spark` | ultra-fast coding | high | low→xhigh | flagged `supported_in_api: false` but **works** via `codex exec` |
| `gpt-5.5` | previous frontier | medium | low→xhigh | |
| `gpt-5.4`, `gpt-5.4-mini` | — | medium | low→xhigh | deprecating into Terra / Luna |
| `codex-auto-review` | Codex's approval-review model | medium | low→xhigh | hidden |

Sol's catalog note, verbatim in spirit: highly capable at lower reasoning efforts, start
lower and turn it up. **The xhigh in `~/.codex/config.toml` is Tyrel's global desktop
setting, not an OpenAI default**, and it is two tiers above what OpenAI suggests.

## The sandbox does not confine where you point it

This one was shipped wrong first, caught by a `consult` read, and then measured three
ways. It matters more than anything else here.

**`-C <dir>` does not bound a `workspace-write` sandbox.** The writable root resolves to
an *ancestor* of the directory you point at.

| probe | seat rooted at | attempted write | result |
|---|---|---|---|
| inside a git repo | `sbx/tray` | `sbx/OUT.txt` (parent) | **written** — boundary is the git repo root |
| no git repo | `$TMP/tray` | `$TMP/OUT.txt` (parent) | **written** — boundary is still an ancestor |
| from outside | `$TMP/tray` | absolute path into `verbatus_alpha/` | **refused** — `operation not permitted` |

Every result was checked on disk, not taken from the model's report.

**Consequence.** A drafting seat rooted at `autoclave/` gets write access to the entire
repository, because the repository is the enclosing git root. That is the reverse of the
quarantine, and the first version of `seats.conf` claimed the opposite in a comment.

**What actually holds.** The outside boundary. A seat rooted outside the tree cannot
reach in. So the drafting seat runs in a fresh temporary directory outside the
repository (`workroot TMPTRAY`), and the session reads its output and carries the file
into `autoclave/` itself. This is also what the quarantine has always said should
happen: no byte enters the tree that a reviewed session did not place there.

`seat.sh` now refuses any `workspace-write` seat rooted anywhere inside the repository,
and `test_seat.py` asserts it — including the `autoclave` case specifically, since that
was the wrong answer the first time.

## Claims not yet verified

- **The four-agent ceiling.** Sol reported `MAX_AGENTS=4` (itself plus three delegates),
  consistently across sessions, but nothing has actually hit the limit. Treat as likely,
  not proven.
- **Per-delegate model pinning.** Sol reported `MODEL_CHOICE_HONORED=unknown` — it can
  *request* Terra for a delegate but cannot observe what the delegate actually ran on.
  So a GPT-side model pin is unverifiable from inside the loop. This is a real asymmetry
  with the Claude roster, where the model is pinned in a file a reviewer can read.
- **The CLI does not validate effort against the per-model list.** `gpt-5.6-luna` accepted
  `ultra` even though the catalog does not list ultra for Luna. A typo'd or unsupported
  effort will not reliably fail loudly — another argument for pinning seats in a tracked
  file rather than typing them per call.
- **GPT pricing.** RUN_PLAN §5 carries Sol $5/$30, Terra $2.50/$15, Luna $1/$6 from a
  previous session's memory. Nothing local confirms these. Unverified.

## Traps that cost time this session

- **`codex exec` hangs forever if stdin never reaches EOF.** Building the prompt with a
  shell heredoc left two calls blocked on `Reading additional input from stdin...` with
  zero model tokens spent — they looked like slow reasoning and were not. **Always append
  `</dev/null`**, and wrap every call in `timeout <n>` so a stall self-bounds instead of
  burning a session.
- Bare `codex models` is not a command; the catalog file is the source of truth.

## What this changes

RUN_PLAN §5's preferred cascade — "Sol orchestrating Terra builders with Sol inspectors
in a mini-loop, reporting to the Opus session" — is now **known-possible**, not a
fallback-pending-verification. The per-call fallback (session drives each `codex exec`
itself) remains available and is the right choice when a run needs the model actually
pinned, since the in-loop pin cannot be verified.

## Cost

The whole investigation — roster discovery, five capability probes, two introspections,
one live delegation test — came to roughly 80k tokens on Tyrel's OpenAI budget.
