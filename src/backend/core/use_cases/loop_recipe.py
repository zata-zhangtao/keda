"""Loop recipe parsing, validation, and template rendering.

A loop recipe is a Markdown file with YAML frontmatter that describes how a
recurring task should be generated. The frontmatter declares the schedule,
target repository, and other tunables; the body is a PRD template rendered
once per fire with built-in and pre-command variables.

This module is intentionally free of infrastructure dependencies. It operates
on a parsed string or ``Path`` and produces plain dataclasses that other use
cases compose with state-store and GitHub client ports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.shared.models.loop import (
    LoopRecipe,
    LoopSchedule,
)

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<front>.*?)\r?\n---[ \t]*\r?\n(?P<body>.*)\Z",
    re.DOTALL,
)


@dataclass(frozen=True)
class _Frontmatter:
    """Raw frontmatter parsed from a recipe."""

    data: dict[str, Any]
    raw_text: str


def _split_frontmatter(recipe_text: str) -> tuple[_Frontmatter, str]:
    """Split a recipe string into its frontmatter block and body.

    Args:
        recipe_text: Full recipe file contents.

    Returns:
        Tuple of ``(_Frontmatter, body_text)``. When no frontmatter block is
        present, returns an empty frontmatter and the entire input as body.

    Raises:
        ValueError: When the frontmatter block exists but cannot be parsed.
    """
    match = _FRONTMATTER_RE.match(recipe_text)
    if match is None:
        return _Frontmatter(data={}, raw_text=""), recipe_text
    raw_front = match.group("front")
    body = match.group("body")
    try:
        data = _parse_simple_yaml(raw_front)
    except ValueError as exc:
        raise ValueError(f"Failed to parse loop recipe frontmatter: {exc}") from exc
    return _Frontmatter(data=data, raw_text=raw_front), body


def _parse_simple_yaml(raw_yaml: str) -> dict[str, Any]:
    """Parse the small subset of YAML used by loop recipe frontmatter.

    The MVP intentionally avoids pulling in a full YAML library. The supported
    shape is::

        key: value
        key: "quoted value"
        list_key:
          - item
          - item

    Anything more exotic should be migrated to ``pyyaml`` later.

    Args:
        raw_yaml: The frontmatter text between the ``---`` markers.

    Returns:
        Dict with parsed values; nested keys (one level deep) supported.

    Raises:
        ValueError: When the YAML shape is unsupported.
    """
    lines = raw_yaml.splitlines()
    result: dict[str, Any] = {}
    current_list_key: str | None = None
    for raw_line in lines:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith("  ") and current_list_key is not None:
            stripped = raw_line.strip()
            if not stripped.startswith("- "):
                raise ValueError(f"Unsupported list item syntax: {raw_line!r}")
            item_text = stripped[2:].strip()
            existing = result.setdefault(current_list_key, [])
            if not isinstance(existing, list):
                raise ValueError(
                    f"Key {current_list_key!r} cannot be both scalar and list."
                )
            existing.append(_coerce_yaml_value(item_text))
            continue
        if ":" not in raw_line:
            raise ValueError(f"Invalid frontmatter line (missing ':'): {raw_line!r}")
        key, _, value_text = raw_line.partition(":")
        key = key.strip()
        value_text = value_text.strip()
        current_list_key = None
        if not value_text:
            # Empty value starts a list block.
            result[key] = []
            current_list_key = key
            continue
        if value_text in ("true", "false"):
            result[key] = value_text == "true"
            continue
        result[key] = _coerce_yaml_value(value_text)
    return result


def _coerce_yaml_value(text: str) -> Any:
    """Coerce a single YAML scalar to a Python value."""
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_coerce_yaml_value(part.strip()) for part in inner.split(",")]
    return text


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_REQUIRED_RECIPE_FIELDS: tuple[str, ...] = ("id", "schedule", "repo_id")
_VALID_ISSUE_TYPES: frozenset[str] = frozenset({"feature", "refactor", "bug"})
_VALID_AGENTS: frozenset[str] = frozenset({"auto", "codex", "claude", "kimi"})


def _validate_recipe_fields(data: dict[str, Any]) -> None:
    """Validate that the parsed frontmatter contains the required fields."""
    missing_fields = [
        field_name for field_name in _REQUIRED_RECIPE_FIELDS if not data.get(field_name)
    ]
    if missing_fields:
        raise ValueError(
            "Loop recipe frontmatter is missing required field(s): "
            + ", ".join(missing_fields)
        )
    issue_type = data.get("issue_type", "feature")
    if issue_type not in _VALID_ISSUE_TYPES:
        raise ValueError(
            f"Loop recipe issue_type must be one of "
            f"{sorted(_VALID_ISSUE_TYPES)}; got {issue_type!r}."
        )
    agent_name = data.get("agent", "auto")
    if agent_name not in _VALID_AGENTS:
        raise ValueError(
            f"Loop recipe agent must be one of {sorted(_VALID_AGENTS)}; "
            f"got {agent_name!r}."
        )


# ---------------------------------------------------------------------------
# Public API: parse_loop_recipe
# ---------------------------------------------------------------------------


def parse_loop_recipe(recipe_path: Path) -> LoopRecipe:
    """Read and parse a loop recipe from disk.

    Args:
        recipe_path: Absolute or relative path to the recipe file.

    Returns:
        A :class:`LoopRecipe` ready to be rendered or registered.

    Raises:
        FileNotFoundError: When ``recipe_path`` does not exist.
        ValueError: When the frontmatter is malformed or invalid.
    """
    resolved = recipe_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Loop recipe not found: {resolved}")
    recipe_text = resolved.read_text(encoding="utf-8")
    return parse_loop_recipe_text(recipe_text, source_path=resolved)


def parse_loop_recipe_text(recipe_text: str, *, source_path: Path) -> LoopRecipe:
    """Parse a loop recipe from raw text.

    Args:
        recipe_text: Full recipe contents.
        source_path: Path used to anchor the recipe when stored.

    Returns:
        A :class:`LoopRecipe` populated from the frontmatter / body.

    Raises:
        ValueError: When the recipe is missing required fields or has an
            invalid value.
    """
    frontmatter, body = _split_frontmatter(recipe_text)
    _validate_recipe_fields(frontmatter.data)
    recipe_id = str(frontmatter.data["id"]).strip()
    if not _SLUG_PATTERN.match(recipe_id):
        raise ValueError(
            f"Loop recipe id {recipe_id!r} must be kebab-case (lowercase "
            "letters, digits, and hyphens; no leading/trailing hyphens)."
        )
    schedule_expression = str(frontmatter.data["schedule"]).strip()
    schedule = LoopSchedule.from_expression(schedule_expression)
    labels = _coerce_label_list(frontmatter.data.get("labels"))
    return LoopRecipe(
        id=recipe_id,
        schedule=schedule,
        repo_id=str(frontmatter.data["repo_id"]).strip(),
        body_template=body,
        source_path=source_path,
        issue_type=str(frontmatter.data.get("issue_type", "feature")),
        agent=str(frontmatter.data.get("agent", "auto")),
        labels=labels,
        publish_prd=bool(frontmatter.data.get("publish_prd", True)),
        queue_ready=bool(frontmatter.data.get("queue_ready", True)),
        run_now=bool(frontmatter.data.get("run_now", False)),
        pre_command=_coerce_optional_string(frontmatter.data.get("pre_command")),
        timezone_name=_coerce_optional_string(frontmatter.data.get("timezone")),
        priority=str(frontmatter.data.get("priority", "P2")),
        slug=_coerce_optional_string(frontmatter.data.get("slug")),
    )


_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _coerce_label_list(value: Any) -> tuple[str, ...]:
    """Coerce the frontmatter ``labels`` field into a tuple of strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        if not value:
            return ()
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ValueError(f"Unsupported labels value: {value!r}")


