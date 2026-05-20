"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from backend.api.routes import agent_runner

app = FastAPI(title="keda backend")
app.include_router(agent_runner.router, prefix="/api/v1")
