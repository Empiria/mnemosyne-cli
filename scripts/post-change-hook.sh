#!/bin/bash
# Git post-commit / post-merge hook for the Mnemosyne vault.
# Checks if committed changes require running `mnemosyne refresh`.
# Install: symlink from .git/hooks/post-commit and .git/hooks/post-merge

changed_files=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null)

# For post-merge, diff against ORIG_HEAD
if [ -z "$changed_files" ] && [ -f "$(git rev-parse --git-dir)/ORIG_HEAD" ]; then
    changed_files=$(git diff --name-only ORIG_HEAD HEAD 2>/dev/null)
fi

[ -z "$changed_files" ] && exit 0

needs_images=false
needs_qmd=false

if echo "$changed_files" | grep -q "^containers/"; then
    needs_images=true
fi

if echo "$changed_files" | grep -qE "^(technologies/|agents/|docs/|projects/.*\.md)"; then
    needs_qmd=true
fi

if $needs_images && $needs_qmd; then
    echo ""
    echo "  ⟳ Container files and vault content changed — run: mnemosyne refresh"
    echo ""
elif $needs_images; then
    echo ""
    echo "  ⟳ Container files changed — run: mnemosyne refresh --skip-qmd"
    echo ""
elif $needs_qmd; then
    echo ""
    echo "  ⟳ Vault content changed — run: mnemosyne refresh --skip-images"
    echo ""
fi
