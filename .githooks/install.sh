#!/bin/sh
# Install this repository's hooks. Run once, immediately after cloning:
#
#     sh .githooks/install.sh
#
# WHY THIS EXISTS
# ===============
# Git will not run hooks from a repository unless it is told to. `core.hooksPath`
# is a *local* setting: it lives in .git/config, and .git/config is never
# committed and never travels with a clone.
#
# So every rule in this repository — no commits on main, no stray notes, no
# force-push, no push without an audit — is switched off in a fresh clone until
# someone runs this. A new machine, a Codex sandbox, a pod, a second checkout:
# all of them start with nothing.
#
# Nothing can fix that from inside the repository. A clone cannot configure
# itself. All that is possible is to make the command short, put it in the
# documents, and make this script say plainly what is and is not protected.

set -e

if ! root=$(git rev-parse --show-toplevel 2>/dev/null); then
  echo "Not inside a git repository. Run this from the clone you just made." >&2
  exit 1
fi
cd "$root"

if [ ! -d .githooks ]; then
  echo "No .githooks directory here. Is this the right repository?" >&2
  exit 1
fi

# Relative, not absolute. A relative hooksPath resolves against whichever
# working tree is running the hook, so every git worktree uses the hooks on
# *its own* branch. With an absolute path they would all silently share the
# main checkout's copy, and a change to a hook could never be tested on the
# branch that makes it.
#
# The cost of that choice, and it is worth knowing: checking out a commit from
# before a hook existed gives you that commit's hooks, not today's. Protections
# travel with the branch. Going backwards in history goes backwards in guards.
git config core.hooksPath .githooks

chmod +x .githooks/pre-commit .githooks/pre-push .githooks/commit-msg \
         .githooks/check-all.sh .githooks/check-documents.sh \
         .githooks/doc-allowlist.sh .githooks/record-audit.sh \
         .githooks/install.sh 2>/dev/null || true

# Git does not preserve empty directories. Recreate the local working areas a
# fresh clone needs; their tracked README files explain what belongs in each.
mkdir -p workbench/active workbench/archive workbench/scratch \
         workbench/design workbench/tools workbench/raw

echo "Hooks installed for this clone."
echo ""
echo "  Now enforced here:"
echo "    no commits on main            no stray notes in the tree"
echo "    no secrets or large payloads  no undeclared binary fixtures"
echo "    no direct push to main        no force-push over someone's work"
echo "    no push without an audit      (.githooks/record-audit.sh)"
echo ""
echo "  Run all repository checks locally with:"
echo "    python3 -m pip install -r requirements-dev.txt"
echo "    sh .githooks/check-all.sh"
echo ""
echo "  GitHub enforces its own rules on main, separately from these and"
echo "  out of reach of anything local. What those are is recorded in"
echo "  README.md's status line, and only there — this script does not"
echo "  repeat them, because a second copy is a copy that goes stale."
echo ""
echo "  Each of these local hooks can be skipped deliberately — --no-verify,"
echo "  or the ALLOW_* variable each hook names when it blocks you. That is"
echo "  by design: they stop the accident, not the intention. The server-side"
echo "  rules are the ones that do not negotiate."
