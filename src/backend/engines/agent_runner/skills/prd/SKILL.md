---
name: prd
description: "[Updated 2026-05-22] Generate an architecture-aware technical PRD. Triggers on: create a prd, write prd for, plan this feature. Prioritizes reuse, minimal-change plans, required output compliance, realistic validation, and conditional web research."
---

# PRD Generator (Architecture-First)

Create technical PRDs that fit the existing codebase instead of expanding it unnecessarily.
The default recommendation must be the smallest change that cleanly solves the problem.

---

## Core Rules

1. **Repository First:** Treat the current codebase as the primary source of truth.
2. **Architecture Before Output:** Do not jump to sections, diagrams, or prototype work before identifying existing boundaries, extension points, and reusable modules.
3. **Minimal Change Bias:** Prefer extending an existing path over adding a new layer, service, hook, table, page, or dependency.
4. **No Redundant Abstractions:** Every newly proposed abstraction must be justified against the current codebase.
5. **Target-State Bias:** Default to a complete end-state plan. Do not split required work into `Phase 1` / `Phase 2`, temporary facades, or deferred cleanup unless a real constraint makes single-stage delivery unsafe or impossible.
6. **Conditional Web Research:** Browse only when the answer depends on external facts that are not stable in the repository.
7. **Output Contract:** Treat the required PRD structure as mandatory. Do not omit, rename, or bury required sections unless the user explicitly requests a different format.
8. **Realistic Validation:** Every PRD must identify the highest-fidelity validation needed to prove the behavior works through real project entry points, not only isolated unit or integration tests.
9. **Executor-Resilient Detail:** Write implementation detail for a less capable executor: be concrete, but prefer semantic anchors and repository searches over brittle coordinates such as line numbers.

---

## Workflow

### Phase 0: Rewrite The Request As An Implementable Claim

State in plain language:
- who wants what behavior
- under which conditions
- what changes in system state, API, UI, or workflow

If you cannot rewrite the request concretely, call that out before generating a PRD.

### Phase 1: Repository Context And Architecture Gate

Before asking questions or proposing changes, inspect the repository for:
- tech stack and runtime constraints
- current module boundaries and dependency direction
- existing extension points and reusable code paths
- current data model and state ownership
- existing docs, tests, and workflows relevant to the request

You must explicitly identify:
- **Existing Path:** the current code path that is closest to the requested change
- **Reuse Candidates:** files/modules that can be extended directly
- **Architecture Constraints:** boundaries that should not be broken
- **Potential Redundancy Risks:** likely sources of duplicated logic or parallel abstractions

Do not ask questions that can be answered by reading the repository.

### Phase 2: Clarify Only What The Code Cannot Answer

Ask only the critical questions that remain unresolved after repository analysis.
If an unresolved item would materially change scope, behavior, trust boundaries, rollout, or architecture, you must ask the user to confirm it before finalizing the PRD.
Do not silently pick a default for a requirement-level ambiguity that the repository cannot answer.

Question categories:
- business rule ambiguity
- permission or trust-boundary ambiguity
- scope boundaries
- rollout or migration decisions

For each unresolved question:
1. state the decision that must be made
2. ask the user to confirm the answer before finalizing the PRD
3. give the single answer you recommend by default
4. justify the recommendation using existing repository patterns where possible
5. include alternative options only when the trade-off is real and material

If there are no critical unresolved questions, proceed without asking.

### Phase 3: Redundancy Gate And Recommendation Check

Before writing the final PRD, stress-test your recommendation against the closest heavier alternative:

1. **Minimal-Change Path**
   Extend the closest existing path with the fewest new moving parts.
2. **Heavier Alternative**
   Introduce a new abstraction, module, service, table, page, or dependency only if warranted.

The PRD should present a single recommended path by default.
Recommend the minimal-change path unless the heavier alternative is clearly necessary.
Mention the heavier alternative in the PRD only when it materially affects scope, risk, or architecture.

For every proposed new item, answer:
- why the existing path is insufficient
- why this does not duplicate an existing responsibility
- what complexity it adds
- whether an existing path can be removed or consolidated as part of the change

If you cannot justify the new item, do not recommend it.

### Phase 3.5: Realistic Validation Gate

Before finalizing the PRD, identify the highest-fidelity validation needed to prove the requested behavior works through real project entry points.

