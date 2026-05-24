# PRD: iar run-once 日志落盘与终端时间戳

- GitHub Issue: https://github.com/zata-zhangtao/keda/issues/27

## 1. Introduction & Goals

当前执行 `uv run iar run-once` 时，终端能看到 agent 实时输出，但存在两个明显缺陷：

1. **无日志落盘**：`cli.py` 使用 `logging.basicConfig()` 仅配置了控制台 StreamHandler，未挂载 FileHandler；`process_runner.py` 和 `factory.py` 中的 agent 实时输出使用裸 `print()`，完全绕过 logging 系统。因此日志文件中缺少 CLI 运行记录和 agent 实时输出。
2. **无时间戳**：裸 `print()` 的终端输出不带时间戳，用户无法判断某段输出（尤其是工具调用、结果返回）发生在何时。

本 PRD 目标：让 `iar` 系列命令的终端输出带时间戳，同时关键运行事件和 agent 输出被持久化到按日期命名的日志文件（如 `logs/app-2026-05-24.log`），实现可观测、可回溯的运行记录。

### 可衡量目标

- `uv run iar run-once` 执行后，`logs/app-YYYY-MM-DD.log` 包含带时间戳的运行事件（如开始处理 issue、工具调用、结果返回、错误、完成等）。
- 终端输出的关键结构化事件（`[agent tool]`、`[agent result]`、`[agent error]`）带有时间戳前缀。
- 流式文本输出（text_delta）保持实时体验，但按逻辑事件边界记录到日志文件。
- `just lint` 和 `pytest` 继续通过。
- 向后兼容：不改变 `IProcessRunner` 接口签名，不影响 `capture_output=True` 分支的行为。

### Real CLI Validation Checklist

- [ ] 本机确认至少一个 agent（`claude`、`codex` 或 `kimi`）可执行且可以发起请求；任一不可用时记录阻塞或跳过原因。
- [ ] 在交互式 TTY 中执行真实命令 `uv run iar run-once --dry-run`（或实际 issue 命令），不使用 fake runner、mock 或替代 provider。
- [ ] 命令运行期间检查终端输出的 `[agent tool]`、`[agent result]`、`[agent error]` 等结构化事件行首带有 `HH:MM:SS` 时间戳。
- [ ] 命令结束后检查 `logs/app-YYYY-MM-DD.log` 存在本次运行记录，包含带时间戳的结构化事件和 agent 输出摘要。
- [ ] 执行真实 Codex/Kimi 路径（默认 `run-once` agent 为 `auto`，无路由标签时解析为 Codex），检查终端实时输出仍可见且带有时间戳。
- [ ] 命令结束后检查 `logs/app-YYYY-MM-DD.log` 中无重复日志条目（同一事件只出现一次）。

### Supporting Automated Checks

- [ ] 执行 `uv run pytest tests/test_process_runner.py -q`，验证 Claude stream 关键事件产生 logging record、text_delta 在 message_stop 边界汇总记录、非 Claude 路径的 pipe relay 行为正确。
- [ ] 执行 `uv run pytest tests/test_agent_runner_cli.py -q`，验证 CLI 启动后 root logger 具备 FileHandler 且 handler 不重复。
- [ ] 执行 `uv run pytest tests/test_logger.py -q`，验证 Logger 单例行为正确。
- [ ] 执行 `just test`，确认仓库现有自动化测试全部通过。
- [ ] 执行 `uv run mkdocs build --strict`，确认文档构建通过。
- [ ] 本组检查只能证明实现契约和回归安全，不能替代 `### Real CLI Validation Checklist` 的真实 agent 端到端验证。

## 2. Requirement Shape

| 维度 | 要求 |
|---|---|
| 执行者 | 运行 `iar run-once`、`iar deliberate`、`iar daemon` 的开发者 |
| 触发条件 | CLI 启动后，agent 子进程产生实时 stdout 输出 |
| 期望行为 | 终端可见带时间戳的结构化事件；`logs/app-YYYY-MM-DD.log` 持久化关键事件与 agent 输出摘要 |
| 范围边界 | 仅修改日志初始化与输出通道；不改动 agent 调用协议、子进程执行方式、`IProcessRunner` 接口 |

具体行为变化：

