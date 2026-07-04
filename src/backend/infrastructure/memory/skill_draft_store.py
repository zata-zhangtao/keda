"""Skill 草稿与已晋升 skill 的本地文件系统持久化。

所有 save / update / 晋升后的写回通过
``infrastructure/memory/_atomic_io.atomic_write_text`` 完成原子落盘，
避免并发 save 时产生半写损坏文件。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ._atomic_io import atomic_write_text

_FRONTMATTER_RE = re.compile(r"^---\s*\n(?P<body>.*?)\n---\s*(?:\n|$)", re.DOTALL)


@dataclass(frozen=True)
class SkillDraft:
    """Persisted skill draft loaded from a markdown file."""

    name: str
    description: str
    tags: tuple[str, ...]
    version: str
    draft: bool
    updated: str
    usage_count: int
    success_count: int
    path: Path
    body: str = ""

    @property
    def success_rate(self) -> float:
        """Return success_rate, defined as success_count / usage_count."""
        if self.usage_count <= 0:
            return 0.0
        return self.success_count / self.usage_count


@dataclass
class SkillDraftUpdate:
    """Mutable record used to update an existing draft in place."""

    name: str
    description: str
    tags: tuple[str, ...]
    body: str
    usage_count: int = 0
    success_count: int = 0
    version: str = "1.0.0"
    draft: bool = True


class SkillDraftStore:
    """Skill draft and promoted-skill persistence."""

    def __init__(self, drafts_dir: str | Path) -> None:
        self._drafts_dir = Path(drafts_dir)

    @property
    def drafts_dir(self) -> Path:
        """Return the drafts directory."""
        return self._drafts_dir

    def save_draft(self, draft: SkillDraftUpdate) -> Path:
        """Persist a new or updated draft to the drafts directory.

        通过 ``atomic_write_text`` 原子落盘，避免并发 save 时出现半写
        损坏文件。
        """
        self._drafts_dir.mkdir(parents=True, exist_ok=True)
        path = self._drafts_dir / f"{_safe_segment(draft.name)}.md"
        atomic_write_text(path, _build_skill_markdown(draft))
        return path

    def find_similar_draft(
        self,
        *,
        name: str,
        tags: Iterable[str],
        description: str = "",
        similarity_threshold: float = 0.8,
    ) -> SkillDraft | None:
        """Find the closest existing draft, if any, by name+tags+description.

        The score is the ratio of overlapping tokens between the new draft and
        the candidate, weighted toward ``name`` and ``tags``. The default
        threshold of ``0.8`` keeps the merge conservative.

        Issue numbers in the name and description are stripped before
        comparison so that two consecutive similar Issues — e.g. ``#1`` and
        ``#2`` — collapse into a single draft instead of creating one per
        Issue.
        """
        if not self._drafts_dir.exists():
            return None
        target_tokens = _tokenize(
            _strip_issue_numbers(name), *tags, _strip_issue_numbers(description)
        )
        best: tuple[float, SkillDraft] | None = None
        for path in sorted(self._drafts_dir.glob("*.md")):
            candidate = _parse_skill(path)
            if candidate is None:
                continue
            candidate_tokens = _tokenize(
                _strip_issue_numbers(candidate.name),
                *candidate.tags,
                _strip_issue_numbers(candidate.description),
            )
            score = _jaccard(target_tokens, candidate_tokens)
            if best is None or score > best[0]:
                best = (score, candidate)
        if best is None or best[0] < similarity_threshold:
            return None
        return best[1]

    def update_draft(
        self,
        existing: SkillDraft,
        update: SkillDraftUpdate,
    ) -> Path:
        """Merge ``update`` into the existing draft, returning its new path."""
        merged = SkillDraftUpdate(
            name=existing.name,
            description=existing.description or update.description,
            tags=_merge_tags(existing.tags, update.tags),
            body=update.body or existing.body,
            usage_count=existing.usage_count + max(0, update.usage_count),
            success_count=existing.success_count + max(0, update.success_count),
            version=existing.version or update.version,
            draft=existing.draft and update.draft,
        )
        return self.save_draft(merged)

    def load_promoted_skills(self, skills_dirs: Iterable[str | Path]) -> list[SkillDraft]:
        """Load all promoted (non-draft) skills from the given directories."""
        results: list[SkillDraft] = []
        for skills_dir in skills_dirs:
            base = Path(skills_dir)
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.md")):
                skill = _parse_skill(path)
                if skill is None or skill.draft:
                    continue
                results.append(skill)
        return results

    def promote_draft(
        self,
        draft: SkillDraft,
        target_dirs: Iterable[str | Path],
    ) -> Path | None:
        """Move a draft to the first writable target directory and clear the draft flag."""
        if not draft.draft:
            return draft.path
        for target_dir in target_dirs:
            target_path = Path(target_dir)
            target_path.mkdir(parents=True, exist_ok=True)
            destination = target_path / draft.path.name
            try:
                shutil.move(str(draft.path), str(destination))
            except OSError:
                continue
            try:
                promoted_text = destination.read_text(encoding="utf-8")
            except OSError:
                continue
            promoted_text = re.sub(
                r"^draft:\s*true\s*$",
                "draft: false",
                promoted_text,
                count=1,
                flags=re.MULTILINE,
            )
            atomic_write_text(destination, promoted_text)
            return destination
        return None


def _merge_tags(*tag_groups: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in tag_groups:
        for tag in group:
            normalized = str(tag).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _build_skill_markdown(draft: SkillDraftUpdate) -> str:
    lines = [
        "---",
        f"name: {draft.name}",
        f"description: {_yaml_escape(draft.description)}",
        f"tags: [{', '.join(draft.tags)}]",
        f"version: {draft.version or '1.0.0'}",
        f"draft: {'true' if draft.draft else 'false'}",
        f"updated: {datetime.now(timezone.utc).isoformat()}",
        f"usage_count: {int(draft.usage_count)}",
        f"success_count: {int(draft.success_count)}",
        "---",
        "",
        draft.body.rstrip() + "\n",
    ]
    return "\n".join(lines)


def _yaml_escape(value: str) -> str:
    if any(char in value for char in (":", "#", "\n", '"')):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _parse_skill(path: Path) -> SkillDraft | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        return None
    frontmatter = match.group("body")
    body = raw[match.end() :]
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if "name" not in fields:
        return None
    tags = _parse_tags_field(fields.get("tags", ""))
    return SkillDraft(
        name=fields.get("name", path.stem),
        description=fields.get("description", ""),
        tags=tags,
        version=fields.get("version", "1.0.0"),
        draft=_parse_bool(fields.get("draft", "false")),
        updated=fields.get("updated", ""),
        usage_count=_parse_int(fields.get("usage_count", "0")),
        success_count=_parse_int(fields.get("success_count", "0")),
        path=path,
        body=body.strip(),
    )


def _parse_tags_field(value: str) -> tuple[str, ...]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1]
        return tuple(part.strip() for part in inner.split(",") if part.strip())
    return tuple(value.split())


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "yes", "1"}


def _parse_int(value: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _tokenize(*values: str) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        for token in re.findall(r"[a-z0-9_\-]+", value.lower()):
            if len(token) > 1:
                tokens.add(token)
    return tokens


_ISSUE_NUMBER_RE = re.compile(r"\bissue[-\s#]*\d+\b", re.IGNORECASE)
_HASHED_ISSUE_RE = re.compile(r"#\d+\b")


def _strip_issue_numbers(value: str) -> str:
    """Remove ``Issue #N`` / ``issue-N`` / ``#N`` markers from a string.

    Used by :meth:`SkillDraftStore.find_similar_draft` so that consecutive
    similar Issues with different numbers still collapse to a single draft.
    """
    if not value:
        return value
    value = _ISSUE_NUMBER_RE.sub("issue", value)
    value = _HASHED_ISSUE_RE.sub("", value)
    return value


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return intersection / union


def _safe_segment(value: str) -> str:
    cleaned = (value or "").strip() or "default"
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in cleaned)


__all__ = [
    "SkillDraft",
    "SkillDraftStore",
    "SkillDraftUpdate",
]
