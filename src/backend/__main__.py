"""Entry point for ``python -m backend``."""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("backend.api.app:app", host="0.0.0.0", port=port)
