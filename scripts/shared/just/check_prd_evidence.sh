#!/usr/bin/env bash
# scripts/shared/just/check_prd_evidence.sh
# Final gate for `just ai implement`: if a PRD touches frontend apps,
# its evidence directory must contain at least one screenshot or video.

set -euo pipefail

prd_path="${1:-}"
worktree_root="${2:-}"

if [ -z "$prd_path" ]; then
    echo "Usage: $0 <prd-path> [worktree-root]"
    exit 1
fi

if [ ! -f "$prd_path" ]; then
    echo "ERROR: PRD file not found: $prd_path"
    exit 1
fi

if [ -n "$worktree_root" ]; then
    search_root="$worktree_root"
else
    search_root="$(git rev-parse --show-toplevel)"
fi

prd_basename="$(basename "$prd_path" .md)"
evidence_dir="$search_root/tasks/evidence/$prd_basename"

# Determine whether this PRD touches frontend apps.
# Priority 1: inspect the PRD Change Impact Tree for frontend paths.
# Priority 2: if the PRD has no explicit tree, fall back to git diff against main.
touches_frontend=false

if grep -qE 'frontend-admin/|frontend-public/' "$prd_path"; then
    touches_frontend=true
fi

if [ "$touches_frontend" != "true" ] && [ -z "$worktree_root" ]; then
    # Fallback: check whether the current branch has modified frontend files.
    if git diff --name-only HEAD >/dev/null 2>&1; then
        if git diff --name-only HEAD | grep -qE '^(frontend-admin|frontend-public)/'; then
            touches_frontend=true
        fi
    fi
fi

if [ "$touches_frontend" != "true" ]; then
    echo "✅ No frontend changes detected for $prd_basename; visual evidence not required."
    exit 0
fi

if [ ! -d "$evidence_dir" ]; then
    echo "ERROR: Frontend changes detected but evidence directory is missing: $evidence_dir"
    echo "       Run Playwright e2e tests and copy screenshots/videos into that directory."
    exit 1
fi

visual_files=()
while IFS= read -r -d '' file; do
    visual_files+=("$file")
done < <(find "$evidence_dir" -maxdepth 1 -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webm' \) -print0 2>/dev/null || true)

if [ "${#visual_files[@]}" -eq 0 ]; then
    echo "ERROR: Frontend changes detected but no visual evidence (.png/.jpg/.webm) found in $evidence_dir"
    echo "       Run Playwright e2e tests and copy screenshots/videos into that directory."
    exit 1
fi

echo "✅ Frontend changes detected and visual evidence found:"
printf '   - %s\n' "$(basename -a "${visual_files[@]}")"
exit 0
