"""Evaluate PRD dependencies for the roadmap.

This module takes the dependency edges produced by
:mod:`roadmap_prd_scanner` and resolves them against the current GitHub
state (Issue closed, group members closed, PRD refs merged/archived).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import LabelConfig
from backend.core.shared.models.roadmap import (
    RoadmapDependencyKind,
    RoadmapPrd,
    RoadmapPrdState,
)

_logger = logging.getLogger(__name__)


def _is_issue_closed(
    issue_number: int,
    github_client: IGitHubClient,
) -> bool:
    """Return ``True`` if the GitHub Issue is closed."""
    try:
        issue = github_client.get_issue(issue_number)
    except Exception as exc:  # noqa: BLE001 - dependency evaluation stays resilient.
        _logger.info("Failed to look up dependency issue #%s: %s", issue_number, exc)
        return False
    return issue.state.upper() == "CLOSED"


def _is_group_fully_closed(
    group_name: str,
    github_client: IGitHubClient,
    labels_config: LabelConfig,
) -> bool:
    """Return ``True`` if every open issue with the group label is closed."""
    group_label = f"{labels_config.group_prefix}{group_name}"
    try:
        issues = github_client.list_issues_by_label(
            label=group_label, limit=1000, state="all"
        )
    except Exception as exc:  # noqa: BLE001
        _logger.info("Failed to list group %s issues: %s", group_label, exc)
        return False
    # If there are no issues at all, the group is considered unresolvable rather
    # than closed so that typos do not silently pass.
    if not issues:
        return False
    return all(issue.state.upper() == "CLOSED" for issue in issues)


def _detect_cycles(prds: Sequence[RoadmapPrd]) -> set[str]:
    """Detect PRD->PRD dependency cycles and return the involved PRD paths."""
    prd_to_index = {prd.prd_path: index for index, prd in enumerate(prds)}
    adjacency: dict[int, list[int]] = {index: [] for index in range(len(prds))}
    for prd in prds:
        source_index = prd_to_index[prd.prd_path]
        for dep in prd.delivery_dependencies:
            if dep.kind is RoadmapDependencyKind.PRD and dep.to_path in prd_to_index:
                adjacency[source_index].append(prd_to_index[dep.to_path])

    # Depth-first search with three-color marking.
    WHITE, GRAY, BLACK = 0, 1, 2
    colors = [WHITE] * len(prds)
    cycle_nodes: set[int] = set()

    def _visit(node: int, stack: list[int]) -> None:
        colors[node] = GRAY
        for neighbor in adjacency[node]:
            if colors[neighbor] == GRAY:
                # Found a cycle; mark nodes from the first occurrence onward.
                cycle_start = stack.index(neighbor)
                cycle_nodes.update(stack[cycle_start:])
            elif colors[neighbor] == WHITE:
                stack.append(neighbor)
                _visit(neighbor, stack)
                stack.pop()
        colors[node] = BLACK

    for node in range(len(prds)):
        if colors[node] == WHITE:
            _visit(node, [node])

    return {prds[index].prd_path for index in cycle_nodes}


def evaluate_roadmap_dependencies(
    prds: Sequence[RoadmapPrd],
    github_client: IGitHubClient,
    labels_config: LabelConfig,
) -> dict[str, str | None]:
    """Evaluate dependency satisfaction for each PRD.

    Args:
        prds: PRDs from the scanner.
        github_client: GitHub client for live state.
        labels_config: Label names configuration.

    Returns:
        Mapping from PRD path to block reason, or ``None`` if unblocked.
    """
    prd_by_path = {prd.prd_path: prd for prd in prds}
    cycle_paths = _detect_cycles(prds)
    block_reasons: dict[str, str | None] = {}

    # Process PRDs in topological order so upstream PRD state is resolved first.
    # For simplicity we do a BFS from zero-indegree PRDs, falling back to the
    # original order if the graph has cycles.
    in_degree: dict[str, int] = {prd.prd_path: 0 for prd in prds}
    for prd in prds:
        for dep in prd.delivery_dependencies:
            if dep.kind is RoadmapDependencyKind.PRD and dep.to_path in prd_by_path:
                in_degree[prd.prd_path] += 1

    queue: deque[str] = deque(
        [path for path, degree in in_degree.items() if degree == 0]
    )
    ordered_paths: list[str] = []
    while queue:
        path = queue.popleft()
        ordered_paths.append(path)
        for prd in prds:
            for dep in prd.delivery_dependencies:
                if dep.kind is RoadmapDependencyKind.PRD and dep.to_path == path:
                    in_degree[prd.prd_path] -= 1
                    if in_degree[prd.prd_path] == 0:
                        queue.append(prd.prd_path)

    # Append any remaining nodes (cycles) in original order.
    for prd in prds:
        if prd.prd_path not in ordered_paths:
            ordered_paths.append(prd.prd_path)

    for path in ordered_paths:
        prd = prd_by_path[path]
        if path in cycle_paths:
            block_reasons[path] = "PRD 依赖形成环，请检查 Delivery Dependencies"
            continue

        blockers: list[str] = []
        for dep in prd.delivery_dependencies:
            if dep.kind is RoadmapDependencyKind.ISSUE:
                issue_number = int(dep.to_path.lstrip("#"))
                if not _is_issue_closed(issue_number, github_client):
                    blockers.append(f"上游 Issue #{issue_number} 未关闭")
            elif dep.kind is RoadmapDependencyKind.GROUP:
                group_name = dep.to_path.split(":", 1)[1]
                if not _is_group_fully_closed(group_name, github_client, labels_config):
                    blockers.append(f"任务组 {group_name} 仍有未关闭 Issue")
            elif dep.kind is RoadmapDependencyKind.PRD:
                upstream = prd_by_path.get(dep.to_path)
                if upstream is None:
                    blockers.append(f"上游 PRD {dep.to_path} 不存在")
                elif upstream.state not in {
                    RoadmapPrdState.MERGED,
                    RoadmapPrdState.ARCHIVED,
                }:
                    blockers.append(f"等待上游 PRD {dep.to_path}")
            elif dep.kind is RoadmapDependencyKind.UNRESOLVED:
                blockers.append(dep.detail or "存在未解析的依赖")

        block_reasons[path] = "；".join(blockers) if blockers else None

    return block_reasons