- 当前 `cli.py` 使用 `logging.basicConfig()` 仅配置控制台日志，无 FileHandler。
- 当前 `process_runner.py` 的 `run_filtered_claude_stream()` 和 `factory.py` 的 deliberation runner 使用裸 `print()`，不进日志。
- 当前 `SubprocessRunner.run()` 的非 Claude 路径使用 `stdout=None`，子进程直接写入 OS 终端，完全绕过 Python。
- 目标状态是所有 uncaptured agent 输出都经过 Python 中继，终端带时间戳，同时写入 `logs/app-YYYY-MM-DD.log`。

## 3. Repository Context And Architecture Fit

### 3.1 当前相关模块

```text
src/backend/api/cli.py                          # CLI 入口，当前使用 logging.basicConfig()
src/backend/engines/agent_runner/factory.py     # 工厂层，包含 SubprocessTranscriptRunner、_run_agent_with_stdin_prompt
src/backend/infrastructure/process_runner.py    # SubprocessRunner、run_filtered_claude_stream、ClaudeStreamRenderer
src/backend/infrastructure/logging/logger.py    # Logger 单例，已配置按日期命名的 FileHandler + StreamHandler
```

### 3.2 根因分析

- `cli.py` 使用 `logging.basicConfig(level=INFO, format="...")`，仅配置了 root logger 的 `StreamHandler`（控制台），**没有 `FileHandler`**。
- `backend.infrastructure.logging.logger.Logger` 单例已配置按日期命名的 `FileHandler`（`logs/app-YYYY-MM-DD.log`）+ `StreamHandler`，但 `cli.py` **未使用该单例**，导致文件日志完全缺失。
- `process_runner.py` 中 `run_filtered_claude_stream()` 使用裸 `print(rendered_text, end="", flush=True)`，**完全绕过 logging 系统**。
- `factory.py` 中 `SubprocessTranscriptRunner.run()` 和 `_run_agent_with_stdin_prompt()` 使用裸 `print(line, end="")`，**不进日志**。
- **`SubprocessRunner.run()` 的非 Claude 路径**（`capture_output=False` 时 `else` 分支）使用 `stdout=None`，子进程输出直接继承父进程 stdout，**完全绕过 Python 进程**。

### 3.3 架构约束

- `api/` 可导入 `core/` 和 `engines/`，**禁止直接导入 `infrastructure/`**。
- `process_runner.py` 位于 `infrastructure/` 层，可以自由使用标准库 `logging`。
- `factory.py` 位于 `engines/` 层，可导入 `infrastructure/`，适合作为日志配置的 bridge。
- `capture_output=True` 分支的 stdout/stderr 可能被上层解析（如 review/supervisor 路径），**不能加时间戳或日志前缀污染**。

## 4. Recommendation

### 4.1 Recommended Approach

采用 **"统一日志初始化 + Agent 输出 Tee + 非 Claude 路径 PIPE 中继"** 三层修复：

**第一层：统一 CLI 日志初始化**
- `cli.py` 废弃 `logging.basicConfig()`。
- 在 `factory.py` 中新增 `configure_cli_logging()` 函数，内部复用 `backend.infrastructure.logging.logger.Logger` 单例。
- 将 `Logger` 单例的 handlers 配置到 **root logger**，但避免处理器重复：在附加前按处理器类型去重，或设置 `"app"` logger 的 `propagate=False`。
- `cli.py` 在 `main()` 开头调用 `configure_cli_logging()`。
- 这样所有模块的 `logging.getLogger(__name__)` 都会自动 propagate 到 root logger，同时写入文件和控制台。

**第二层：非 Claude 路径 PIPE 中继（首要修复）**
- `SubprocessRunner.run()` 的 `else` 分支（`capture_output=False`，非 Claude agent）当前使用 `stdout=None`。
- **改为 `stdout=subprocess.PIPE`**，然后添加中继循环：逐行读取子进程输出，打印带时间戳的内容到终端，同时通过 `logger.info()` 记录到日志。
- 这是最高优先级的代码变更，因为默认的 `run-once`（Codex/Kimi）走此路径，当前完全无法被 Python 端的任何日志机制触及。