def _coerce_optional_string(value: Any) -> str | None:
    """Return ``None`` for empty / missing string fields."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


_VAR_PATTERN = re.compile(r"\{\{\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render_loop_recipe(
    recipe: LoopRecipe,
    *,
    fire_at: datetime,
    extra_variables: dict[str, str] | None = None,
) -> str:
    """Render the recipe body template into a PRD Markdown document.

    Built-in variables:

    - ``{{date}}``: ``YYYY-MM-DD`` of ``fire_at``.
    - ``{{timestamp}}``: ``YYYYMMDD-HHMMSS`` of ``fire_at``.
    - ``{{datetime}}``: ISO-8601 local time of ``fire_at``.
    - ``{{loop_id}}``: Recipe id.
    - ``{{repo_id}}``: Target repository id.

    Args:
        recipe: Parsed recipe whose ``body_template`` will be rendered.
        fire_at: Wall-clock time of the fire (used for the date variables).
        extra_variables: Optional additional variables (e.g. from
            ``pre_command`` output).

    Returns:
        Rendered PRD Markdown body (excluding the final file header).

    Raises:
        ValueError: When a template variable is referenced but not defined.
    """
    variables: dict[str, str] = {
        "date": fire_at.strftime("%Y-%m-%d"),
        "timestamp": fire_at.strftime("%Y%m%d-%H%M%S"),
        "datetime": fire_at.isoformat(timespec="seconds"),
        "loop_id": recipe.id,
        "repo_id": recipe.repo_id,
    }
    if extra_variables:
        variables.update(extra_variables)
    return _render_template(recipe.body_template, variables)


def render_loop_recipe_title(
    recipe: LoopRecipe,
    *,
    fire_at: datetime,
    extra_variables: dict[str, str] | None = None,
) -> str:
    """Render the recipe body template, returning only the H1 title.

    Loop recipes often open with a single ``#`` title line containing a
    ``{{date}}`` placeholder. This helper extracts and renders that line so
    callers can use the rendered title as the Issue title without the rest
    of the PRD body.

    Args:
        recipe: Parsed recipe whose ``body_template`` will be inspected.
        fire_at: Wall-clock time of the fire.
        extra_variables: Optional extra variables forwarded to
            :func:`render_loop_recipe`.

    Returns:
        Rendered H1 title without the leading ``#`` and surrounding whitespace.
        Falls back to the recipe id when the body has no H1.
    """
    rendered_body = render_loop_recipe(
        recipe, fire_at=fire_at, extra_variables=extra_variables
    )
    for line in rendered_body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return _strip_leading_prd_marker(stripped[2:].strip())
    return recipe.id


def _strip_leading_prd_marker(title: str) -> str:
    """Strip a leading ``PRD:`` / ``PRD：`` marker from a title."""
    if title.startswith("PRD:") or title.startswith("PRD："):
        return title.split(":", 1)[1].strip()
    return title


def _render_template(template_text: str, variables: dict[str, str]) -> str:
    """Replace ``{{name}}`` occurrences with values from ``variables``.

    Args:
        template_text: Source template body.
        variables: Map of variable name to its rendered string value.

    Returns:
        The rendered text with all variables replaced.

    Raises:
        ValueError: When a referenced variable has no value in
            ``variables``.
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group("name")
        if var_name not in variables:
            raise ValueError(
                f"Loop recipe template references undefined variable "
                f"{var_name!r}. Add it via pre_command or frontmatter."
            )
        return variables[var_name]

    return _VAR_PATTERN.sub(_replace, template_text)


# ---------------------------------------------------------------------------
# Pre-command output parsing
# ---------------------------------------------------------------------------


_KEY_VALUE_LINE_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)


def parse_pre_command_output(stdout: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines from a ``pre_command`` invocation.

    Blank lines and lines starting with ``#`` are ignored. Values are
    returned as raw strings; template rendering does no further coercion.

    Args:
        stdout: Captured stdout of the pre-command.

    Returns:
        Mapping of variable name to its value.
    """
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_VALUE_LINE_RE.match(line)
        if match is None:
            continue
        key = match.group("key").strip()
        value = match.group("value").strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        parsed[key] = value
    return parsed