For each behavior that changes user-visible output, API behavior, persistence, background workflow, external integration, or deployment/runtime behavior, specify:
- the real entry point to exercise, such as CLI command, HTTP API, app startup, Playwright flow, worker job, migration, or service composition root
- which dependencies may be mocked and which must remain real
- required test data, environment variables, service state, or sandbox accounts
- the exact automated command or manual/sandbox validation procedure
- why lower-level unit or integration tests alone are sufficient or insufficient

**Hard rule:** If the PRD introduces or changes executable behavior (CLI commands, API endpoints, background jobs, file output, or external integrations), the Realistic Validation Plan MUST contain at least one validation row that exercises the behavior through the real entry point. "Unit tests are sufficient" is NOT an acceptable substitute unless the change is purely internal refactoring with no user-visible or executable surface. Use dry-run, local file output, or sandbox mode to avoid requiring live external services when credentials are unavailable.

Do not require live external services by default. If live or sandbox validation is necessary, make it opt-in, explicitly gated by environment variables, and document the fallback validation when credentials are unavailable.

When validating delivery/archive readiness, run the bundled checklist checker when a Python runtime is available. Resolve `scripts/check_prd_acceptance_checklist.py` relative to this skill directory; do not hard-code an installation path.

```bash
python scripts/check_prd_acceptance_checklist.py --repo-root <repo-root> --all
```

For a pending PRD that is about to be archived, explicitly validate that file:

```bash
python scripts/check_prd_acceptance_checklist.py --repo-root <repo-root> --check-provided tasks/pending/P0-BUG-20260527-120000-example.md
```

Pending PRDs may intentionally keep unchecked acceptance items while waiting for implementation, so do not run the acceptance-completion checker as a blocker for a normal newly generated PRD.
This checker is intentionally bundled with the skill for installed-skill usage; repository `pre-commit` integration is optional and may use a project-local hook instead.

### Phase 4: Conditional Web Research

Use web search only when the decision depends on external facts that may have changed, such as:
- third-party APIs, SDKs, or vendor capabilities
- security guidance, standards, or regulations
- framework, library, or platform version behavior
- ecosystem best practices that materially affect safety or operability
- explicit user requests for competitive or external-pattern research

When browsing:
- prefer official documentation and primary sources
- use search to inform constraints, compatibility, and risks, not to override repository patterns
- include sources and dates in the PRD
- clearly mark what is sourced fact versus your inference

Do not use web results as a reason to add abstractions by default.

### Phase 5: Prototype And Visual Artifact Gate

Visual artifacts should clarify the change, not pad the PRD.

Always include:
- a **Change Impact Tree**
- at least one **flow or architecture diagram**

Include a **low-fidelity prototype** only when:
- the request is UI-heavy, or
- behavior depends on multi-step user interaction, or
- layout is necessary to resolve scope ambiguity

Include an **ER diagram** only when data model or persistent state changes.

Create or modify interactive prototype files under `docs/prototypes/` only when:
- the user explicitly asks for a prototype, wireframe, or interactive demo, or
- static diagrams cannot adequately express the behavior under review

Do not create prototype files merely because the feature touches UI.

### Phase 5.5: Executor Resilience Gate

Implementation guidance may be detailed, especially when the PRD will be handed to another agent, but it must be resilient to normal repository drift.

Required:
- use file paths, symbol names, recipe names, config keys, routes, selectors, or section headings as anchors
- include `rg` search commands for legacy references, new target references, and likely hidden references when repository-wide references may exist
- state that the listed files are the starting point, not a guarantee that no other affected files exist
- include short validation-failure triage notes for risky commands, such as Docker build context, CI working directory, cache path, artifact path, route path, env var, or composition-root checks
- mark live production, vendor, or credential-dependent validation as opt-in or post-merge unless it is truly required to prove the change

Forbidden:
- instructions that depend on deleting, editing, or trusting exact line numbers or line ranges
- shell commands that are not copy-paste executable in the target repository environment
- `grep` alternation such as `a\|b` unless the command explicitly uses a compatible mode such as `grep -E`; prefer `rg`
- acceptance criteria that imply the explicit file list is exhaustive when a repository search can verify the final state

### Phase 6: Generate And Save The PRD

Write the PRD to:
- `tasks/pending/<PRIORITY>-<TYPE>-<YYYYMMDD-HHMMSS>-<slug>.md`

| Segment | Description |
|---------|-------------|
| `PRIORITY` | `P0` (urgent) / `P1` (high) / `P2` (normal) / `P3` (low) |
| `TYPE` | `BUG` / `FEAT` / `REFACTOR` / `PERF` / `DOCS` / `CHORE` / `SECURITY` |
| `YYYYMMDD-HHMMSS` | Local current time |
| `slug` | Lowercase with hyphens |

