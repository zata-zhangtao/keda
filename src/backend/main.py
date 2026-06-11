"""Backend application entrypoint.

Supports both FastAPI (``uvicorn backend.main:app``) and CLI (``iar``) modes.
The CLI entry point lives in ``backend.api.cli:main`` and is registered via
``[project.scripts]`` in ``pyproject.toml``.
"""

from __future__ import annotations

import os


def main() -> None:
    """Run the backend entrypoint placeholder."""
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("backend.api.app:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
