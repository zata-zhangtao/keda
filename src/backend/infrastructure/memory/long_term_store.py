"""长期记忆文件存储实现（按主题/类别组织的 markdown）。

所有写入通过共享的 ``infrastructure/memory/_atomic_io.atomic_write_text``
完成 ``tmp + os.replace`` 原子落盘，避免并发 save 时产生半写损坏文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ._atomic_io import atomic_write_text

_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*(?:\n|$)", re.DOTALL)


@dataclass(frozen=True)
class LongTermFact:
    """A single long-term memory fact with tags."""

    topic: str
    category: str
    content: str
    tags: tuple[str, ...]
    path: Path

    @property
    def name(self) -> str:
        """Return a stable display name for the fact."""
        return f"{self.category}/{self.topic}"


class LongTermMemoryStore:
    """Long-term memory persisted as markdown files with YAML-like front matter."""

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    @property
    def base_dir(self) -> Path:
        """Return the configured base directory."""
        return self._base_dir

    def append_fact(
        self,
        *,
        category: str,
        topic: str,
        content: str,
        tags: Iterable[str] = (),
    ) -> Path:
        """Append or create a fact file. Existing tag union is preserved.

        通过 ``atomic_write_text`` 原子落盘：先写入临时文件再用
        ``os.replace`` 替换，确保并发 save 时目标文件始终是完整可解析
        的 markdown，不出现半写损坏的中间状态。
        """
        path = self._fact_path(category, topic)
        existing = self._read_existing(path)
        merged_tags = _merge_tags(existing.tags, tags)
        payload = _build_markdown(
            category=category,
            topic=topic,
            content=content,
            tags=merged_tags,
        )
        return atomic_write_text(path, payload)

    def load_by_tags(self, tags: Iterable[str], *, limit: int | None = None) -> list[LongTermFact]:
        """Load all facts whose tag set intersects with ``tags``."""
        target_tags = {tag.strip() for tag in tags if tag and tag.strip()}
        if not target_tags:
            return []
        matches: list[LongTermFact] = []
        if not self._base_dir.exists():
            return []
        for path in sorted(self._base_dir.rglob("*.md")):
            fact = _parse_fact(path)
            if fact is None:
                continue
            if target_tags.intersection(fact.tags):
                matches.append(fact)
                if limit is not None and len(matches) >= limit:
                    break
        return matches

    def load_all(self) -> list[LongTermFact]:
        """Load every fact under the base directory."""
        if not self._base_dir.exists():
            return []
        facts: list[LongTermFact] = []
        for path in sorted(self._base_dir.rglob("*.md")):
            fact = _parse_fact(path)
            if fact is not None:
                facts.append(fact)
        return facts

    def _fact_path(self, category: str, topic: str) -> Path:
        return self._base_dir / "long_term" / _safe_segment(category) / f"{_safe_segment(topic)}.md"

    def _read_existing(self, path: Path) -> _ExistingFact:
        if not path.is_file():
            return _ExistingFact(content="", tags=())
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return _ExistingFact(content="", tags=())
        body, tags = _split_frontmatter(raw)
        return _ExistingFact(content=body, tags=tags)


@dataclass(frozen=True)
class _ExistingFact:
    content: str
    tags: tuple[str, ...]


def _merge_tags(
    existing: tuple[str, ...],
    new: Iterable[str],
) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for tag in list(existing) + list(new):
        if not tag:
            continue
        normalized = str(tag).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return tuple(merged)


def _build_markdown(
    *,
    category: str,
    topic: str,
    content: str,
    tags: tuple[str, ...],
) -> str:
    lines = [
        "---",
        f"category: {category}",
        f"topic: {topic}",
        f"tags: [{', '.join(tags)}]",
        "---",
        "",
        content.rstrip() + "\n",
    ]
    return "\n".join(lines)


def _split_frontmatter(raw: str) -> tuple[str, tuple[str, ...]]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return raw, ()
    body = raw[match.end() :]
    tags = _parse_tags(match.group("body"))
    return body, tags


def _parse_tags(frontmatter: str) -> tuple[str, ...]:
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if not stripped.startswith("tags:"):
            continue
        value = stripped[len("tags:") :].strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            parts = [part.strip() for part in inner.split(",")]
            return tuple(part for part in parts if part)
        return tuple(value.split())
    return ()


def _parse_fact(path: Path) -> LongTermFact | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    body, tags = _split_frontmatter(raw)
    if not body.strip():
        return None
    frontmatter_match = _FRONTMATTER_RE.match(raw)
    category = ""
    topic = ""
    if frontmatter_match is not None:
        for line in frontmatter_match.group("body").splitlines():
            stripped = line.strip()
            if stripped.startswith("category:"):
                category = stripped[len("category:") :].strip()
            elif stripped.startswith("topic:"):
                topic = stripped[len("topic:") :].strip()
    if not topic:
        topic = path.stem
    if not category:
        category = path.parent.name
    return LongTermFact(
        topic=topic,
        category=category,
        content=body.strip(),
        tags=tags,
        path=path,
    )


def _safe_segment(value: str) -> str:
    cleaned = (value or "").strip() or "default"
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in cleaned)


__all__ = [
    "LongTermFact",
    "LongTermMemoryStore",
]