Feature slug must be lowercase with hyphens.
Timestamp must use local current time in `YYYYMMDD-HHMMSS` format.

### Phase 7: PRD Compliance Gate

Before handing off the PRD, verify the whole document has:
- all required top-level sections in the required order
- Section 1 includes a `### Realistic Validation` checklist immediately after the goals/objectives
- Section 5 starts with the required living implementation guide statement
- a Change Impact Tree
- at least one Mermaid flow or architecture diagram
- a Realistic Validation Plan that contains at least one row with a real entry point (not only pytest/helper functions), unless the PRD explicitly documents why the change is pure internal refactoring with no executable surface
- an Acceptance Checklist with grouped headings and concrete checkbox items
- Functional Requirements using `FR-1`, `FR-2`, ... identifiers
- Non-Goals
- Risks And Follow-Ups
- a Decision Log with at least one row
- no line-number-dependent implementation instructions
- copy-paste executable validation/search commands, with `rg` preferred for repository searches
- executor drift guards for repository-wide refactors, migrations, path moves, config rewires, or other changes where hidden references are likely

When updating an existing PRD, run this gate against the entire file. If the existing file is non-compliant, preserve valid context and decisions but reorganize the document into the required structure instead of appending a compliant fragment to a non-compliant PRD.

**If the PRD changes executable behavior and the Realistic Validation Plan contains only unit/integration test entries with no real entry point, or the Validation Acceptance lacks a real entry-point item without a justified internal-refactoring exception, this gate FAILS. Do not hand off the PRD.**

Use `rg -n "^## " <prd-file>` or an equivalent section-header check when a PRD file exists.

---

## Required PRD Structure

This structure is the output contract for generated and updated PRDs.

### 1. Introduction & Goals

Brief problem statement plus measurable objectives.

Must include a `### Realistic Validation` checklist before Section 2.
This first-section checklist is a concise, reviewer-facing summary of the highest-fidelity validation expected for the task. It must use Markdown checkbox items and mirror the style of:

```markdown
### Realistic Validation

除单元测试和集成测试外，本 PRD 要求通过**真实项目入口点**验证关键行为，确保真实使用路径生效，而非仅在隔离 fixture 中通过。

- [ ] **[行为名称] 真实验证**：通过 `[真实入口命令或流程]` 验证 `[关键可观察结果]`。
- [ ] **[配置/状态/回退] 真实验证**：通过 `[真实入口命令或流程]` 验证 `[关键可观察结果]`。
- [ ] **为什么单元测试不够**：说明真实入口验证覆盖了哪些单元测试无法证明的行为。
```

Rules:
- Keep this checklist short enough to scan, usually 2-5 items.
- Use concrete real entry points such as CLI commands, HTTP endpoints, app startup, Playwright flows, worker jobs, migrations, or publish/deploy procedures.
- Include dry-run, local file output, sandbox mode, or mocked external boundary details when live services are not required.
- This checklist does not replace the detailed `Realistic Validation Plan` table in Section 5.

### 2. Requirement Shape

- actor
- trigger
- expected behavior
- explicit scope boundary

### 3. Repository Context And Architecture Fit

Must include:
- current relevant modules/files
- existing architecture pattern to follow
- ownership and dependency boundaries
- constraints from runtime, docs, tests, or workflows

### 4. Recommendation

Must include:
- **Recommended Approach**
- why this is the best fit for the current architecture
- rationale for rejecting redundant abstractions
- **Alternatives Considered** only when a plausible non-trivial alternative exists

### 5. Implementation Guide

This section must start with this sentence or a close equivalent:

> This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

Must include:
- **Core Logic:** how data and control move through the existing system
- **Change Impact Tree**
- **Executor Drift Guard** when hidden references or repository drift could affect implementation
- **Flow or Architecture Diagram**
- **Realistic Validation Plan**
- **Low-Fidelity Prototype** when required
- **ER Diagram** when required
- **Interactive Prototype Change Log** when prototype files changed
- **External Validation** when web research was used

### 6. Definition Of Done

Include:
- implementation validation
- realistic validation through the highest feasible real entry point
- docs updates
- no regression checks
- architecture-fit checks
- overall delivery/readiness gates only; do not use this section as a substitute for the Acceptance Checklist

### 7. Acceptance Checklist

