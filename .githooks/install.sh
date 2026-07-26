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
git config core.hooksPath .githooks

chmod +x .githooks/pre-commit .githooks/pre-push .githooks/record-audit.sh \
         .githooks/install.sh 2>/dev/null || true

echo "Hooks installed for this clone."
echo ""
echo "  Now enforced here:"
echo "    no commits on main            no stray notes in the tree"
echo "    no direct push to main        no force-push over someone's work"
echo "    no push without an audit      (.githooks/record-audit.sh)"
echo ""
echo "  Still NOT enforced anywhere, and no local setting can change it:"
echo "    Nothing stops a pull request being merged with a red check."
echo "    This is a private repository on a free plan, which has no branch"
echo "    protection, so every check here is an alarm and none is a lock."
echo ""
echo "  Each of these hooks can be skipped deliberately — --no-verify, or the"
echo "  ALLOW_* variables each hook names when it blocks you. That is by"
echo "  design. They stop the accident, not the intention."