**第三层：Agent 实时输出日志化与时间戳**
- 在 `process_runner.py` 引入模块级 `_logger = logging.getLogger(__name__)`。
- **`run_filtered_claude_stream()`**：
  - 终端实时输出保持 `print()`（保证流式体验）。
  - 关键结构化事件（`[agent tool]`、`[agent result]`、`[agent error]`）通过 `_logger.info()` 记录，自动获得时间戳和文件落盘。
  - 流式文本增量（text_delta）在 `message_stop` 边界处汇总，通过 `_logger.info("Agent output: %s", text_buffer)` 整句记录。
  - 引入 `_TimestampedPrinter` 辅助类，为所有中继输出行添加行首时间戳，正确处理结构化事件中的前导 `\n`（不能跳过以 `\n` 开头的字符串）。
- **`SubprocessTranscriptRunner.run()` 与 `_run_agent_with_stdin_prompt()`**（Kimi / Codex 的 deliberation 路径）：
  - 当前 `print(line, end="")` 改为通过辅助函数，同时 `print()` 到终端和 `_logger.info()` 到日志。
  - 由于这些 agent 的输出天然以行为单位，直接逐行记录不会碎片化。

**为什么这是最佳方案**：
- 最小变更面：不改动接口，只改日志初始化和输出通道。
- 架构合规：`api/` 不直接接触 `infrastructure/`，通过 `factory.py` bridge。
- 体验无损：text_delta 仍实时流式输出，时间戳只在行首/事件边界出现。
- 复用已有设施：`Logger` 单例的日志格式和文件写入能力无需重建，只需改为按日期生成文件名。
- 修复首要缺陷：非 Claude 路径不再绕过 Python。

### 4.2 Alternatives Considered

| 方案 | 说明 | 拒绝原因 |
|------|------|----------|
| 在 `cli.py` 直接添加 `FileHandler` | 绕过 `Logger` 单例，手动构造文件路径 | 重复已有 `logger.py` 的旋转逻辑，且 `api/` 不应关心日志文件路径 |
| 把所有 `print()` 换成 `logger.info()` | 包括 text_delta 碎片 | text_delta 逐字/逐词到达，会产生大量带时间戳的碎片化日志，终端体验极差 |
| 新增 `IProcessRunner` 的 `log_sink` 参数 | 通过接口注入日志回调 | 改动面过大，需要修改所有测试 mock 和调用方，收益不足 |
| 仅记录原始 JSON stream | 把 `output_line` 原样写日志 | 人可读性差，且不符合用户"日志记录"的直观预期 |
| 仅修改 `logging.basicConfig` | 只给 root logger 加 FileHandler | agent 子进程实时 `print()` 和 `stdout=None` 仍不会进入文件日志 |
| 在 shell 层用 `| ts | tee` | 建议用户手动处理 | 无法覆盖无人值守 daemon 和 Python 内部错误日志 |

## 5. Implementation Guide

This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

### 5.1 Core Logic

**日志文件按日期划分与过期清理：**

```python
# backend/infrastructure/logging/logger.py
import glob
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from backend.infrastructure.config.settings import config


class Logger:
    """Singleton logger manager with daily log files."""

    # ... existing singleton boilerplate ...

    def _setup_logger(self) -> None:
        self._logger = logging.getLogger(config.app_name)
        self._logger.setLevel(getattr(logging, config.log_level))

        if self._logger.handlers:
            return

        formatter = logging.Formatter(
            fmt="%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, config.log_level))
        console_handler.setFormatter(formatter)
        if hasattr(console_handler.stream, "reconfigure"):
            try:
                console_handler.stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        self._logger.addHandler(console_handler)

        try:
            log_dir = Path(config.log_file).parent
            log_dir.mkdir(parents=True, exist_ok=True)

            today = datetime.now().strftime("%Y-%m-%d")
            log_path = log_dir / f"app-{today}.log"

            file_handler = logging.FileHandler(
                filename=str(log_path),
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, config.log_level))
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)

            # Clean up logs older than 14 days
            self._cleanup_old_logs(log_dir, keep_days=14)
        except (OSError, PermissionError) as error:
            print(f"Warning: 无法创建日志文件处理器: {error}")

    def _cleanup_old_logs(self, log_dir: Path, keep_days: int) -> None:
        """Remove log files older than keep_days."""
        cutoff = datetime.now() - timedelta(days=keep_days)
        for path in log_dir.glob("app-*.log"):
            try:
                # Extract date from filename like app-2026-05-24.log
                date_str = path.stem.replace("app-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    os.remove(path)
            except (ValueError, OSError):
                pass
```

