#!/bin/sh
# The one list of markdown files this repository is allowed to track.
#
# Reads paths on stdin, one per line — any paths, not only markdown ones. This
# script decides what counts as markdown, so that "what is a document" is
# settled here and not separately by each caller.
#
# Prints back any path that is markdown and not allowed.
#   exit 0   every path allowed
#   exit 1   at least one stray, listed on stdout
#   other    the check itself broke. Callers must treat this as a failure and
#            never as a pass — see GOVERNANCE.md 10.
#
# Two callers share this file so the rule cannot drift:
#
#   .githooks/pre-commit       what you are about to commit. Local and fast,
#                              and skippable — `git commit --no-verify`, a
#                              rename, or a merge all evade it.
#   .github/workflows/ci.yml   every tracked file in the repository. Checks
#                              what the repository *contains*, not what you
#                              did, so none of those evasions help.
#
# What CI cannot do is *stop* a merge. This repository is private on a free
# plan, which has no branch protection and so no required checks. A red run is
# loud, and it is on the record — but the merge button still works. That is an
# alarm, not a lock, and it is worth knowing which one you have.

stray=0

while IFS= read -r f; do
  [ -n "$f" ] || continue

  # Is it markdown? Case-insensitively, and by any of the usual spellings —
  # GitHub renders NOTES.MD and session.markdown exactly like a .md file, so a
  # check that only knows lowercase .md is a check with a door in it.
  case "$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]')" in
    *.md|*.markdown|*.mdown|*.mdwn|*.mkd|*.mkdn) ;;
    *) continue ;;
  esac

  # Is it allowed? Matched against the real path, case-sensitively: the
  # canonical documents have exact names, and `Readme.md` is not one of them.
  case "$f" in
    # The canonical documents. The only instructions in the repository.
    README.md|GOALS.md|GOVERNANCE.md|ARCHITECTURE.md|GLOSSARY.md|CLAUDE.md|DATA_CONTRACT.md) ;;
    # Every directory may explain itself; every stage declares what it writes.
    # Including at the root, which CLAUDE.md allows and a bare */ pattern misses.
    HANDOFF.md|*/README.md|*/HANDOFF.md) ;;
    # Dated evidence.
    history/*.md) ;;
    # The harness's own documents. One level only: `*` matches `/` in a shell
    # case pattern, so `.github/*.md` would otherwise permit a notes folder
    # nested to any depth underneath it.
    .github/*.md)
      case "${f#.github/}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    .claude/agents/*.md)
      case "${f#.claude/agents/}" in */*) printf '%s\n' "$f"; stray=1 ;; esac ;;
    *) printf '%s\n' "$f"; stray=1 ;;
  esac
done

exit $stray
