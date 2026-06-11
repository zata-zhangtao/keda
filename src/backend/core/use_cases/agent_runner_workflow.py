"""Agent Runner workflow state helpers.

提供 phase-agnostic 的 workflow label 互斥转换和 marker history 判断，
供 CI rework recovery、forbidden blocked resolution 等后续恢复任务复用。
"""

from __future__ import annotations

from backend.core.shared.interfaces.agent_runner import IGitHubClient
from backend.core.shared.models.agent_runner import AppConfig, ReviewEventMarker
from backend.core.use_cases.agent_runner_events import _parse_event_marker


def workflow_state_labels(config: AppConfig) -> list[str]:
    """返回所有 durable workflow state labels。

    这些标签代表 Issue 的生命周期状态，状态切换时会从当前 labels 中
    移除其他 workflow labels，只保留目标标签。
    """
    return [
        config.labels.ready,
        config.labels.running,
        config.labels.supervising,
        config.labels.review,
        config.labels.failed,
        config.labels.blocked,
    ]


def build_transition_labels(
    current_labels: tuple[str, ...],
    config: AppConfig,
    target_label: str,
) -> list[str]:
    """计算状态切换后的 labels 列表。

    规则：
    - 添加 target_label
    - 移除所有其他 durable workflow labels
    - 保留非 workflow labels（如 agent routing labels、task-group labels）

    Args:
        current_labels: 当前 Issue 的所有 labels
        config: 应用配置
        target_label: 目标 workflow state label

    Returns:
        切换后应设置的完整 labels 列表
    """
    durable = set(workflow_state_labels(config))
    preserved = [label for label in current_labels if label not in durable]
    if target_label not in preserved:
        preserved.append(target_label)
    return preserved


def transition_issue_workflow_state(
    github_client: IGitHubClient,
    issue_number: int,
    config: AppConfig,
    target_label: str,
) -> tuple[bool, list[str]]:
    """将 Issue 切换到目标 workflow state，保持 label 互斥。

    先读取当前 labels，计算应保留的非 workflow labels，然后执行更新。

    Args:
        github_client: GitHub 客户端
        issue_number: Issue 编号
        config: 应用配置
        target_label: 目标 workflow state label

    Returns:
        (是否成功, 最终 labels 列表)。成功时返回更新后的 labels。
    """
    issue = github_client.get_issue(issue_number)
    new_labels = build_transition_labels(issue.labels, config, target_label)
    github_client.edit_issue_labels(
        issue_number,
        add=new_labels,
        remove=[label for label in issue.labels if label not in new_labels],
    )
    return True, new_labels


def find_latest_unconsumed_marker(
    comments: list[str],
    phase: str,
    completion_phases: set[str],
) -> ReviewEventMarker | None:
    """查找最近的、尚未被完成事件消费的指定 phase marker。

    从评论列表倒序扫描 event markers。如果找到目标 phase，则检查其后
    是否出现了 completion phases 中的任一 phase；若出现，则视为已消费。

    Args:
        comments: Issue 评论正文列表（按时间顺序）
        phase: 目标 marker phase（如 ``post_pr_rework_requested``）
        completion_phases: 表示消费完成的 phase 集合

    Returns:
        未消费的 marker，或 None
    """
    has_later_completion = False
    for comment_body in reversed(comments):
        marker = _parse_event_marker(comment_body)
        if marker is None:
            continue
        if marker.phase == phase:
            if has_later_completion:
                return None
            return marker
        if marker.phase in completion_phases:
            has_later_completion = True
    return None


def claim_blocked_issue(
    github_client: IGitHubClient,
    issue_number: int,
    config: AppConfig,
) -> bool:
    """尝试竞争认领一个 blocked Issue，将其切换到 running。

    采用读-检查-写模式（best-effort CAS）：先读取当前 labels，确认仍是
    blocked；然后执行 transition；最后重新读取 labels，确认已成功切换为
    running 且不再包含 blocked。二次确认失败时返回 False。

    .. note::
        这不是真正的 compare-and-swap。在极高并发下存在 TOCTOU 窗口：
        两个 runner 可能同时读取到 blocked 状态并同时写入 running。
        二次读取确认可将该窗口缩到极小，但无法彻底消除。后续应通过
        ``If-Match`` ETag 实现真正的原子 label 更新。

    Args:
        github_client: GitHub 客户端
        issue_number: Issue 编号
        config: 应用配置

    Returns:
        是否成功认领
    """
    issue_before = github_client.get_issue(issue_number)
    if config.labels.blocked not in issue_before.labels:
        return False

    transition_issue_workflow_state(
        github_client, issue_number, config, config.labels.running
    )

    issue_after = github_client.get_issue(issue_number)
    return (
        config.labels.running in issue_after.labels
        and config.labels.blocked not in issue_after.labels
    )