**日志初始化修复：**

```python
# backend/engines/agent_runner/factory.py
import logging


def configure_cli_logging() -> None:
    """Configure root logger with handlers from Logger singleton."""
    from backend.infrastructure.logging.logger import Logger

    app_logger = Logger().get_logger()
    root_logger = logging.getLogger()
    root_logger.setLevel(app_logger.level)

    # Avoid duplicate handlers: deduplicate by handler type before adding
    existing_types = {type(h) for h in root_logger.handlers}
    for handler in app_logger.handlers:
        if type(handler) not in existing_types:
            root_logger.addHandler(handler)
            existing_types.add(type(handler))

    # Prevent double-logging from "app" logger propagation
    app_logger.propagate = False
```

```python
# backend/api/cli.py
def main(argv: list[str] | None = None) -> int:
    from backend.engines.agent_runner.factory import configure_cli_logging

    configure_cli_logging()
    # Remove old logging.basicConfig(...)
    parsed = build_parser().parse_args(argv)
    ...
```

**非 Claude 路径 PIPE 中继：**

```python
# backend/infrastructure/process_runner.py
# In SubprocessRunner.run(), replace the else branch:
else:
    process = subprocess.Popen(
        list(command),
        ...
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    for line in process.stdout:
        if line:
            timestamped = _format_timestamped_line(line)
            print(timestamped, end="")
            _logger.info("%s", line.rstrip("\n"))
            stdout_lines.append(line)
    for line in process.stderr:
        if line:
            timestamped = _format_timestamped_line(line)
            print(timestamped, end="", file=sys.stderr)
            _logger.warning("%s", line.rstrip("\n"))
            stderr_lines.append(line)
    return_code = process.wait()
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
```

**Agent 输出 Tee 与时间戳：**

```python
# backend/infrastructure/process_runner.py
import logging
import sys
from datetime import datetime

_logger = logging.getLogger(__name__)


def _format_timestamped_line(text: str) -> str:
    """Prefix each line with HH:MM:SS timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    lines = text.split("\n")
    result = []
    for i, line in enumerate(lines):
        prefix = f"[{ts}] " if line else ""
        if i == len(lines) - 1:
            result.append(f"{prefix}{line}")
        else:
            result.append(f"{prefix}{line}\n")
    return "".join(result)


def run_filtered_claude_stream(...):
    renderer = ClaudeStreamRenderer()
    text_buffer: list[str] = []
    max_buffer_size = 4096  # Prevent unbounded growth
    ...
    for output_line in process.stdout:
        rendered_text = renderer.render_line(output_line)
        if collect_stdout and rendered_text:
            stdout_lines.append(rendered_text)

        if rendered_text:
            timestamped = _format_timestamped_line(rendered_text)
            print(timestamped, end="", flush=True)

            # Structured events go straight to logger
            if "[agent tool]" in rendered_text or "[agent result]" in rendered_text or "[agent error]" in rendered_text:
                _logger.info("%s", rendered_text.strip())
            else:
                text_buffer.append(rendered_text)
                buffered_text = "".join(text_buffer)
                if rendered_text.endswith("\n") or len(buffered_text) >= max_buffer_size:
                    stripped = buffered_text.strip()
                    if stripped:
                        _logger.info("Agent output: %s", stripped)
                    text_buffer.clear()
    if text_buffer:
        buffered = "".join(text_buffer).strip()
        if buffered:
            _logger.info("Agent output: %s", buffered)
    ...
```

### 5.2 Affected Files

#### 修改文件