Include:
- a dedicated section named `Acceptance Checklist`
- grouped checklist headings such as `Architecture Acceptance`, `Dependency Acceptance`, `Behavior Acceptance`, `Documentation Acceptance`, and `Validation Acceptance` when relevant
- concrete, repository-verifiable checkbox items
- exact paths, API contracts, commands, or search assertions where applicable
- at least one `Validation Acceptance` item that exercises the changed behavior through the highest feasible real entry point; if no real entry-point validation is included, the PRD must explicitly document that the change is pure internal refactoring with no executable surface, and this justification must be reviewed in the Decision Log
- no checklist item may be replaced by a `Definition Of Done` bullet or by local requirement acceptance notes

### 8. Functional Requirements

Use numbered requirements such as `FR-1`, `FR-2`.

### 9. Non-Goals

List explicit out-of-scope items.

### 10. Risks And Follow-Ups

List only unavoidable migration risk, rollout risk, or explicitly approved non-blocking follow-up.
Do not use this section to park work that is actually required for the recommended target state.

### 11. Decision Log

Record every key decision made during this PRD as a permanent reference that survives archival.

Rules:
- Each row answers one decision question (e.g. "which architecture pattern", "which storage backend").
- **Chosen** must match the recommendation in Section 4.
- **Rejected** must name the concrete alternative from Section 4 when one is documented, not a vague "other approaches".
- **Rationale** must be one concrete sentence — not "fits the architecture" but why specifically.
- Assign sequential IDs: D-01, D-02, …
- Minimum one row per PRD. Add rows for major trade-offs or alternatives explicitly resolved in Section 4.

---

## PRD Content Rules

### A. Change Impact Tree

Use Tree style, organized by layer:

```text
.
├── xxx/
│   └── xxx.py
│       [新增] / [修改] / [删除]
│       【总结】一句话总结本次修改
│
│       ├── 具体改动 1
│       ├── 具体改动 2
│       └── 具体改动 3
```

**每个文件必须有【总结】**：一句话、高信息密度，直接说明文件改了什么，不重复文件名。

层级的推荐顺序：

```text
Database
Infrastructure
Domain
API
Frontend
Tests
Docs
```

内容重点覆盖：SQL 变化、字段变化、数据流变化、API 变化、ORM 变化、Domain Logic 变化、UI 展示变化、类型同步、测试同步。

不要包含：import 调整、格式化、lint 修复、无意义 rename、与任务无关的小改动。

For executor handoff quality:
- keep file-level and logical specificity, but do not reference exact line numbers or line ranges
- locate fragile edits by semantic anchors such as function names, recipe names, route names, config keys, or headings
- include the `rg` command an executor can use to find each important anchor when helpful
- if the file list could be incomplete, say so and point to the Executor Drift Guard instead of pretending the tree is exhaustive

### B. Flow / Architecture Diagram

At least one Mermaid diagram is required.
Prefer a diagram that shows where the requested change lands inside the existing system.

Mermaid label safety rule:
- If a node label contains special characters such as `/`, `{}`, `[]`, `()`, `:`, or `-`, wrap the label with double quotes.

### C. Low-Fidelity Prototype

Include only when required by Phase 5.
Use ASCII wireframe or Mermaid layout.

### D. ER Diagram

Include only when:
- a new entity/table/model is added
- fields or relationships change
- persistent structured state changes

If not required, state:
- `No data model changes in this PRD.`

### E. Interactive Prototype Change Log

Include only when prototype files were actually changed.

| File Path | Change Type | Before | After | Why |
|---|---|---|---|---|
| `docs/prototypes/<feature>-demo.html` | Modify | Static layout only | Added state transition controls | Clarify behavior that static diagrams could not show |

If no prototype files changed, state:
- `No interactive prototype file changes in this PRD.`

### F. Realistic Validation Plan

Every PRD must include this section.

Use this structure:

| Behavior | Real Entry Point | Test Layer | Mock Boundary | Data/Env Needed | Command Or Procedure | Required For Acceptance |
|---|---|---|---|---|---|---|
| [changed behavior] | [API/CLI/UI/job/startup/migration/etc.] | [unit/integration/e2e/smoke/sandbox/manual] | [what is mocked vs real] | [fixtures/env/services] | `[exact command]` | Yes/No |

If the change has no executable behavior, state:
- `No executable behavior changes; realistic validation is limited to documentation/build checks.`

If live or sandbox validation needs credentials or external services that may be unavailable, state:
- the opt-in environment variables or service prerequisites
- the fallback automated validation that must still run without those credentials
- whether skipped live validation blocks acceptance

