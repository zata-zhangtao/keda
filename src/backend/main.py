"""Backend application entrypoint.

Supports both FastAPI (``uvicorn backend.main:app``) and CLI (``iar``) modes.
The CLI entry point lives in ``backend.api.cli:main`` and is registered via
``[project.scripts]`` in ``pyproject.toml``.
"""

from __future__ import annotations


def main() -> None:
    """Run the backend entrypoint placeholder."""
    import uvicorn

    uvicorn.run("backend.api.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