```text
src/backend/api/cli.py
  [修改]
  【总结】废弃 logging.basicConfig()，改用 factory.configure_cli_logging()

  └── main() 开头调用 configure_cli_logging()，移除 basicConfig

src/backend/infrastructure/logging/logger.py
  [修改]
  【总结】将 TimedRotatingFileHandler 改为按日期命名的 FileHandler，启动时清理超过 14 天的旧日志

  ├── _setup_logger()：filename 从固定 app.log 改为 app-YYYY-MM-DD.log
  ├── 新增 _cleanup_old_logs()，删除超过 keep_days 的 app-*.log 文件
  └── 保留 UTF-8 编码和现有日志格式

src/backend/engines/agent_runner/factory.py
  [修改]
  【总结】新增 configure_cli_logging() bridge 函数；transcript runner 输出增加日志记录

  ├── 新增 configure_cli_logging()，复用 Logger 单例配置 root logger
  ├── 设置 app_logger.propagate = False 避免双重日志
  ├── SubprocessTranscriptRunner.run()：print(line, end="") 同时 logger.info()
  └── _run_agent_with_stdin_prompt()：print(line, end="") 同时 logger.info()

src/backend/infrastructure/process_runner.py
  [修改]
  【总结】引入模块级 logger，为 Agent 实时输出增加文件落盘和终端时间戳；非 Claude 路径改为 PIPE 中继

  ├── 新增 _logger = logging.getLogger(__name__)
  ├── 新增 _format_timestamped_line() 辅助函数（正确处理前导 \n）
  ├── SubprocessRunner.run()：非 Claude 路径 stdout=None -> subprocess.PIPE + 中继循环
  ├── run_filtered_claude_stream()：print() -> 带时间戳 print() + logger 记录
  └── text_delta 增加 max_buffer_size 限制防止内存无限增长

tests/test_process_runner.py
  [修改/新增]
  【总结】验证 run_filtered_claude_stream 的日志输出行为；验证非 Claude 路径 PIPE 中继

  ├── 断言关键事件（tool use, result）产生对应 logging record
  ├── 断言 text_delta 在 message_stop 后汇总记录
  ├── 断言 text_delta 缓冲区达到 max_buffer_size 时强制刷新
  └── 断言非 Claude 路径 stdout=None 已改为 PIPE 且输出被记录

tests/test_agent_runner_cli.py
  [新增]
  【总结】验证 CLI 启动后 root logger 具备 FileHandler 且不重复

  ├── 断言 main() 调用后 logging.getLogger().handlers 包含 FileHandler
  └── 断言多次调用 configure_cli_logging() 不会重复添加 handler

tests/test_logger.py
  [修改]
  【总结】验证 app_logger.propagate = False 设置正确

  └── 断言 Logger 单例的 propagate 属性为 False
docs/guides/agent-runner.md
  [修改]
  【总结】补充日志文件位置和查看方式说明

  └── 新增 "查看运行日志" 小节，说明按日期划分的日志文件（如 logs/app-2026-05-24.log）和 14 天保留策略

docs/guides/configuration.md
  [修改]
  【总结】说明 iar run-once 日志行为和时间戳

  └── 补充日志文件位置、轮转行为和排查方式
```

### 5.3 Change Matrix

```text
src/backend/api/cli.py
  [修改]
  【总结】替换 logging.basicConfig() 为 factory.configure_cli_logging()

src/backend/engines/agent_runner/factory.py
  [修改]
  【总结】新增 configure_cli_logging() 桥接函数；transcript runner 输出增加日志记录

src/backend/infrastructure/logging/logger.py
  [修改]
  【总结】将 TimedRotatingFileHandler 改为按日期命名的 FileHandler，启动时清理过期日志

src/backend/infrastructure/process_runner.py
  [修改]
  【总结】Agent 实时输出增加时间戳终端打印和文件日志记录；非 Claude 路径改为 PIPE 中继

tests/test_process_runner.py
  [新增/修改]
  【总结】覆盖 Agent 输出日志化行为、PIPE 中继行为、缓冲区限制

tests/test_agent_runner_cli.py
  [新增]
  【总结】覆盖 CLI 日志初始化行为

tests/test_logger.py
  [修改]
  【总结】验证 propagate=False 设置
```

### 5.4 Flow

```text
iar run-once
  └── cli.main()
        └── configure_cli_logging()          # root logger -> console + logs/app-YYYY-MM-DD.log
        └── run_agent_repositories_once()
              └── run_once()
                    └── run_agent()
                          └── process_runner.run(capture_output=False)
                                ├── Claude 路径:
                                │     └── run_filtered_claude_stream()
                                │           ├── _format_timestamped_line() -> 终端（带行首时间戳）
                                │           └── _logger.info()            -> logs/app-YYYY-MM-DD.log
                                └── 非 Claude 路径:
                                      └── subprocess.PIPE + 中继循环
                                            ├── _format_timestamped_line() -> 终端（带行首时间戳）
                                            └── _logger.info()            -> logs/app-YYYY-MM-DD.log
```

