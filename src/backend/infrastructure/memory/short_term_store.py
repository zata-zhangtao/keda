"""短期记忆文件存储实现。

短期记忆按 ``<base_dir>/short_term/<repo_id>/<issue_number>/context.json`` 组织；
文件内容是结构化 JSON 上下文，包含任务摘要、尝试轮次、最终成功方案、关键文件路径。
所有文件 I/O 显式使用 ``encoding="utf-8"``。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ShortTermAttemptRecord:
    """短期记忆中一次尝试的精简记录。"""

    attempt_number: int
    failure_type: str
    detail: str
    recovered: bool = False


@dataclass
class ShortTermMemoryContext:
    """短期记忆上下文。"""

    repo_id: str
    issue_number: int
    issue_title: str
    issue_url: str
    summary: str = ""
    attempts: list[ShortTermAttemptRecord] = field(default_factory=list)
    final_solution: str = ""
    key_files: tuple[str, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict for on-disk persistence."""
        payload = asdict(self)
        payload["attempts"] = [asdict(record) for record in self.attempts]
        payload["key_files"] = list(self.key_files)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ShortTermMemoryContext":
        """Rehydrate from a persisted dict."""
        attempts_raw = payload.get("attempts", [])
        attempts = tuple(ShortTermAttemptRecord(**record) for record in attempts_raw)
        key_files = tuple(payload.get("key_files", ()))
        return cls(
            repo_id=str(payload.get("repo_id", "")),
            issue_number=int(payload.get("issue_number", 0)),
            issue_title=str(payload.get("issue_title", "")),
            issue_url=str(payload.get("issue_url", "")),
            summary=str(payload.get("summary", "")),
            attempts=list(attempts),
            final_solution=str(payload.get("final_solution", "")),
            key_files=key_files,
            updated_at=str(payload.get("updated_at", "")),
        )


class ShortTermMemoryStore:
    """基于本地文件系统的短期记忆读写器。"""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    @property
    def base_dir(self) -> Path:
        """Return the configured base directory."""
        return self._base_dir

    def _context_path(self, repo_id: str, issue_number: int) -> Path:
        return (
            self._base_dir
            / "short_term"
            / _safe_segment(repo_id)
            / str(int(issue_number))
            / "context.json"
        )

    def save(
        self,
        repo_id: str,
        issue_number: int,
        memory_context: ShortTermMemoryContext,
    ) -> Path:
        """Persist the short-term memory for an issue, returning the file path."""
        path = self._context_path(repo_id, issue_number)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = memory_context.to_dict()
        if not payload.get("updated_at"):
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, repo_id: str, issue_number: int) -> ShortTermMemoryContext | None:
        """Load a previously-saved short-term memory, or ``None`` if missing."""
        path = self._context_path(repo_id, issue_number)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return ShortTermMemoryContext.from_dict(payload)


def _safe_segment(value: str) -> str:
    """Coerce a string to a safe filesystem segment."""
    cleaned = (value or "").strip() or "default"
    return "".join(
        char if char.isalnum() or char in ("-", "_", ".") else "_" for char in cleaned
    )


__all__ = [
    "ShortTermAttemptRecord",
    "ShortTermMemoryContext",
    "ShortTermMemoryStore",
]
