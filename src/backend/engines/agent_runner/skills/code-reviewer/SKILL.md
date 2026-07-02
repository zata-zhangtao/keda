---
name: code-reviewer
description: "[Updated 2026-04-10] Full-stack pre-merge review specialist covering security, quality, maintainability, requirement alignment, validation evidence, and documentation synchronization. Use after implementation to review diffs, PRs, or recent commits and surface delivery gaps before merge."
---

# Code Reviewer

## Overview

Senior pre-merge reviewer for repository changes. This skill does not stop at code quality. It also checks whether the implementation matches the stated requirement, whether risky interfaces have real verification evidence, whether repository-standard validation commands were run, and whether docs stayed in sync.

Use this as the single primary skill for the pre-merge review phase. Do not stack it with a second reviewer skill in the same phase.

## Phase Boundary

- **Primary phase**: Pre-merge review
- **Run after**: Requirements are clarified and implementation is already complete
- **Do not use instead of**:
  - `requirement-sanity-reviewer` for ambiguous or contradictory requirements
  - `prd` for producing the implementation plan or PRD
- **Default rule**: One phase, one primary skill

## Review Process

When invoked:

1. **Gather delivery context** -- Run `git diff --staged` and `git diff` to inspect current changes. If no diff exists, inspect recent commits with `git log --oneline -5` and `git show`.
2. **Gather requirement context** -- Find the closest PRD, ticket, task file, or explicit user request before judging the code. Search `tasks/` first for active PRDs or task artifacts that match the change, then fall back to `tasks/archive/` when the matching PRD has already been archived or is clearly the right artifact.
3. **Extract acceptance criteria** -- Rewrite the expected behavior into a short checklist. Identify required scope, explicit non-goals, and any risky assumptions.
4. **Read surrounding code** -- Review the full files, imports, call sites, docs, and config touched by the change. Do not review the diff in isolation.
5. **Check requirement alignment** -- Decide whether the implementation is missing required behavior, adding out-of-scope behavior, or diverging from the stated contract.
6. **Check code quality and safety** -- Apply the checklist below from CRITICAL to LOW.
7. **Check interface validation evidence** -- If routes, handlers, schemas, auth, persistence write paths, or external integrations changed, look for API-, integration-, smoke-, or e2e-level proof instead of relying only on unit tests.
8. **Check repository validation status** -- Record whether `just lint`, relevant tests, and `uv run mkdocs build` were run. If they were not run, report that as a validation gap rather than silently assuming success.
9. **Check docs synchronization** -- For behavior, interface, config, command, or workflow changes, confirm whether `docs/`, `mkdocs.yml`, docstrings, examples, and API reference inputs were updated together.
10. **Report findings** -- Use the output contract below. Separate requirement gaps, code issues, validation gaps, and docs sync gaps.

## Confidence-Based Filtering

- **Report** findings only when you are more than 80% confident they are real
- **Skip** personal style preferences unless they violate repository conventions
- **Skip** issues in unchanged code unless they are CRITICAL security issues
- **Consolidate** repeated issues into one finding when they share the same root cause
- **Prioritize** defects that can cause incorrect behavior, security exposure, failed delivery, broken verification, or stale documentation

## Requirement Alignment

Always produce a short acceptance-criteria view before reviewing details:

- What the implementation was supposed to do
- What it was explicitly not supposed to do
- Which files or interfaces are the real delivery boundary

Flag a requirement finding when any of these are true:

- Required behavior from the PRD, ticket, task, or user request is missing
- The implementation changes behavior beyond the approved scope
- The change contradicts stated acceptance criteria, examples, or interface contracts
- The implementation silently changes defaults, auth rules, schema shape, or operational behavior without the requirement reflecting it

If the requirement source is weak or fragmented, say so explicitly in the review assumptions section instead of pretending it is clear.

## Interface Validation Evidence

Treat these changes as interface-boundary changes:

- route, handler, controller, or page entrypoint changes
- request or response schema changes
- auth, permission, middleware, session, or cookie changes
- database write-path or migration-adjacent changes
- external API or webhook integration changes

When an interface boundary is touched, check for evidence such as:

- automated API or integration tests
- HTTP-level tests using a client or test server
- e2e coverage
- recorded smoke verification using `curl`, `httpie`, or equivalent commands

Unit tests alone are often insufficient for these changes. If no interface-level evidence exists, report a validation gap even when the code looks correct.

## Repository Validation Commands

Prefer repository-standard verification over ad hoc commands. Record the status of:

- `just lint`
- relevant tests for the changed behavior
- `uv run mkdocs build`

Use these statuses:

- **passed** -- evidence shows the command ran successfully for the current change
- **partial** -- only a subset ran, or coverage did not exercise the risky path
- **not run** -- no evidence that the command was executed
- **blocked** -- the command could not run because of an environment or repository issue

Do not silently downgrade missing verification. Call it out.

## Docs Synchronization Checks

Whenever behavior, configuration, interfaces, or operating instructions change, check whether all relevant docs moved with the code:

- `docs/` pages for user-facing or maintainer-facing workflow changes
- `mkdocs.yml` navigation when new docs pages are added
- docstrings for public Python modules, classes, and functions
- examples, commands, environment variable references, and configuration guidance

Report a docs sync gap when code changes materially alter behavior or usage but the documentation still describes the old reality.

## Review Checklist

### CRITICAL -- Security

These MUST be flagged because they can cause real damage:

- **Hardcoded credentials** -- API keys, passwords, tokens, connection strings in source
- **SQL injection** -- String concatenation or f-strings in queries instead of parameterized queries
- **XSS vulnerabilities** -- Unescaped user input rendered in HTML or JSX
- **Command injection** -- Unvalidated input in shell commands; prefer subprocess list args
- **Path traversal** -- User-controlled file paths without sanitization
- **CSRF vulnerabilities** -- State-changing endpoints without CSRF protection where applicable
- **Authentication bypasses** -- Missing auth checks on protected routes
- **Insecure dependencies** -- Known vulnerable packages
- **Exposed secrets in logs** -- Tokens, passwords, or PII written to logs
- **Eval or exec abuse** -- Dynamic code execution with user input
- **Unsafe deserialization** -- `pickle` or unsafe YAML load on untrusted input
- **Weak crypto** -- MD5 or SHA1 used for security-sensitive flows

```python
# BAD: SQL injection via f-string
query = f"SELECT * FROM users WHERE id = {user_id}"

# GOOD: Parameterized query
query = "SELECT * FROM users WHERE id = %s"
cursor.execute(query, (user_id,))
```

```typescript
// BAD: SQL injection via string concatenation
const query = `SELECT * FROM users WHERE id = ${userId}`;

// GOOD: Parameterized query
const query = `SELECT * FROM users WHERE id = $1`;
const result = await db.query(query, [userId]);
```

### CRITICAL -- Error Handling (Python)

- **Bare except** -- `except: pass` catches everything including `SystemExit`; catch specific exceptions
- **Swallowed exceptions** -- Silent failures without logging or recovery
- **Missing context managers** -- Manual file or resource management instead of `with`

### HIGH -- Delivery Risks

- **Requirement mismatch** -- Behavior does not satisfy the stated requirement
- **Out-of-scope implementation** -- Extra behavior added without approval
- **Missing interface verification evidence** -- Risky interface changes with no API, integration, smoke, or e2e proof
- **Repository validation missing** -- `just lint`, relevant tests, or `uv run mkdocs build` not run for merge-critical changes
- **Docs drift** -- Code changed but `docs/`, docstrings, examples, or MkDocs navigation did not

### HIGH -- Code Quality