## 6. Definition Of Done

- [ ] `uv run iar run-once` 执行后，`logs/app-YYYY-MM-DD.log` 存在带时间戳的运行记录。
- [ ] 终端输出的 `[agent tool]`、`[agent result]`、`[agent error]` 带有时间戳前缀。
- [ ] 流式 text_delta 保持实时、不碎片化地显示在终端。
- [ ] 非 Claude agent（默认 Codex/Kimi）的终端输出同样带有时间戳并写入日志。
- [ ] `logs/app-YYYY-MM-DD.log` 中无重复日志条目。
- [ ] `just lint` 通过。
- [ ] `pytest` 全部通过。
- [ ] 文档更新了日志查看说明。

## 7. Acceptance Checklist

### Behavior Acceptance

- [ ] `cli.py` 的 `main()` 不再调用 `logging.basicConfig()`。
- [ ] `factory.py` 的 `configure_cli_logging()` 被 `cli.main()` 调用。
- [ ] 调用后 `logging.getLogger().handlers` 至少包含一个 `FileHandler` 和一个 `StreamHandler`。
- [ ] 多次调用 `configure_cli_logging()` 不会重复添加 handler。
- [ ] `Logger` 单例的 `propagate` 属性为 `False`。
- [ ] `SubprocessRunner.run()` 的非 Claude 路径使用 `subprocess.PIPE` 而非 `stdout=None`。
- [ ] `run_filtered_claude_stream()` 的关键事件（tool / result / error）通过 `_logger.info()` 记录。
- [ ] `run_filtered_claude_stream()` 的流式文本在 `message_stop` 后汇总记录。
- [ ] `run_filtered_claude_stream()` 的 text_delta 缓冲区达到 `max_buffer_size` 时强制刷新。
- [ ] `SubprocessTranscriptRunner.run()` 的 deliberation 输出同时进入终端和日志。
- [ ] 终端时间戳出现在每行行首，不插入在流式单词中间。
- [ ] `capture_output=True` 分支的 stdout/stderr 不被时间戳或日志前缀污染。

### Validation Acceptance

- [ ] `pytest tests/test_process_runner.py -q` 通过。
- [ ] `pytest tests/test_agent_runner_cli.py -q` 通过。
- [ ] `pytest tests/test_logger.py -q` 通过。
- [ ] `just test` 全部通过。
- [ ] `uv run mkdocs build --strict` 通过。

## 8. Functional Requirements

- **FR-1**: CLI 启动时必须统一配置 root logger，使其同时输出到控制台和 `logs/app-YYYY-MM-DD.log`。
- **FR-2**: 日志格式必须包含时间戳、logger 名称、日志级别和消息内容。
- **FR-3**: `run_filtered_claude_stream()` 的结构化事件（tool use、result、error）必须通过 logging 记录。
- **FR-4**: `run_filtered_claude_stream()` 的流式文本增量必须在消息边界（`message_stop`）处汇总后记录；缓冲区达到 `max_buffer_size`（默认 4096 字符）时必须强制刷新。
- **FR-5**: `SubprocessTranscriptRunner` 和 `_run_agent_with_stdin_prompt()` 的 deliberation 实时输出必须同时进入终端和日志。
- **FR-6**: `SubprocessRunner.run()` 的非 Claude 路径必须使用 `subprocess.PIPE` + 中继循环，逐行读取、打印带时间戳输出并记录日志。
- **FR-7**: 终端实时输出必须在每行行首附加 `HH:MM:SS` 时间戳，且不影响流式连续性。
- **FR-8**: 日志文件必须按当天日期命名（如 `logs/app-2026-05-24.log`），启动时自动清理超过 14 天的旧日志文件。
- **FR-9**: `configure_cli_logging()` 必须是幂等的：多次调用不会重复添加 handler。
- **FR-10**: `capture_output=True` 分支的 stdout/stderr 返回值不得被时间戳或日志前缀污染。

## 9. Non-Goals

