# scripts/shared/alembic

This directory holds shell scripts that the upstream template exposes to all derived projects via `justfile.shared`.

## new_migration.sh

Create a new Alembic migration script under `alembic/versions/` following the naming convention defined in `docs/ai-standards/alembic.md`:

```bash
scripts/shared/alembic/new_migration.sh <slug> [<alembic-versions-dir>]
```

Examples:

```bash
# Default location: ./alembic/versions
scripts/shared/alembic/new_migration.sh add_trace_tables

# Custom location
scripts/shared/alembic/new_migration.sh drop_legacy_session_index ./alembic/versions
```

Behavior:

- Validates `<slug>` against `^[a-z][a-z0-9_]*$`.
- Calls `uv run alembic heads` and aborts unless there is exactly one head.
- Generates a temporary file via `alembic revision` so the body matches `script.py.mako`, then renames it to `YYYYMMDD_HHMMSS_<slug>.py`.
- Rewrites the `Revision ID`, `Revises`, `Create Date` docstring lines and the `revision` variable so they match the filename.
- Prints the resolved `down_revision` so the developer / agent can confirm the chain before editing `upgrade()` / `downgrade()`.

This script is invoked from the `just new-migration` recipe in `justfile.shared`.
