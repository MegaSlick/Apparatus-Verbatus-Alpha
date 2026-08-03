# Autoclave measurements, 2026-08-02

Dated evidence, moved here verbatim from `operations/autoclave/README.md` when its
status sections were removed. The root `README.md` says status lives in one place and
that a status claim anywhere else is wrong by construction; both tables below were
already stale when they were moved — the suite count they cite has changed twice. They
are kept because what was measured, and what was not, is worth having. Nothing here is
current status and nothing here instructs anything.

## What has been proven

Measured on the machine this was built for — Intel i9-9980HK, 32 GB — with Colima
at 6 CPUs and 12 GB.

| Claim | Result |
|---|---|
| The image builds | 1.88 GB. Python 3.13.5, pytest 9.1.1, ruff 0.15.4, git 2.47.3, Node 22, ripgrep 14.1.1 |
| Both vendors' CLIs are present | Claude Code 2.1.220 and Codex CLI 0.146.0 |
| `/src` is genuinely read-only | a write to it returns `Read-only file system`; the host tree stayed clean |
| Both briefs land without collision | the chamber's limits at `/CLAUDE.md`, the project's rules at `/work/CLAUDE.md` |
| The brief reaches a Codex agent | **probed, 2026-08-02.** Asked what it had loaded before reading its prompt, a Codex seat answered "nothing" — twice, including with the brief at `/AGENTS.md`. With it at `/work/AGENTS.md` the same probe quotes the chamber's rules back. Evidence: `workbench/raw/2026-08-02_codex-brief-probe/` |
| The repository's own suite runs inside | passed, and the count matched the host's on the day — 953 on 2026-08-01, 584 on 2026-08-02. **The measurement is the match, not the number**; a bare count here goes stale the next time a test lands |
| Work returns | a commit made inside came back as `agent/<task>`; a reader's `/out/report.md` was collected |
| Nothing merges by itself | `collect` fetches a branch and prints the two commands to read it |
| Several chambers at once | four ran together; idle cost 1–3 MB each |
| Parallel work is real | three full suites concurrently, all 953 passing, **17s wall clock** — the same as one alone |
| VM headroom | 679 MB used of 11 GB with three suites running. Far more than four would fit |

## What has still not been proven

1. **Claude Code running unattended inside.** Codex has now been driven through
   real tasks — audits and reviews, returning reports — but the Claude CLI has
   only ever reported its version here.
2. **A long task.** The longest measured run is about twenty minutes. Nothing has
   run for an hour, filled a disk, or been interrupted halfway.
3. **That the brief reaches a *Claude* agent.** The Codex half is settled — see the
   proven table — but no probe has confirmed which file a Claude agent in a chamber
   loads automatically. Assume its limits may not have reached it until one has.
4. **The masks and the vendor split under an adversarial agent.** Both are verified
   by inspection from inside a chamber and by tests over the launcher's source. No
   agent has been asked to try to defeat them.