- 不引入结构化日志（JSON Lines）或日志采集系统（如 ELK、Loki）。
- 不改变 `IProcessRunner` 接口签名。
- 不改变 `capture_output=True` 分支的行为（继续全量捕获到 `CommandResult`）。
- 不修改 agent 子进程的启动方式或参数（除 `stdout=None` -> `PIPE` 外）。
- 不实现 Web UI 日志查看器。
- 不要求 deliberation 的 `create_event_sink` 也改用 logging（其已有 `events.jsonl` 独立持久化）。
- 不要求用户通过 shell 管道手动 `tee` 日志。

## 10. Risks And Follow-Ups

- **非 Claude 路径绕过风险**：`stdout=None` 使默认 Codex/Kimi `run-once` 完全绕过 Python 日志。改为 `PIPE` + 中继是首要修复，否则 PRD 对默认路径无效。
- **双重日志风险**：`"app"` logger 的 handlers 被复制到 root logger 后，若 `propagate=True`，`"app"` 命名空间下的记录会产生双重日志。实现中必须设置 `app_logger.propagate = False`。
- **时间戳缺失风险**：`_TimestampedPrinter` 若跳过以 `\n` 开头的字符串，`[agent tool]`、`[agent result]`、`[agent error]` 等结构化事件将无时间戳。实现中必须正确处理前导 `\n`。
- **内存风险**：agent 长会话中 `text_delta` 缓冲区可能无限增长。实现中必须设置 `max_buffer_size` 强制刷新边界。
- **测试脆弱性风险**：`main()` 中的 `logging.basicConfig()` 和处理器状态变更会泄漏到跨测试中；如果没有确定性的清理机制，测试顺序将变得重要。新测试必须使用 mocking 隔离或提供清理机制。
- **终端性能风险**：极小——按当前 agent 输出速率，`datetime.now().strftime()` 每行调用的开销可以忽略不计。
- **日志文件堆积风险**：按日期命名后每天产生一个文件，若 14 天保留策略未正确实现或进程异常退出导致清理未执行，`logs/` 目录可能积累大量文件。实现中必须确保 `_cleanup_old_logs()` 在每次 `_setup_logger()` 时都被调用。
- **可测试性风险**：`FakeProcessRunner` 在 `capture_output=False` 时返回 `stdout=""`，完全掩盖了真实运行器的行为。新测试必须直接针对 `process_runner.py` 使用模拟的 `subprocess.Popen` 进行测试。

## 11. Decision Log

| # | 决策问题 | 选择 | 放弃的方案 | 理由 |
|---|---|---|---|---|
| D-01 | 日志初始化位置 | `factory.py` 提供 `configure_cli_logging()` | `cli.py` 直接构造 `FileHandler` | 遵守 `api/` 不直接导入 `infrastructure/` 的架构约束，复用 `Logger` 单例；日志文件按日期命名避免单文件堆积。 |
| D-02 | text_delta 日志策略 | `message_stop` 边界汇总记录 + `max_buffer_size` 强制刷新 | 逐片段 `logger.info()` | 逐片段会产生碎片化时间戳，终端和日志都极难阅读。 |
| D-03 | 终端时间戳策略 | `_format_timestamped_line()` 为每行添加前缀 | 强制所有输出走 logging StreamHandler | StreamHandler 每个 record 换行，会破坏 text_delta 的流式连续性。 |
| D-04 | deliberation 输出 | `print()` + `logger.info()` 双通道 | 仅保留 `print()` | `deliberate` 输出也是 agent 运行记录，理应持久化。 |
| D-05 | 是否改接口 | 不改 `IProcessRunner` 签名 | 新增 `log_sink` 参数 | 接口变更会波及所有测试 mock，收益不足。 |
| D-06 | 非 Claude 路径 | `subprocess.PIPE` + Python 中继循环 | 保留 `stdout=None` | `stdout=None` 使子进程直接写入 OS 终端，任何 Python 日志机制都无法触及。 |
| D-07 | 处理器重复防护 | 按 handler 类型去重 + `propagate=False` | 简单 `if root_logger.handlers: return` | 测试框架可能已预装 StreamHandler；按类型去重更精确。设置 `propagate=False` 彻底避免双重日志。 |
| D-08 | 时间戳前导换行 | `_format_timestamped_line()` 逐行处理 | `_TimestampedPrinter` 状态机 | 逐行处理更简洁，天然正确处理前导 `\n`。 |