- **Large functions** -- More than roughly 50 lines of mixed responsibility
- **Large files** -- More than roughly 800 lines without clear segmentation
- **Deep nesting** -- More than four levels; prefer early returns or helper extraction
- **Missing error handling** -- Unhandled promise rejections or empty catch blocks
- **Mutation patterns** -- Prefer immutable operations where practical
- **Debug statements** -- Remove `console.log` or `print()` debug logging before merge
- **Missing tests** -- New code paths without coverage
- **Dead code** -- Commented-out code, unused imports, unreachable branches

```python
# BAD: Deep nesting plus mutation
def process_users(users):
    if users:
        for user in users:
            if user.active:
                if user.email:
                    user.verified = True
                    results.append(user)
    return results

# GOOD: Early returns plus immutability
def process_users(users: list[User]) -> list[User]:
    if not users:
        return []
    return [
        user.model_copy(update={"verified": True})
        for user in users
        if user.active and user.email
    ]
```

### HIGH -- Python Patterns

- **Missing type annotations** -- Public functions without type hints
- **Using `Any`** when a specific type is available
- **Missing `Optional`** for nullable parameters
- **Mutable default arguments** -- Use `None` and assign inside the function
- **`value == None`** -- Use `value is None`
- **`from module import *`** -- Avoid namespace pollution
- **`type() ==`** instead of `isinstance()`
- **String concatenation in loops** -- Prefer `"".join()`
- **Magic numbers** without named constants or an `Enum`
- **Shadowing builtins** such as `list`, `dict`, `str`, or `id`
- **Missing `encoding=\"utf-8\"`** -- All `open()`, `Path.read_text()`, and `Path.write_text()` calls must specify encoding
- **`print()` instead of `logging`** -- Use the logging module

```python
# BAD: Missing encoding on Windows-sensitive I/O
with open("file.txt", "w") as file_handle:
    file_handle.write(data)

# GOOD: Explicit UTF-8 encoding
with open("file.txt", "w", encoding="utf-8") as file_handle:
    file_handle.write(data)
```

### HIGH -- Python Concurrency

- **Shared state without locks** -- Use `threading.Lock` or another safe guard
- **Mixing sync and async incorrectly**
- **N+1 queries in loops** -- Batch queries instead

### HIGH -- React and Next.js Patterns

- **Missing dependency arrays** -- Hooks with incomplete dependencies
- **State updates in render** -- Calling state setters during render
- **Missing keys in lists** -- Array indices used as keys when order can change
- **Prop drilling** -- State passed through too many levels without a clear reason
- **Unnecessary re-renders** -- Expensive work without stabilization when needed
- **Client or server boundary mistakes** -- Server components using client-only APIs
- **Missing loading or error states** -- Data fetching without fallbacks
- **Stale closures** -- Event handlers capturing outdated state

```tsx
// BAD: Missing dependency, stale closure risk
useEffect(() => {
  fetchData(userId);
}, []);

// GOOD: Complete dependencies
useEffect(() => {
  fetchData(userId);
}, [userId]);
```

### HIGH -- Node.js and Backend Patterns

- **Unvalidated input** -- Request bodies or params used without schema validation
- **Missing rate limiting** -- Public endpoints without throttling
- **Unbounded queries** -- User-facing queries without a sensible limit
- **N+1 queries** -- Related data fetched in loops
- **Missing timeouts** -- External HTTP calls without timeout configuration
- **Error message leakage** -- Internal details returned to clients
- **Missing CORS configuration** -- APIs reachable from unintended origins

```typescript
// BAD: N+1 query pattern
const users = await db.query("SELECT * FROM users");
for (const user of users) {
  user.posts = await db.query("SELECT * FROM posts WHERE user_id = $1", [user.id]);
}

// GOOD: Single query with JOIN
const usersWithPosts = await db.query(`
  SELECT u.*, json_agg(p.*) as posts
  FROM users u LEFT JOIN posts p ON p.user_id = u.id
  GROUP BY u.id
`);
```

### MEDIUM -- Performance

