"""Tests for the Idea Inbox use cases and API routes."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.api.routes import agent_runner_idea_inbox as inbox_routes
from backend.core.shared.interfaces.agent_runner import IContentGenerator
from backend.core.shared.models.agent_runner import (
    AppConfig,
    CommandResult,
    RepositoryRunContext,
)
from backend.core.shared.models.idea_inbox import IdeaInboxSource
from backend.core.use_cases.idea_inbox import (
    append_idea,
    read_idea_inbox,
    refresh_idea_summary,
)
from backend.core.use_cases.idea_prd_drafts import (
    IdeaInboxError,
    approve_prd_draft,
    create_prd_draft,
)

# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FakeContentGenerator(IContentGenerator):
    """A static ``IContentGenerator`` returning a fixed PRD body."""

    body: str

    def generate(
        self,
        agent_name: str,
        prompt: str,
        *,
        cwd: Path,
        timeout: int | None = None,
    ) -> CommandResult:
        return CommandResult(
            return_code=0,
            stdout=self.body,
            stderr="",
            command=("fake",),
        )


def _make_fake_generator(body: str) -> IContentGenerator:
    return _FakeContentGenerator(body=body)


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    """Initialize an empty git repo directory for the idea inbox use cases."""
    repo = tmp_path / "fake-repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _make_context(repo: Path) -> RepositoryRunContext:
    return RepositoryRunContext(
        repo_id="keda-main",
        display_name="Keda Main",
        repo_path=repo,
        config=AppConfig(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# use case: append_idea + read_idea_inbox
# ─────────────────────────────────────────────────────────────────────────────


def test_append_idea_creates_initial_file_and_appends_blocks(tmp_path: Path) -> None:
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    ideas_path = repo / "tasks" / "inbox" / "ideas.md"

    assert not ideas_path.exists()
    first = append_idea(
        repo,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="第一条想法",
        occurred_at="2026-06-15 09:00",
    )
    assert ideas_path.exists()
    snapshot = read_idea_inbox(repo)
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].entry_id == first.entry.entry_id
    assert snapshot.entries[0].text == "第一条想法"

    # Append a second one and verify append-only behavior.
    before_text = ideas_path.read_text(encoding="utf-8")
    append_idea(
        repo,
        source=IdeaInboxSource.FRONTEND,
        author="bob",
        text="第二条想法\n第二行",
        occurred_at="2026-06-15 09:30",
    )
    after_text = ideas_path.read_text(encoding="utf-8")
    assert before_text in after_text  # the first block is byte-identical.
    snapshot = read_idea_inbox(repo)
    assert len(snapshot.entries) == 2
    assert snapshot.entries[1].text == "第二条想法\n第二行"


def test_append_idea_rejects_empty_text(repo_dir: Path) -> None:
    with pytest.raises(ValueError):
        append_idea(
            repo_dir,
            source=IdeaInboxSource.FRONTEND,
            author="alice",
            text="   ",
            occurred_at="2026-06-15 09:00",
        )


def test_read_idea_inbox_handles_missing_files(repo_dir: Path) -> None:
    snapshot = read_idea_inbox(repo_dir)
    assert snapshot.entries == ()
    assert snapshot.drafts == ()
    assert snapshot.ideas_raw == ""
    assert snapshot.summary_raw == ""


# ─────────────────────────────────────────────────────────────────────────────
# use case: refresh_idea_summary
# ─────────────────────────────────────────────────────────────────────────────


def test_refresh_summary_marks_text_as_ai_derived(repo_dir: Path) -> None:
    append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 A",
        occurred_at="2026-06-15 09:00",
    )
    append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 B",
        occurred_at="2026-06-15 09:10",
    )
    result = refresh_idea_summary(
        repo_dir,
        summary_text="两条想法都是关于跨平台采集。",
        source_label="agent",
    )
    assert "事实来源是" in result.summary_text
    assert "来源：agent" in result.summary_text
    assert "AI 总结" in result.summary_text
    # Read snapshot sees the refreshed summary.
    snapshot = read_idea_inbox(repo_dir)
    assert snapshot.summary_raw == result.summary_text


# ─────────────────────────────────────────────────────────────────────────────
# use case: create_prd_draft + approve_prd_draft
# ─────────────────────────────────────────────────────────────────────────────


_DRAFT_BODY = """# PRD: 跨平台 Idea Inbox

