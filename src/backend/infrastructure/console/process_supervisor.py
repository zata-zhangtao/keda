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
from typing import Sequence

_logger = logging.getLogger(__name__)


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
            _logger.warning(
                "Failed to read process registry %s: %s", self._registry_path, exc
            )
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
        return replace(
            record, status="exited", exit_code=exit_code, stopped_at=_now_iso()
        )

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
                _logger.warning(
                    "Dropping corrupt process registry entry %s: %s", process_id, exc
                )
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

    def read_log(
        self, process_id: str, *, offset: int, max_bytes: int
    ) -> ProcessLogChunk:
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
