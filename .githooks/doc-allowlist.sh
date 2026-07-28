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
#   .githooks/pre-commit       what you are about to commit. Local and fast,
#                              and skippable — `git commit --no-verify` and
#                              merge-created commits can evade it.
#   .github/workflows/ci.yml   every tracked file in the repository. Checks
#                              what the repository *contains*, not what you
#                              did, so none of those evasions help.
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
    # The session skills. Instructions to a session, so they are harness
    # documents like the agents beside them. Exactly `.claude/skills/<name>/SKILL.md`
    # and nothing else under it: a skill may bundle reference files, but each one
    # is a document this repository has to declare, and an open `*.md` here would
    # make `.claude/skills/notes/` a notes folder by another name.
    .claude/skills/*/SKILL.md)
      rest=${f#.claude/skills/}
      case "${rest%/SKILL.md}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    *) printf '%s\n' "$f"; stray=1 ;;
  esac
done

exit $stray
