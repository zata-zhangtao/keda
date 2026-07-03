"""将已晋升 skill 列表格式化为注入 prompt 的目录段落。"""

from __future__ import annotations

from typing import Iterable

_MAX_DESCRIPTION_CHARS = 240


def format_skill_catalog(
    skills: Iterable,
    *,
    header: str = "Available Skills (read the file when relevant):",
) -> str:
    """Render a directory-style listing of promoted skills for a prompt.

    Only the ``name``, ``description`` and on-disk ``path`` are included —
    skill bodies are intentionally NOT inlined, so the agent reads them
    on demand rather than burning prompt tokens up front.

    Args:
        skills: Iterable of :class:`SkillDraft` (or any object exposing
            ``name``, ``description`` and ``path`` attributes).
        header: Optional leading header line.

    Returns:
        A multi-line block. Empty string when ``skills`` is empty so the
        caller can insert it unconditionally without producing an empty
        paragraph.
    """
    materialised = list(skills)
    if not materialised:
        return ""
    lines: list[str] = [header]
    for skill in materialised:
        description = _truncate(str(skill.description or ""), _MAX_DESCRIPTION_CHARS)
        path_text = str(skill.path)
        lines.append(f"- {skill.name} — {description} (path: {path_text})")
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


__all__ = ["format_skill_catalog"]