## 1. Introduction & Goals

让外部 IM 的消息能进入 inbox。

## 2. Acceptance Checklist

- [ ] 端到端联调飞书 webhook
- [ ] 草稿入 pending 流程
"""


def test_create_draft_writes_to_prd_drafts_with_metadata(repo_dir: Path) -> None:
    entry_a = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 1",
        occurred_at="2026-06-15 09:00",
    )
    entry_b = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 2",
        occurred_at="2026-06-15 09:10",
    )
    generator = _make_fake_generator(_DRAFT_BODY)
    result = create_prd_draft(
        repo_dir,
        idea_refs=(entry_a.entry.entry_id, entry_b.entry.entry_id),
        generator=generator,
        priority="P1",
        prd_type="FEAT",
    )
    draft_path = repo_dir / result.draft_path
    assert draft_path.exists()
    text = draft_path.read_text(encoding="utf-8")
    assert "Draft Status: pending-review" in text
    assert "Priority: P1" in text
    assert "Type: FEAT" in text
    assert entry_a.entry.entry_id in text
    assert "跨平台 Idea Inbox" in text
    from backend.core.shared.models.idea_inbox import PrdDraftStatus

    assert result.draft.metadata.status is PrdDraftStatus.PENDING_REVIEW


def test_create_draft_falls_back_when_generator_returns_empty(
    repo_dir: Path,
) -> None:
    entry = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 1",
        occurred_at="2026-06-15 09:00",
    )

    class _FailingGenerator(IContentGenerator):
        def generate(self, agent_name, prompt, *, cwd, timeout=None):
            return CommandResult(
                return_code=1, stdout="", stderr="boom", command=("x",)
            )

    result = create_prd_draft(
        repo_dir,
        idea_refs=(entry.entry.entry_id,),
        generator=_FailingGenerator(),
        priority="P2",
        prd_type="FEAT",
    )
    text = (repo_dir / result.draft_path).read_text(encoding="utf-8")
    assert "本草稿由 append-only 想法生成" in text


def test_create_draft_rejects_invalid_priority_or_type(repo_dir: Path) -> None:
    entry = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 1",
        occurred_at="2026-06-15 09:00",
    )
    with pytest.raises(IdeaInboxError):
        create_prd_draft(
            repo_dir,
            idea_refs=(entry.entry.entry_id,),
            generator=None,
            priority="X9",
        )
    with pytest.raises(IdeaInboxError):
        create_prd_draft(
            repo_dir,
            idea_refs=(entry.entry.entry_id,),
            generator=None,
            prd_type="WTF",
        )


def test_approve_draft_creates_pending_and_marks_approved(repo_dir: Path) -> None:
    entry = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 1",
        occurred_at="2026-06-15 09:00",
    )
    generator = _make_fake_generator(_DRAFT_BODY)
    created = create_prd_draft(
        repo_dir,
        idea_refs=(entry.entry.entry_id,),
        generator=generator,
        priority="P1",
        prd_type="FEAT",
    )
    approved = approve_prd_draft(
        repo_dir,
        draft_relpath=created.draft_path,
        priority="P1",
        prd_type="FEAT",
    )
    pending_path = repo_dir / approved.pending_path
    assert pending_path.exists()
    pending_text = pending_path.read_text(encoding="utf-8")
    assert "Priority: P1" in pending_text
    assert "Type: FEAT" in pending_text
    assert "Source Idea Refs:" in pending_text
    assert approved.draft.metadata.approved_pending_path == approved.pending_path
    # The draft file's metadata now shows approved.
    draft_text = (repo_dir / created.draft_path).read_text(encoding="utf-8")
    assert "Draft Status: approved" in draft_text
    assert f"Approved Pending Path: {approved.pending_path}" in draft_text


def test_approve_draft_fails_fast_on_second_approval(repo_dir: Path) -> None:
    entry = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 1",
        occurred_at="2026-06-15 09:00",
    )
    created = create_prd_draft(
        repo_dir,
        idea_refs=(entry.entry.entry_id,),
        generator=_make_fake_generator(_DRAFT_BODY),
    )
    approve_prd_draft(repo_dir, draft_relpath=created.draft_path)
    with pytest.raises(IdeaInboxError) as excinfo:
        approve_prd_draft(repo_dir, draft_relpath=created.draft_path)
    assert "仅 pending-review 可批准" in str(excinfo.value)


def test_approve_draft_rejects_existing_pending_path(repo_dir: Path) -> None:
    entry = append_idea(
        repo_dir,
        source=IdeaInboxSource.FRONTEND,
        author="alice",
        text="想法 1",
        occurred_at="2026-06-15 09:00",
    )
    created = create_prd_draft(
        repo_dir,
        idea_refs=(entry.entry.entry_id,),
        generator=_make_fake_generator(_DRAFT_BODY),
    )
    pending_dir = repo_dir / "tasks" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    # Pre-create a pending file matching the same naming pattern.
    draft_stem = Path(created.draft_path).stem
    pending_filename = (
        f"P2-FEAT-{created.draft.metadata.draft_id}-"
        f"{'-'.join(draft_stem.split('-')[2:])}.md"
    )
    pending_dir.joinpath(pending_filename).write_text("placeholder", encoding="utf-8")
    with pytest.raises(IdeaInboxError) as excinfo:
        approve_prd_draft(repo_dir, draft_relpath=created.draft_path)
    assert "pending 文件已存在" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# API route tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def api_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "fake-repo"
    (repo / ".git").mkdir(parents=True)
    ctx = _make_context(repo)
    monkeypatch.setattr(inbox_routes, "_resolve_contexts", lambda: [ctx])
    monkeypatch.setattr(inbox_routes, "_resolve_context", lambda rid: ctx)
    monkeypatch.setattr(
        inbox_routes,
        "_get_content_generator",
        lambda: _make_fake_generator(_DRAFT_BODY),
    )
    return {"repo": repo}


def test_api_snapshot_is_200_and_empty(api_environment: dict) -> None:
    client = TestClient(app)
    response = client.get("/api/v1/agent-runner/idea-inbox/repositories/keda-main")
    assert response.status_code == 200
    payload = response.json()
    assert payload["entries"] == []
    assert payload["drafts"] == []


def test_api_append_idea_returns_entry_id(api_environment: dict) -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/ideas",
        json={"text": "来自 API", "author": "tester"},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["entry"]["text"] == "来自 API"
    # Snapshot now has the entry.
    snapshot = client.get(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main"
    ).json()
    assert len(snapshot["entries"]) == 1
    assert snapshot["entries"][0]["text"] == "来自 API"


def test_api_refresh_summary_replaces_content(api_environment: dict) -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/summary/refresh",
        json={"summary_text": "这是 AI 总结", "source_label": "agent"},
    )
    assert response.status_code == 200
    summary_path = response.json()["summary_path"]
    assert summary_path == "tasks/inbox/summary.md"
    text = (api_environment["repo"] / summary_path).read_text(encoding="utf-8")
    assert "这是 AI 总结" in text


def test_api_create_draft_uses_content_generator(api_environment: dict) -> None:
    client = TestClient(app)
    client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/ideas",
        json={"text": "想法 A", "author": "tester"},
    )
    snapshot = client.get(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main"
    ).json()
    entry_id = snapshot["entries"][0]["entry_id"]
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/drafts",
        json={
            "idea_refs": [entry_id],
            "priority": "P1",
            "prd_type": "FEAT",
        },
    )
    assert response.status_code == 201
    draft_path = response.json()["draft_path"]
    text = (api_environment["repo"] / draft_path).read_text(encoding="utf-8")
    assert "跨平台 Idea Inbox" in text
    assert "Priority: P1" in text


def test_api_approve_draft_moves_to_pending(api_environment: dict) -> None:
    client = TestClient(app)
    client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/ideas",
        json={"text": "想法 A", "author": "tester"},
    )
    snapshot = client.get(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main"
    ).json()
    entry_id = snapshot["entries"][0]["entry_id"]
    draft = client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/drafts",
        json={"idea_refs": [entry_id]},
    ).json()
    response = client.post(
        f"/api/v1/agent-runner/idea-inbox/repositories/keda-main/drafts/"
        f"{_urlsafe_b64(draft['draft_path'])}/approve",
        json={},
    )
    assert response.status_code == 200
    pending_path = response.json()["pending_path"]
    assert pending_path.startswith("tasks/pending/")
    assert (api_environment["repo"] / pending_path).exists()


def test_api_approve_draft_returns_400_on_already_approved(
    api_environment: dict,
) -> None:
    client = TestClient(app)
    client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/ideas",
        json={"text": "想法 A", "author": "tester"},
    )
    snapshot = client.get(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main"
    ).json()
    entry_id = snapshot["entries"][0]["entry_id"]
    draft = client.post(
        "/api/v1/agent-runner/idea-inbox/repositories/keda-main/drafts",
        json={"idea_refs": [entry_id]},
    ).json()
    encoded = _urlsafe_b64(draft["draft_path"])
    client.post(
        f"/api/v1/agent-runner/idea-inbox/repositories/keda-main/drafts/{encoded}/approve",
        json={},
    )
    response = client.post(
        f"/api/v1/agent-runner/idea-inbox/repositories/keda-main/drafts/{encoded}/approve",
        json={},
    )
    assert response.status_code == 400


def test_api_inbound_rejects_missing_signature(
    api_environment: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IAR_IDEA_INBOX_INBOUND_SECRET", "topsecret")
    client = TestClient(app)
    body = json.dumps(
        {
            "provider": "feishu",
            "repo_id": "keda-main",
            "sender": "user-1",
            "text": "想法",
        },
        ensure_ascii=False,
    )
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/inbound",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_api_inbound_rejects_invalid_signature(
    api_environment: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IAR_IDEA_INBOX_INBOUND_SECRET", "topsecret")
    client = TestClient(app)
    body = json.dumps(
        {
            "provider": "feishu",
            "repo_id": "keda-main",
            "sender": "user-1",
            "text": "想法",
        },
        ensure_ascii=False,
    )
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-IAR-Signature": "sha256=deadbeef",
        },
    )
    assert response.status_code == 401


def test_api_inbound_rejects_missing_secret(
    api_environment: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IAR_IDEA_INBOX_INBOUND_SECRET", raising=False)
    client = TestClient(app)
    body = json.dumps(
        {
            "provider": "feishu",
            "repo_id": "keda-main",
            "sender": "user-1",
            "text": "想法",
        },
        ensure_ascii=False,
    )
    sig = (
        "sha256="
        + hmac.new(b"topsecret", body.encode("utf-8"), hashlib.sha256).hexdigest()
    )
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-IAR-Signature": sig,
        },
    )
    assert response.status_code == 503


def test_api_inbound_rejects_unknown_repo(
    api_environment: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IAR_IDEA_INBOX_INBOUND_SECRET", "topsecret")
    from fastapi import HTTPException

    def _reject(_rid: str):
        raise HTTPException(status_code=400, detail="仓库 'keda-main' 不存在或未启用。")

    monkeypatch.setattr(inbox_routes, "_resolve_context", _reject)
    client = TestClient(app)
    body = json.dumps(
        {
            "provider": "feishu",
            "repo_id": "keda-main",
            "sender": "user-1",
            "text": "想法",
        },
        ensure_ascii=False,
    )
    sig = (
        "sha256="
        + hmac.new(b"topsecret", body.encode("utf-8"), hashlib.sha256).hexdigest()
    )
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-IAR-Signature": sig,
        },
    )
    assert response.status_code == 400


def test_api_inbound_accepts_valid_signature(
    api_environment: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IAR_IDEA_INBOX_INBOUND_SECRET", "topsecret")
    client = TestClient(app)
    body = json.dumps(
        {
            "provider": "feishu",
            "repo_id": "keda-main",
            "sender": "user-1",
            "text": "从飞书来的想法",
        },
        ensure_ascii=False,
    )
    sig = (
        "sha256="
        + hmac.new(b"topsecret", body.encode("utf-8"), hashlib.sha256).hexdigest()
    )
    response = client.post(
        "/api/v1/agent-runner/idea-inbox/inbound",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-IAR-Signature": sig,
        },
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["repo_id"] == "keda-main"
    assert "entry_id" in payload


def test_api_metadata_endpoint_returns_options(api_environment: dict) -> None:
    client = TestClient(app)
    response = client.get("/api/v1/agent-runner/idea-inbox/metadata")
    assert response.status_code == 200
    payload = response.json()
    assert "P1" in payload["priorities"]
    assert "FEAT" in payload["prd_types"]
    assert payload["inbound_signature_header"] == "X-IAR-Signature"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _urlsafe_b64(value: str) -> str:
    import base64

    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