- **Inefficient algorithms** -- `O(n^2)` where `O(n log n)` or `O(n)` is feasible
- **Unnecessary re-renders** -- Heavy computations without control
- **Large bundle sizes** -- Pulling in entire libraries unnecessarily
- **Missing caching** -- Repeated expensive work without memoization or reuse
- **Unoptimized images** -- Large assets without compression or lazy loading
- **Synchronous I/O** -- Blocking operations inside async paths

### MEDIUM -- Best Practices

- **PEP 8 violations** -- Import order, naming, spacing
- **Missing docstrings** -- Public functions lacking Google Style docstrings
- **Poor naming** -- Generic names like `data`, `item`, or `res` in non-trivial contexts
- **Magic numbers** -- Numeric constants without explanation
- **Inconsistent formatting** -- Mixed semicolons, quote styles, or indentation

### LOW -- Minor

- **TODO or FIXME without tracking context** -- Reference an issue or explain ownership
- **Missing JSDoc or docstring for public APIs** -- Missing docs on exported interfaces

## Project-Specific Checks

These checks come from this repository's working conventions:

- **Encoding** -- All `open()`, `Path.read_text()`, and `Path.write_text()` calls must use `encoding="utf-8"`
- **Google Style Docstrings** -- Module, class, and function docstrings should follow Google Style with `Args`, `Returns`, and `Raises`
- **Type Annotations** -- Function arguments and return types should be annotated
- **AI-Native Naming** -- Avoid vague names such as `data`, `item`, or `res`; prefer source-aware names
- **SSA Pattern** -- Prefer fresh variable names for each processing stage instead of repeated mutation
- **Pydantic Models** -- Use `BaseModel` with `Field` descriptions for structured data when appropriate

## Docker and Container Checks

### CRITICAL -- Docker Security

- **Running as root** -- Final stage should use a non-root user
- **Secrets in image** -- `.env`, credentials, or API keys copied into image layers
- **Secrets in build args** -- Sensitive data passed through Docker `ARG`
- **Using `latest` tag** -- Base images should pin versions or digests
- **Exposed sensitive ports** -- Debug or database ports exposed in production

```dockerfile
# BAD: Running as root, unpinned base
FROM python
COPY . .
CMD ["python", "app.py"]

# GOOD: Pinned base, non-root user, minimal image
FROM python:3.12-slim AS runtime
RUN groupadd -r app && useradd -r -g app appuser
COPY --from=builder /app /app
USER appuser
CMD ["python", "app.py"]
```

### HIGH -- Dockerfile Quality

- **No multi-stage build** -- Separate build dependencies from runtime
- **Missing `.dockerignore`** -- Exclude `.git`, `node_modules`, `__pycache__`, `.venv`, and `.env`
- **Poor layer caching** -- Copy dependency manifests before source code
- **Installing dev dependencies in runtime**
- **No healthcheck** -- Production images should define `HEALTHCHECK`
- **Unnecessary packages** -- Missing `--no-install-recommends` or apt cleanup
- **Missing `COPY --chown`** -- Ownership mismatch for non-root runtime users

```dockerfile
# BAD: Poor layer caching
COPY . .
RUN pip install -r requirements.txt

# GOOD: Dependency files first for cache efficiency
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
```

### HIGH -- docker-compose Quality

- **Missing healthcheck** -- Services should define health checks for dependency ordering
- **Missing resource limits** -- Production services should set memory or CPU limits
- **Missing restart policy** -- Use `restart: unless-stopped` or equivalent
- **Hardcoded credentials** -- Passwords or secrets inline instead of env files or secrets
- **No named volumes** -- Anonymous database volumes risk accidental data loss
- **Missing `depends_on` conditions** -- Prefer `condition: service_healthy`
- **Missing environment variables** -- Cross-check app expectations against compose injection

```yaml
# BAD: No healthcheck, no limits, hardcoded password
services:
  db:
    image: postgres
    environment:
      POSTGRES_PASSWORD: mysecret

# GOOD: Healthcheck, limits, env file
services:
  db:
    image: postgres:16-alpine
    env_file: .env
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $POSTGRES_USER"]
      interval: 10s
      timeout: 5s
      retries: 3
    deploy:
      resources:
        limits:
          memory: 512M
    restart: unless-stopped
    volumes:
      - pgdata:/var/lib/postgresql/data
```

