#!/bin/sh
# The one list of documentation files this repository is allowed to track.
#
# Reads paths on stdin, one per line — any paths, not only markdown ones. This
# script decides what counts as documentation, so that "what is a document" is
# settled here and not separately by each caller.
#
# Prints back any path that is documentation and not allowed.
#   exit 0   every path allowed
#   exit 1   at least one stray, listed on stdout
#   other    the check itself broke. Callers must treat this as a failure and
#            never as a pass — see GOVERNANCE.md 10.
#
# Two callers share this file so the rule cannot drift:
#
#   .githooks/pre-commit           what you are about to commit. Local and
#                                  fast, and skippable — `git commit
#                                  --no-verify` and merge-created commits can
#                                  evade it.
#   .githooks/check-documents.sh   what the repository currently holds: every
#                                  tracked file *and* every unignored
#                                  untracked one, so an uncommitted stray is
#                                  visible too. Checks what the working tree
#                                  *contains*, not what you did, so none of
#                                  those evasions help. CI runs this script on
#                                  every pull request, and check-all.sh runs
#                                  the same script locally.
#
# Nothing else calls it. check-all.sh and install.sh name this file in their
# lint and install lists without ever running it, which is not a call.
#
# Which of the two actually stops a merge is settled by GitHub, not here, and
# README.md is where that is recorded. Do not restate it in this file: a second
# copy is a copy that goes stale, and this one already did once.

stray=0

while IFS= read -r f; do
  [ -n "$f" ] || continue

  # Is it documentation? Match the usual markup formats and note-like text
  # filenames. Ordinary text assets remain possible, but NOTES.txt cannot
  # bypass the same rule that correctly catches NOTES.md.
  if ! lower=$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]'); then
    echo "could not normalize documentation path; refusing unchecked input" >&2
    exit 2
  fi
  case "$lower" in
    *.md|*.markdown|*.mdown|*.mdwn|*.mkd|*.mkdn|*.rst|*.rest|*.adoc|*.asciidoc) ;;
    *.txt)
      name=${lower##*/}
      case "$name" in
        notes.txt|notes-*.txt|notes_*.txt|todo.txt|todo-*.txt|todo_*.txt|\
        plan.txt|plan-*.txt|plan_*.txt|progress.txt|progress-*.txt|progress_*.txt|\
        status.txt|status-*.txt|status_*.txt|session.txt|session-*.txt|session_*.txt|\
        handoff.txt|handoff-*.txt|handoff_*.txt|next-session*.txt|next_session*.txt|\
        scratch.txt|scratch-*.txt|scratch_*.txt) ;;
        *) continue ;;
      esac ;;
    *) continue ;;
  esac

  # Is it allowed? Matched against the real path, case-sensitively: the
  # canonical documents have exact names, and `Readme.md` is not one of them.
  case "$f" in
    # The canonical documents. The binding project specification; the harness
    # instruction files admitted below govern how sessions and agents work.
    README.md|GOALS.md|GOVERNANCE.md|ARCHITECTURE.md|GLOSSARY.md|CLAUDE.md|DATA_CONTRACT.md) ;;
    # **The bounded drawers come first, because `case` takes the first match.** The
    # generic rule below accepts `*/README.md` at any depth — `*` matches `/` in a
    # shell case pattern — so it admitted `operations/autoclave/briefs/nested/README.md`
    # before the one-level-deep rules further down could refuse it, and the same for
    # `HANDOFF.md`. Those drawers are bounded on purpose: an unbounded one becomes a
    # notes folder by another name. Found by CodeRabbit on pull request 15.
    operations/autoclave/briefs/*/*) printf '%s\n' "$f"; stray=1 ;;
    .claude/agents/*/*) printf '%s\n' "$f"; stray=1 ;;
    .github/*/*) printf '%s\n' "$f"; stray=1 ;;
    # Every directory may explain itself; every stage declares what it writes.
    # Including at the root, which CLAUDE.md allows and a bare */ pattern misses.
    HANDOFF.md|*/README.md|*/HANDOFF.md) ;;
    # Dated evidence, one level deep. `history/undated-note.md` is still a
    # note; putting it in the evidence drawer does not make it evidence.
    history/*)
      item=${f#history/}
      case "$item" in
        */*) printf '%s\n' "$f"; stray=1 ;;
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].md) ;;
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_*.md) ;;
        *) printf '%s\n' "$f"; stray=1 ;;
      esac ;;
    # The harness's own documents. One level only: `*` matches `/` in a shell
    # case pattern, so `.github/*.md` would otherwise permit a notes folder
    # nested to any depth underneath it.
    .github/*.md)
      case "${f#.github/}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    .claude/agents/*.md)
      case "${f#.claude/agents/}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    # The standing brief a dispatched agent reads inside the autoclave container.
    # A harness document by the same logic as the agent files above — it instructs
    # an agent — and admitted by exact path rather than a glob so the directory
    # does not become a notes drawer. It is deliberately NOT named `CLAUDE.md`:
    # under that name a session working in this directory *on the host* would read
    # it and believe it was inside a container. The Dockerfile renames it on the
    # way into the image, which is the only place it is true.
    operations/autoclave/agent-brief.md) ;;
    # The standing briefs a dispatched agent is given for its role — what
    # `.claude/agents/worker.md` and its two siblings held before writing work
    # moved off this machine. Harness documents by the same logic as the agent
    # files above, and one level deep only, so the drawer cannot grow notes.
    operations/autoclave/briefs/*.md)
      case "${f#operations/autoclave/briefs/}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    # The session skills. Instructions to a session, so they are harness
    # documents like the agents beside them. Exactly `.claude/skills/<name>/SKILL.md`
    # and nothing else under it: a skill may bundle reference files, but each one
    # is a document this repository has to declare, and an open `*.md` here would
    # make `.claude/skills/notes/` a notes folder by another name.
    .claude/skills/*/SKILL.md)
      rest=${f#.claude/skills/}
      case "${rest%/SKILL.md}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    # workbench/ has no exception. `active/` had one briefly, so a reviewer could
    # read the run plan beside the code; that answered its question and the drawer
    # is ignored again. These are task files that `session-end` moves to archive/
    # when the work closes, so tracking them meant tracking things whose purpose
    # is to move.
    *) printf '%s\n' "$f"; stray=1 ;;
  esac
done

exit $stray
