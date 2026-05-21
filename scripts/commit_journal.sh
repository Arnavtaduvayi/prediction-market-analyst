#!/usr/bin/env bash
# Commits journal state back to GitHub so the user can see progress from anywhere.
# Triggered hourly by predmkt-commit.timer.
#
# REQUIRES: a GitHub deploy key OR a personal access token configured for the
# predmkt user, allowing push access to the repo. See docs/VPS.md.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! git diff --quiet paper_cross_trades.json data/targets.json 2>/dev/null; then
  git add paper_cross_trades.json data/targets.json 2>/dev/null || true
  if ! git diff --staged --quiet; then
    git commit -m "vps: state snapshot $(date -u +%Y-%m-%dT%H:%MZ)" \
      --author="predmkt-bot <bot@$(hostname)>"
    git push origin main || echo "(push failed — check deploy key)"
  fi
fi
