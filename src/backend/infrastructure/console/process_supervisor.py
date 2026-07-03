"""托管 runner 子进程的监管器实现。

设计要点：

- 子进程以 ``start_new_session=True`` 启动，脱离后端进程组：后端重启
  不会杀掉正在执行 Issue 的 runner。
- 进程登记表是一个 JSON pidfile（默认 ``~/.iar/processes.json``），
  写入采用「临时文件 + ``os.replace``」原子替换；后端重启后据此复活
  记录并重新探活。
- 探活使用 ``os.kill(pid, 0)``；进程不存在（``ProcessLookupError``）
  视为已退出。pid 复用的误判窗口由 started_at 记录辅助人工判断，
  极端情况下的精确防护见 PRD Risks。
- 停止流程：SIGTERM → 等待 ``timeout_seconds`` → 仍存活则 SIGKILL。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

_logger = logging.getLogger(__name__)

try:
    import psutil
except Exception:  # noqa: BLE001 - psutil 是可选依赖；不可用时降级为空扫描。
    psutil = None  # type: ignore[assignment]


# 以下数据类与 core/shared/interfaces/runner_console.py 中的同名类型
# 结构一致（鸭子类型实现端口），infrastructure 层禁止导入 core。
# ``kind`` 在本层为普通字符串；core 的 RunnerProcessKind 是 str Enum，
# 与字符串比较兼容。


@dataclass(frozen=True)
class RunnerProcessRecord:
    """一个被托管 runner 进程的状态快照（与 core 同构）。"""

    process_id: str
    repo_id: str
    kind: str
    pid: int
    status: str
    exit_code: int | None
    log_path: str
    command: tuple[str, ...]
    started_at: str
    stopped_at: str | None


@dataclass(frozen=True)
class ProcessLogChunk:
    """日志 offset 续读的一段内容（与 core 同构）。"""

    content: str
    next_offset: int
    eof: bool


#: 本进程内 spawn 的子进程句柄（pid → Popen）。
#: 必须保留句柄并通过 ``poll()`` 探活：子进程退出后在父进程存活期间
#: 是僵尸态，``os.kill(pid, 0)`` 会误判为存活；``poll()`` 同时完成 reap。
#: 模块级共享，使每请求新建的监管器实例也能探测到先前 spawn 的子进程。
_CHILD_PROCESSES: dict[int, subprocess.Popen] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _probe_pid(pid: int) -> tuple[bool, int | None]:
    """探测 pid 是否存活。

    Returns:
        (是否存活, 退出码)。退出码仅在 pid 是本进程子进程且已退出时已知。
    """
    if pid <= 0:
        return False, None
    child_handle = _CHILD_PROCESSES.get(pid)
    if child_handle is not None:
        exit_code = child_handle.poll()
        if exit_code is not None:
            _CHILD_PROCESSES.pop(pid, None)
            return False, exit_code
        return True, None
    try:
        os.kill(pid, 0)
        return True, None
    except ProcessLookupError:
        return False, None
    except PermissionError:
        # 进程存在但属于其他用户 —— 对本面板而言仍视为存活。
        return True, None


#: 需要识别的 runner 子命令到 supervisor 内部 kind 字符串的映射。
#: 顺序重要：review-daemon 必须在 daemon 之前检查，避免 "daemon" 子串误匹配。
_UNMANAGED_KIND_PATTERNS = (
    ("review-daemon", "review_daemon"),
    ("daemon", "daemon"),
)

#: 这些 runner 子命令下还带有非 runner 的嵌套子命令（如 ``daemon status``），
#: 不应被识别为常驻 runner 进程。
_NON_RUNNER_NESTED_COMMANDS = frozenset({"status"})


def _find_iar_command_index(cmdline: tuple[str, ...]) -> int | None:
    """在命令行中定位 ``iar`` 可执行文件的位置。

    兼容 ``iar``、``uv run iar``、``/path/to/iar`` 等形态。
    """
    for index, arg in enumerate(cmdline):
        basename = os.path.basename(arg)
        if basename == "iar":
            return index
    return None


def _parse_unmanaged_kind(cmdline: tuple[str, ...]) -> str | None:
    """从命令行解析 runner 子命令对应的 kind。

    Returns:
        ``"daemon"`` / ``"review_daemon"`` 或 ``None``。
    """
    iar_index = _find_iar_command_index(cmdline)
    if iar_index is None:
        return None
    candidate_args = cmdline[iar_index + 1 :]
    # 跳过选项参数（如 --repo /path/to/repo），定位子命令。
    for index, arg in enumerate(candidate_args):
        if arg.startswith("-"):
            continue
        for pattern, kind in _UNMANAGED_KIND_PATTERNS:
            if arg == pattern:
                # 检查是否是嵌套的非 runner 子命令（如 ``iar daemon status``）。
                for next_candidate in candidate_args[index + 1 :]:
                    if not next_candidate.startswith("-"):
                        if next_candidate in _NON_RUNNER_NESTED_COMMANDS:
                            return None
                        break
                return kind
        # 第一个非选项非 runner 子命令的参数说明不是目标进程。
        return None
    return None


def _parse_repo_id_from_argv(cmdline: tuple[str, ...]) -> str | None:
    """解析命令行中的 ``--repo-id`` 值。"""
    for index, arg in enumerate(cmdline):
        if arg == "--repo-id" and index + 1 < len(cmdline):
            return cmdline[index + 1]
    return None


def _resolve_repo_id_from_cwd(
    cwd: str | None,
    registry_entries: Sequence[Any],
) -> str | None:
    """用进程 cwd 匹配 registry 中的仓库路径，返回 repo_id。

    Args:
        cwd: 进程当前工作目录。
        registry_entries: 与 ``RegistryRepositoryEntry`` 同构的对象序列，
            要求每个条目有 ``repo_id`` 和 ``path`` 属性。
    """
    if not cwd:
        return None
    try:
        cwd_path = Path(cwd).resolve()
    except (OSError, ValueError):
        return None
    for entry in registry_entries:
        try:
            entry_path = Path(str(getattr(entry, "path", ""))).expanduser().resolve()
        except (OSError, ValueError):
            continue
        if cwd_path == entry_path:
            return getattr(entry, "repo_id", None)
    return None


def _format_create_time(create_time: float | None) -> str:
    """把 psutil 的 create_time 转成 ISO 字符串；不可用则返回空字符串。"""
    if create_time is None:
        return ""
    try:
        return datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat(timespec="seconds")
    except (OSError, ValueError, OverflowError):
        return ""


class PidfileProcessSupervisor:
    """``IRunnerProcessSupervisor`` 端口的 subprocess + JSON pidfile 实现（鸭子类型）。"""

    def __init__(self, *, registry_path: str | Path, log_dir: str | Path) -> None:
        """初始化监管器。

        Args:
            registry_path: JSON pidfile 路径，支持 ``~`` 展开。
            log_dir: 托管进程日志根目录。
        """
        self._registry_path = Path(registry_path).expanduser()
        self._log_dir = Path(log_dir).expanduser()

    # ── registry 持久化 ────────────────────────────────────────────────

    def _load_registry(self) -> dict[str, dict]:
        if not self._registry_path.exists():
            return {}
        try:
            raw_text = self._registry_path.read_text(encoding="utf-8")
            loaded = json.loads(raw_text or "{}")
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("Failed to read process registry %s: %s", self._registry_path, exc)
            return {}

    def _save_registry(self, registry_entries: dict[str, dict]) -> None:
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._registry_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(registry_entries, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, self._registry_path)

    @staticmethod
    def _record_from_entry(entry: dict) -> RunnerProcessRecord:
        return RunnerProcessRecord(
            process_id=entry["process_id"],
            repo_id=entry["repo_id"],
            kind=str(entry["kind"]),
            pid=int(entry["pid"]),
            status=entry["status"],
            exit_code=entry.get("exit_code"),
            log_path=entry["log_path"],
            command=tuple(entry.get("command", ())),
            started_at=entry["started_at"],
            stopped_at=entry.get("stopped_at"),
        )

    @staticmethod
    def _entry_from_record(record: RunnerProcessRecord) -> dict:
        entry = asdict(record)
        entry["command"] = list(record.command)
        return entry

    def _refresh_record(self, record: RunnerProcessRecord) -> RunnerProcessRecord:
        """对 running 记录做存活探测，已死亡的标记为 exited。"""
        if record.status != "running":
            return record
        alive, exit_code = _probe_pid(record.pid)
        if alive:
            return record
        return replace(record, status="exited", exit_code=exit_code, stopped_at=_now_iso())

    # ── 端口实现 ───────────────────────────────────────────────────────

    def spawn(
        self,
        *,
        repo_id: str,
        kind: object,
        argv: Sequence[str],
        cwd: Path,
    ) -> RunnerProcessRecord:
        """启动并登记一个托管 runner 子进程。

        Args:
            repo_id: 目标仓库 ID。
            kind: 进程类型；接受 core 的 str Enum 或普通字符串。
            argv: 完整命令参数序列。
            cwd: 子进程工作目录。
        """
        kind_value = str(getattr(kind, "value", kind))
        process_id = uuid.uuid4().hex[:12]
        log_directory = self._log_dir / repo_id
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / f"{kind_value}-{process_id}.log"

        # IAR_CONSOLE 标记让子进程把运行记录的 trigger 记为 console_*。
        child_env = dict(os.environ)
        child_env["IAR_CONSOLE"] = "1"
        with open(log_path, "ab") as log_file:
            child_process = subprocess.Popen(  # noqa: S603 - argv 由白名单枚举构建。
                list(argv),
                cwd=str(cwd),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=child_env,
            )

        _CHILD_PROCESSES[child_process.pid] = child_process

        record = RunnerProcessRecord(
            process_id=process_id,
            repo_id=repo_id,
            kind=kind_value,
            pid=child_process.pid,
            status="running",
            exit_code=None,
            log_path=str(log_path),
            command=tuple(argv),
            started_at=_now_iso(),
            stopped_at=None,
        )
        registry_entries = self._load_registry()
        registry_entries[process_id] = self._entry_from_record(record)
        self._save_registry(registry_entries)
        _logger.info(
            "Spawned runner process %s (%s/%s) pid=%d log=%s",
            process_id,
            repo_id,
            kind_value,
            child_process.pid,
            log_path,
        )
        return record

    def list_processes(self) -> list[RunnerProcessRecord]:
        """列出全部登记进程并刷新存活状态（结果写回 registry）。"""
        registry_entries = self._load_registry()
        refreshed_records: list[RunnerProcessRecord] = []
        registry_changed = False
        for process_id, entry in registry_entries.items():
            try:
                record = self._record_from_entry(entry)
            except (KeyError, ValueError) as exc:
                _logger.warning("Dropping corrupt process registry entry %s: %s", process_id, exc)
                registry_changed = True
                continue
            refreshed = self._refresh_record(record)
            if refreshed != record:
                registry_entries[process_id] = self._entry_from_record(refreshed)
                registry_changed = True
            refreshed_records.append(refreshed)
        if registry_changed:
            valid_ids = {record.process_id for record in refreshed_records}
            self._save_registry(
                {
                    pid_key: entry
                    for pid_key, entry in registry_entries.items()
                    if pid_key in valid_ids
                }
            )
        refreshed_records.sort(key=lambda record: record.started_at, reverse=True)
        return refreshed_records

    def list_unmanaged_processes(
        self, registry_entries: Sequence[Any]
    ) -> list[RunnerProcessRecord]:
        """扫描系统进程，返回未在 pidfile 中登记的 iar daemon / review-daemon。

        仅返回当前用户拥有的进程；命令行无法解析或不属于 registry 的进程
        被忽略。结果仅用于观测，不参与 ``stop`` / ``read_log`` 等托管操作。

        Args:
            registry_entries: 与 ``RegistryRepositoryEntry`` 同构的对象序列，
                每个条目至少包含 ``repo_id`` 与 ``path`` 属性，用于按 cwd
                匹配未显式指定 ``--repo-id`` 的手动进程。
        """
        if psutil is None:
            _logger.warning("psutil is not available; cannot scan for unmanaged runner processes.")
            return []

        # 收集已托管进程的 pid，避免重复报告。
        registry = self._load_registry()
        managed_pids: set[int] = set()
        for entry in registry.values():
            try:
                managed_pids.add(int(entry["pid"]))
            except (KeyError, TypeError, ValueError):
                continue

        try:
            current_username = psutil.Process().username()
        except Exception:  # noqa: BLE001 - 用户名读取失败时不阻断扫描。
            current_username = None
        current_pid = os.getpid()

        unmanaged_records: list[RunnerProcessRecord] = []
        for proc in psutil.process_iter(["pid", "name", "cmdline", "username", "create_time"]):
            try:
                proc_info = proc.info
                if not isinstance(proc_info, dict):
                    continue
                pid = proc_info.get("pid")
                username = proc_info.get("username")
                cmdline = proc_info.get("cmdline")
                if not isinstance(pid, int) or pid <= 0:
                    continue
                if pid == current_pid:
                    continue
                if current_username is not None and username != current_username:
                    continue
                if not isinstance(cmdline, (list, tuple)) or not cmdline:
                    continue
                cmdline_tuple = tuple(str(arg) for arg in cmdline)
                kind = _parse_unmanaged_kind(cmdline_tuple)
                if kind is None:
                    continue
                if pid in managed_pids:
                    continue
                repo_id = _parse_repo_id_from_argv(cmdline_tuple)
                if repo_id is None:
                    try:
                        cwd = proc.cwd()
                    except Exception:  # noqa: BLE001 - cwd 读取失败时跳过按路径匹配。
                        cwd = None
                    repo_id = _resolve_repo_id_from_cwd(cwd, registry_entries)
                if repo_id is None:
                    continue
                unmanaged_records.append(
                    RunnerProcessRecord(
                        process_id=f"unmanaged-{pid}",
                        repo_id=repo_id,
                        kind=kind,
                        pid=pid,
                        status="running",
                        exit_code=None,
                        log_path="",
                        command=cmdline_tuple,
                        started_at=_format_create_time(proc_info.get("create_time")),
                        stopped_at=None,
                    )
                )
            except Exception:  # noqa: BLE001 - 单个进程扫描失败不应中断整体。
                continue

        return unmanaged_records

    def get_process(self, process_id: str) -> RunnerProcessRecord | None:
        """按 ID 查询单个进程的最新状态。"""
        registry_entries = self._load_registry()
        entry = registry_entries.get(process_id)
        if entry is None:
            return None
        record = self._refresh_record(self._record_from_entry(entry))
        if record != self._record_from_entry(entry):
            registry_entries[process_id] = self._entry_from_record(record)
            self._save_registry(registry_entries)
        return record

    def stop(self, process_id: str, *, timeout_seconds: int) -> RunnerProcessRecord:
        """SIGTERM → 等待 → SIGKILL 停止进程并更新登记。"""
        registry_entries = self._load_registry()
        entry = registry_entries.get(process_id)
        if entry is None:
            raise KeyError(f"Process '{process_id}' is not registered.")
        record = self._refresh_record(self._record_from_entry(entry))
        if record.status != "running":
            registry_entries[process_id] = self._entry_from_record(record)
            self._save_registry(registry_entries)
            return record

        final_status = "stopped"
        final_exit_code: int | None = None
        try:
            os.kill(record.pid, signal.SIGTERM)
            deadline = time.monotonic() + max(timeout_seconds, 1)
            while time.monotonic() < deadline:
                alive, final_exit_code = _probe_pid(record.pid)
                if not alive:
                    break
                time.sleep(0.2)
            else:
                alive, final_exit_code = _probe_pid(record.pid)
            if alive:
                os.kill(record.pid, signal.SIGKILL)
                final_status = "killed"
                child_handle = _CHILD_PROCESSES.pop(record.pid, None)
                if child_handle is not None:
                    try:
                        final_exit_code = child_handle.wait(timeout=5)
                    except Exception:  # noqa: BLE001 - reap is best effort.
                        final_exit_code = None
        except ProcessLookupError:
            final_status = "exited"

        stopped_record = replace(
            record,
            status=final_status,
            exit_code=final_exit_code,
            stopped_at=_now_iso(),
        )
        registry_entries[process_id] = self._entry_from_record(stopped_record)
        self._save_registry(registry_entries)
        _logger.info(
            "Stopped runner process %s pid=%d status=%s",
            process_id,
            record.pid,
            final_status,
        )
        return stopped_record

    def read_log(self, process_id: str, *, offset: int, max_bytes: int) -> ProcessLogChunk:
        """从指定偏移量续读进程日志文件。"""
        registry_entries = self._load_registry()
        entry = registry_entries.get(process_id)
        if entry is None:
            raise KeyError(f"Process '{process_id}' is not registered.")
        log_path = Path(entry["log_path"])
        if not log_path.exists():
            return ProcessLogChunk(content="", next_offset=0, eof=True)
        safe_offset = max(offset, 0)
        with open(log_path, "rb") as log_file:
            log_file.seek(safe_offset)
            raw_chunk = log_file.read(max(max_bytes, 1))
            next_offset = log_file.tell()
            eof = log_file.read(1) == b""
        return ProcessLogChunk(
            content=raw_chunk.decode("utf-8", errors="replace"),
            next_offset=next_offset,
            eof=eof,
        )