### MEDIUM -- Container Best Practices

- **Large image size** -- Prefer slim, alpine, or distroless bases where appropriate
- **Missing `WORKDIR`** -- Set a working directory explicitly
- **Multiple `RUN` layers** -- Combine related steps where practical
- **`ADD` instead of `COPY`** -- Use `COPY` unless `ADD` is needed
- **No `.env.example`** -- Provide a safe env template when env vars are required

## Framework-Specific Checks

- **Django** -- `select_related`, `prefetch_related`, `atomic()`, migrations
- **FastAPI** -- CORS config, Pydantic validation, response models, no blocking in async handlers
- **Flask** -- Proper error handlers and CSRF protection

## Review Output Format

最终评审报告必须使用中文输出。

Present findings first, ordered by severity, and include file references when available.

Use this structure:

```text
[HIGH][requirement] 缺少 PRD 要求的导出流程
文件: app/services/export.py:88
影响: 任务要求支持 CSV 和 JSON 导出，但实现仅添加了 CSV。
依据: tasks/20260410-101907-prd-review-skill-delivery-gates.md 明确说明两种格式均在范围内。
修复建议: 在合并前实现 JSON 分支，或收窄需求范围。
```

Allowed finding categories:

- `requirement`
- `code`
- `validation`
- `docs`

### Review Summary

End every review with a concise delivery summary using symbols for clarity. Keep it short and text-based, no tables.

```text
## 评审总结

🔴 严重: 0  ✅
🟠 高: 2  ⚠️
  1. 缺少 PRD 要求的 JSON 导出分支
  2. 相关接口无集成测试覆盖
🟡 中: 1  ⚠️
  1. 导出服务缺少输入参数校验
🔵 低: 0  ✅

需求状态: ✅ 通过
验证状态: ⚠️ 部分
文档状态: ✅ 通过

已检查命令:
- just lint: ✅ 通过
- 相关测试: ⚠️ 部分
- uv run mkdocs build: ✅ 通过

结论: ⚠️ 警告 -- 合并前仍有 2 项高优先级问题待解决。
```

### Machine-Readable Verdict Block

After the Chinese review report above, append a final fenced JSON block so that
automated gates (e.g. the agent runner pre-PR review gate) can parse the
verdict without ambiguity:

```json
{
  "verdict": "approved" | "changes_requested",
  "summary": "short rationale in English or Chinese",
  "findings": [
    {
      "category": "requirement|code|validation|docs",
      "severity": "critical|high|medium|low",
      "file": "path/to/file.py",
      "line": 42,
      "title": "short title",
      "description": "why this is a problem",
      "recommendation": "how to fix"
    }
  ]
}
```

Rules for the JSON block:

- It must be the last fenced code block in your response.
- If there are no unresolved findings, set `verdict` to `approved` and `findings` to `[]`.
- If there are any unresolved findings, set `verdict` to `changes_requested`.
- If you have already applied fixes in the worktree, set `verdict` to `approved` and
  write `.agent-runner/commit-request.json` with a descriptive `commit_message`.

If requirement context is incomplete, add an `Assumptions` section and state exactly what requirement source you relied on.

## Approval Criteria

- **通过** -- 无严重问题，无未解决的高优先级问题，且需求和文档状态均未失败
- **警告** -- 存在高优先级问题，或验证状态为部分通过，但变更可在明确接受风险后合并
- **阻止** -- 存在严重问题，缺少必需行为，或高风险接口变更缺乏必要的验证证据

## AI-Generated Code Review Addendum

When reviewing AI-generated changes, additionally check:

1. Behavioral regressions and edge-case handling
2. Security assumptions and trust boundaries
3. Hidden coupling or accidental architecture drift
4. Unnecessary complexity that increases maintenance or model cost