Command rules:
- commands must be copy-paste executable from the documented working directory
- prefer `rg` over `grep` for repository searches
- if `grep` is used with alternation, use an explicit compatible form such as `grep -E 'a|b'`
- do not make production deploys or live vendor calls blocking acceptance unless the behavior cannot be proven through local, CI, dry-run, sandbox, or staged validation
- add a short failure-triage note for high-friction validation, naming the first paths, config keys, or boundaries to inspect

### G. External Validation

Include only when web research was used.

Use this structure:

| Topic | Source | Checked On | Relevant Finding | Impact On Recommendation |
|---|---|---|---|---|
| [Vendor/API/standard] | [URL or document title] | [YYYY-MM-DD] | [Fact] | [Constraint, risk, or compatibility note] |

If no web research was needed, state:
- `No external validation required; repository evidence was sufficient.`

### H. Acceptance Checklist

This section is required even when Functional Requirements already include acceptance criteria.
Do not merge it into `Definition Of Done`.

Use grouped subsections.
For architecture-heavy or refactor work, prefer:
- `Architecture Acceptance`
- `Dependency Acceptance`
- `Behavior Acceptance`
- `Documentation Acceptance`
- `Validation Acceptance`

Each checkbox must describe a concrete, verifiable end state.
Prefer exact file paths, commands, API paths/contracts, dependency boundaries, or repository search assertions over vague quality statements.
Validation Acceptance must include the highest feasible real entry point from the Realistic Validation Plan, not only isolated lower-level tests, unless the PRD explicitly explains why no executable behavior changed. "Mocked at boundary" or "unit tests cover it" are not valid justifications for skipping real entry-point validation when executable behavior changed.
For repository-wide refactors, migrations, or path moves, include at least one repository search assertion that proves obsolete references are gone and expected target references remain.
If a default group does not fit the task, rename or replace it with a more precise group instead of dropping the section entirely.
The checklist must validate the final target state, not merely the completion of an interim first phase.

---

## Checklist

**BLOCKER items must be satisfied before the PRD can be handed off. Non-blocker items should be satisfied but do not stop delivery.**

* [ ] Rewrote the request into a concrete behavior change
* [ ] Inspected the repository before asking questions
* [ ] Identified the closest existing code path
* [ ] Handled critical unresolved questions correctly: asked the user only when repository evidence was insufficient and the answer would materially affect the PRD
* [ ] Compared a minimal-change option against a heavier option
* [ ] Justified every new abstraction, dependency, or file path
* [ ] Rejected redundant layers where reuse was sufficient
* [ ] Included a Change Impact Tree with architecture-fit reasoning
* [ ] **BLOCKER:** Did not include line-number-dependent edit instructions; all fragile edits use semantic anchors and/or `rg` search commands
* [ ] Included at least one flow or architecture diagram
* [ ] Implementation Guide includes the required living implementation guide statement
* [ ] Included an Executor Drift Guard when hidden references, moved paths, config rewires, or repository-wide updates are likely
* [ ] **BLOCKER:** Included a Realistic Validation Plan that names real entry points, mock boundaries, data/env needs, and commands or procedures
* [ ] Added low-fidelity prototype only when actually needed
* [ ] Added ER diagram only when data model changes are present
* [ ] Used web research only when external facts were required
* [ ] Cited sources and dates for any web-derived claims
* [ ] Saved new PRDs to `tasks/pending/<PRIORITY>-<TYPE>-<YYYYMMDD-HHMMSS>-<slug>.md`
* [ ] Did not require acceptance-completion checks for normal pending PRDs; for archive readiness, ran the bundled `scripts/check_prd_acceptance_checklist.py` checker when available
* [ ] For existing PRD updates, restructured the whole PRD to the required shape instead of appending to a non-compliant file
* [ ] Ran a section compliance check, manually or with `rg -n "^## " <prd-file>`
* [ ] Included a dedicated `Acceptance Checklist` section and did not collapse it into `Definition Of Done` or local requirement notes
* [ ] **BLOCKER:** All validation/search commands are copy-paste executable; repository searches prefer `rg`, and any `grep` alternation uses an explicit compatible mode
* [ ] **BLOCKER:** Validation Acceptance includes the highest feasible real entry-point validation or explicitly documents why the change is pure internal refactoring with no executable surface
* [ ] Recommended a full target state rather than leaving required work in `Phase 2`, `follow-up`, or temporary compatibility layers unless a hard constraint was explicitly documented
* [ ] Decision Log has at least one row for each major trade-off or documented alternative resolved in Section 4
* [ ] Each Decision Log row names a concrete rejected alternative (not a vague "other approaches")
