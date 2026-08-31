#!/usr/bin/env bash
# Install the repo git hooks for this clone.
# Idempotent: safe to run again any time.
set -euo pipefail
cd "$(dirname "$0")/.."

git config core.hooksPath .githooks
echo "hooks installed: pre-commit (fast checks), commit-msg (format), pre-push (full tests)"
