#!/bin/sh
# Run once after cloning: sh .githooks/install.sh
# core.hooksPath is local config, so a fresh clone runs no hooks until this runs. The
# path is relative so each worktree uses the hooks on its own branch.
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

# git does not run pre-commit for a merge, so pre-merge-commit carries the same
# checks there. A hook that cannot be made executable fails the install.
if ! chmod +x .githooks/pre-commit .githooks/pre-merge-commit \
           .githooks/commit-msg \
           .githooks/check-all.sh .githooks/check-fast.sh \
           .githooks/check-static.sh .githooks/check-documents.sh \
           .githooks/install.sh; then
  echo "Could not make the hooks executable. Hooks were not configured and are not usable." >&2
  echo "Fix the filesystem permissions, then run this installer again." >&2
  exit 1
fi

# Git does not preserve empty directories. Recreate the local working areas a
# fresh clone needs; their tracked README files explain what belongs in each.
mkdir -p workbench/active workbench/standing workbench/archive \
         workbench/scratch workbench/design workbench/tools workbench/raw \
         workbench/quarantine

# Configure Git only after every filesystem prerequisite succeeds. If either
# chmod or mkdir fails, a previously working hooksPath must stay in place.
git config core.hooksPath .githooks

echo "Hooks installed for this clone."
echo ""
echo "  On every commit: no commits on main, and no credentials, undeclared"
echo "  binaries or oversized files in what you stage or in the message."
echo ""
echo "  Quick checks:  sh .githooks/check-fast.sh"
echo "  Full suite:    sh .githooks/check-all.sh   (CI runs this on every pull request)"
