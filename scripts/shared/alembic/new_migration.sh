#!/usr/bin/env bash
# Create a new Alembic migration script with a YYYYMMDD_HHMMSS_<slug>.py name.
#
# This script enforces the migration naming convention documented in
# docs/ai-standards/alembic.md. It only generates the file shell; the developer
# (or AI agent) is responsible for filling in upgrade() / downgrade() and the
# docstring.

set -euo pipefail

show_usage() {
    cat <<'EOF'
Usage:
  scripts/shared/alembic/new_migration.sh <slug> [<alembic-versions-dir>]

Arguments:
  slug                  Required. Lowercase snake_case describing the migration,
                        e.g. add_trace_tables, drop_legacy_session_index.
  alembic-versions-dir  Optional. Defaults to ./alembic/versions relative to
                        the current working directory.

Behavior:
  - Rejects slugs that are empty, contain non-[a-z0-9_] characters, or start
    with a digit (Python identifier rules).
  - Refuses to run when alembic heads reports zero or multiple heads; the
    version graph must be a single linear head.
  - Invokes `alembic revision -m <slug>` so the body matches script.py.mako,
    then renames the generated file to YYYYMMDD_HHMMSS_<slug>.py and rewrites
    the docstring header + `revision` variable so they match the filename.
  - Does NOT auto-fill upgrade() / downgrade(); the caller edits those by hand.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    show_usage
    exit 0
fi

slug="${1:-}"
versions_dir="${2:-./alembic/versions}"

if [ -z "$slug" ]; then
    echo "ERROR: missing <slug> argument." >&2
    show_usage >&2
    exit 2
fi

if ! [[ "$slug" =~ ^[a-z][a-z0-9_]*$ ]]; then
    echo "ERROR: slug must match ^[a-z][a-z0-9_]*\$ (got: '$slug')." >&2
    exit 2
fi

if [ ! -d "$versions_dir" ]; then
    echo "ERROR: alembic versions directory not found: $versions_dir" >&2
    exit 2
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is required on PATH." >&2
    exit 2
fi

# Enforce unique head before creating a new revision.
heads_output="$(uv run alembic heads 2>/dev/null || true)"
head_count="$(printf "%s\n" "$heads_output" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
if [ "$head_count" -ne 1 ]; then
    echo "ERROR: alembic version graph must have exactly 1 head before creating a new migration." >&2
    echo "       Current heads:" >&2
    printf "%s\n" "$heads_output" >&2
    exit 3
fi

current_head="$(printf "%s\n" "$heads_output" | sed '/^[[:space:]]*$/d' | head -n 1)"
timestamp="$(date +%Y%m%d_%H%M%S)"
target_filename="${timestamp}_${slug}.py"
target_path="${versions_dir%/}/${target_filename}"

if [ -e "$target_path" ]; then
    echo "ERROR: target file already exists: $target_path" >&2
    exit 3
fi

# Find the new file that alembic just generated. We can't pre-name it because
# alembic revision does not expose --output-file; it always writes to the
# configured version_path with a name derived from -m + rev_id.
generated_files_before="$(ls "$versions_dir" 2>/dev/null || true)"

uv run alembic revision -m "$slug" >/dev/null

generated_files_after="$(ls "$versions_dir")"
generated_file="$(comm -13 \
    <(printf "%s\n" "$generated_files_before" | sed '/^$/d' | sort) \
    <(printf "%s\n" "$generated_files_after"  | sed '/^$/d' | sort) | head -n 1)"

if [ -z "$generated_file" ]; then
    echo "ERROR: could not locate the new migration file alembic just created." >&2
    exit 4
fi

generated_path="${versions_dir%/}/${generated_file}"
mv "$generated_path" "$target_path"

# Patch docstring header (Revision ID / Revises / Create Date) and the
# `revision` variable so they match the filename, even if script.py.mako or
# the alembic defaults produced something different.
create_date="$(date '+%Y-%m-%d %H:%M:%S.000000')"
if sed --version >/dev/null 2>&1; then
    sed -i \
        -e "s|^Revision ID: .*|Revision ID: ${timestamp}|" \
        -e "s|^Revises: .*|Revises: ${current_head}|" \
        -e "s|^Create Date: .*|Create Date: ${create_date}|" \
        -e "s|^revision: str = .*|revision: str = \"${timestamp}\"|" \
        "$target_path"
else
    sed -i '' \
        -e "s|^Revision ID: .*|Revision ID: ${timestamp}|" \
        -e "s|^Revises: .*|Revises: ${current_head}|" \
        -e "s|^Create Date: .*|Create Date: ${create_date}|" \
        -e "s|^revision: str = .*|revision: str = \"${timestamp}\"|" \
        "$target_path"
fi

echo "Created: $target_path"
echo "down_revision: $current_head"
echo "Next: edit upgrade() and downgrade() in $target_path"
