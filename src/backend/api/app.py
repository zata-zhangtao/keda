"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from backend.api.routes import (
    agent_runner,
    agent_runner_console,
    agent_runner_idea_inbox,
    agent_runner_roadmap,
    local_auth,
)

app = FastAPI(title="keda backend")
app.include_router(agent_runner.router, prefix="/api/v1")
app.include_router(agent_runner_console.router, prefix="/api/v1")
app.include_router(agent_runner_roadmap.router, prefix="/api/v1")
app.include_router(agent_runner_idea_inbox.router, prefix="/api/v1")
app.include_router(local_auth.router, prefix="/api")
