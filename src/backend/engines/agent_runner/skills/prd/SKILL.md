---
name: prd
description: "[Updated 2026-06-24] Generate an architecture-aware technical PRD. Triggers on: create a prd, write prd for, plan this feature. Prioritizes reuse, minimal-change plans, required output compliance, realistic validation, and conditional web research."
---

# PRD Generator (Architecture-First)

> **Maintenance note:** The `[Updated YYYY-MM-DD]` tag in the frontmatter reflects the date of the last **substantive** change to this SKILL.md (core rules, workflow phases, output contract, or templates). When committing a change to this file or any template under `skills/prd/`, bump the date to match the commit day so it never lags behind git history.

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
10. **Full-Stack Surface:** Treat the user-visible frontend as first-class. Discover the repo's actual frontend app(s) (don't assume a framework or directory) and plan any user-facing change with backend-level rigor; a genuinely backend-only PRD must state `No frontend impact` with a one-line reason rather than omit it silently. (Detailed gate: Phase 1.5.)

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
- user-facing surface: which frontend app(s) the repository ships (discover from top-level app directories, `package.json`, and framework configs) and whether the request touches them, including the closest routes/components/state
- existing PRDs under `tasks/pending/` and related archived PRDs under `tasks/archive/`

You must inspect existing PRDs before creating a new one:
- search `tasks/pending/` first for duplicate, overlapping, prerequisite, or downstream work
- search `tasks/archive/` when a completed PRD may define context, prior decisions, or reusable acceptance criteria
- reuse or update an existing pending PRD when it clearly represents the same work instead of creating a duplicate
- populate `Delivery Dependencies` from explicit pending PRD relationships when a task must wait for another task or group
- use `none` only after checking pending PRDs and finding no sequencing dependency
- do not infer hard dependencies from vague topic similarity; record uncertain relationships as `soft` or ask the user when dependency choice changes scope or execution order

You must explicitly identify:
- **Existing Path:** the current code path that is closest to the requested change
- **Reuse Candidates:** files/modules that can be extended directly
- **Architecture Constraints:** boundaries that should not be broken
- **Frontend Impact:** which of the repository's frontend app(s) change, or none with a reason
- **Existing PRD Relationship:** whether the request duplicates, depends on, blocks, or is independent from current pending PRDs
- **Potential Redundancy Risks:** likely sources of duplicated logic or parallel abstractions

Do not ask questions that can be answered by reading the repository.

### Phase 1.5: Frontend Impact Gate

Decide the user-facing surface before designing the backend. First discover what frontend(s) the repository actually ships — do not assume a fixed framework or directory name. Inspect:
- top-level application directories and the repository's architecture docs
- each candidate's `package.json` (framework, and the dev/build/test/e2e scripts) and framework config files
- how each frontend is run and tested in this repo (its dev command, app-run command, and e2e/UI test command)

Record each frontend app's path, stack, run command, and e2e/UI test command from what you find, and reuse those concrete values in the Change Impact Tree and validation.

Then classify the request as exactly one of:

- **Full-stack:** backend behavior plus a user-visible change. Plan the affected frontend app(s) as first-class: components, routes/pages, state, the API client call that hits the new/changed backend endpoint, and type/contract sync. These must appear in the Change Impact Tree and in validation.
- **Frontend-only:** UI/UX change with no backend contract change. Plan the frontend with full concreteness; note that no backend layer changes.
- **Backend-only:** no user-visible surface (internal CLI, worker job, migration, infra). The PRD must state `No frontend impact` with a one-line reason, so omission is a documented decision rather than a silent default.

**Hard rule:** if the request changes anything a user sees or interacts with, the PRD MUST plan the frontend with the same concreteness as the backend. A lone "update UI" line is not acceptable — name the app, the components/routes, and the API wiring. If more than one frontend could plausibly host the surface, ask the user which one in Phase 2 instead of guessing.

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

Identify the highest-fidelity validation that proves the behavior through a real project entry point, and record it in the Section 6 **Realistic Validation Plan** (content rule F defines the table columns, mock boundary, opt-in live, and fallback rules).

**Hard rule:** if the PRD introduces or changes executable behavior (CLI, API, jobs, file output, external integrations, or user-visible frontend), the plan MUST contain at least one row exercising a real entry point; user-visible changes need at least one real frontend entry point (the repo's e2e/UI test command or a manual app run), not only a unit test. "Unit tests are sufficient" is acceptable only for pure internal refactoring with no executable surface. Do not require live external services by default — gate them behind opt-in env vars and document the no-credential fallback.

For delivery/archive readiness, run the bundled checker when a Python runtime is available (resolve `scripts/check_prd_acceptance_checklist.py` relative to this skill directory, do not hard-code a path):

```bash
python scripts/check_prd_acceptance_checklist.py --repo-root <repo-root> --all
```

Pending PRDs may keep unchecked acceptance items, so this completion checker is not a blocker for a normal newly generated PRD; for a pending PRD about to be archived, validate it with `--check-provided tasks/pending/<prd-file>.md`.

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

Always include a **Change Impact Tree** and at least one **flow/architecture diagram** (content rules A, B). Add a **low-fidelity prototype** (rule C) only when the request is UI-heavy, depends on multi-step interaction, or layout is needed to resolve scope; add an **ER diagram** (rule D) only when the data model or persistent state changes.

Create or modify interactive prototype files under `docs/prototypes/` only when the user explicitly asks for a prototype/wireframe/demo, or static diagrams cannot express the behavior. Skipping a prototype file does not waive the Frontend Impact Gate — the frontend must still be planned in the Change Impact Tree and validation when the user-visible surface changes.

### Phase 5.5: Executor Resilience Gate

Implementation detail may be thorough, but must survive normal repository drift. Anchor fragile edits to file paths, symbol/recipe/route names, config keys, or headings — never to line numbers or line ranges. Include `rg` searches for legacy, new-target, and likely-hidden references when repo-wide references exist, and state that the listed files are a starting point, not an exhaustive set. Keep every shell command copy-paste executable (prefer `rg`; if `grep` alternation is used, use `grep -E 'a|b'`), add a short failure-triage note for risky commands (build context, CI working dir, cache/artifact path, route, env var, composition root), and mark live/credential-dependent validation as opt-in or post-merge unless truly required.

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

Before handing off, verify the document against the **Checklist** at the end of this skill — that list is the single source of truth for required sections, blocks, and blockers. Use `rg -n "^## " <prd-file>` for a quick section-header check.

When updating an existing PRD, run the Checklist against the entire file. If the existing file is non-compliant, preserve valid context and decisions but reorganize the document into the required structure instead of appending a compliant fragment to a non-compliant PRD.

**If the PRD changes executable behavior and the Realistic Validation Plan contains only unit/integration test entries with no real entry point, or the Validation Acceptance lacks a real entry-point item without a justified internal-refactoring exception, this gate FAILS. Do not hand off the PRD.**

---

## Required PRD Structure

This structure is the output contract for generated and updated PRDs.

### 1. Introduction & Goals

Brief problem statement, proposed solution summary, and measurable objectives.

Must include a concise `### Proposed Solution Summary` before the measurable objectives.
This summary should give reviewers the implementation direction before they reach Section 5 or Section 6:

- name the core mechanism or architecture path being recommended
- state who supplies any required declaration, configuration, or input, and whether the system infers it or only consumes explicit data
- identify the existing entry point, module boundary, API, workflow, or UI surface it plugs into
- state the main system state, output, or user-visible behavior change
- state what important complexity is intentionally avoided, such as new storage, a parallel abstraction, or a changed state machine

Must include a `### Realistic Validation` checklist before Section 2.
This first-section checklist is a concise, reviewer-facing summary of the highest-fidelity validation expected for the task. It must use Markdown checkbox items and mirror the style of:

```markdown
### Realistic Validation

除单元测试和集成测试外，本 PRD 要求通过**真实项目入口点**验证关键行为，确保真实使用路径生效，而非仅在隔离 fixture 中通过。

- [ ] **[行为名称] 真实验证**：通过 `[真实入口命令或流程]` 验证 `[关键可观察结果]`。
- [ ] **[配置/状态/回退] 真实验证**：通过 `[真实入口命令或流程]` 验证 `[关键可观察结果]`。

**为什么单元测试不够**：说明真实入口验证覆盖了哪些单元测试无法证明的行为。
```

Rules:
- Keep this checklist short enough to scan, usually 2-5 items.
- Use concrete real entry points such as CLI commands, HTTP endpoints, app startup, Playwright flows, worker jobs, migrations, or publish/deploy procedures.
- Include dry-run, local file output, sandbox mode, or mocked external boundary details when live services are not required.
- The checkbox items are actionable real-entry verifications. Capture "为什么单元测试不够" (why unit tests are insufficient) as a one-line rationale beneath the checklist, not as a checkbox, since it states reasoning rather than a task to complete.
- This checklist does not replace the detailed `Realistic Validation Plan` table in Section 6.

Must include a `### Delivery Dependencies` block after the first-section `### Realistic Validation` checklist and before Section 2.
This block is tool-neutral sequencing metadata, not a tool-specific queue syntax.
Use `none` explicitly when the task has no dependency.

Use this shape:

```markdown
### Delivery Dependencies

- Group: [logical-delivery-group-or-none]
- Depends on groups:
  - none
- Depends on tasks/issues:
  - none
- Gate type: none
- Notes: [Use tool-neutral dependency names. Do not put tool-specific hidden markers here.]
```

Rules:
- `Group` names the logical delivery group for this PRD, or `none`.
- `Depends on groups` lists logical upstream groups, not tool-specific labels.
- `Depends on tasks/issues` lists upstream task names, PRD slugs, issue numbers, or `none`.
- `Gate type` must be `none`, `soft`, or `hard`.
- `hard` means an execution tool may treat the dependency as a blocking gate when that repository has a deterministic adapter.
- `soft` documents sequencing context but must not be treated as a blocking gate unless a repository-specific PRD explicitly defines that behavior.
- Do not place tool-specific hidden markers, labels, or queue syntax in this block. Repository-specific publish tooling may translate the block into its own markers or labels.

### 2. Usage And Impact After Implementation

Place this immediately after the goals so reviewers and the requester see the concrete delivered outcome before the requirement, architecture, and implementation detail. Write it at PRD time as a target end state — a usage script to build toward and verify against — not a post-hoc log.

Required when the change is user-visible or has executable behavior (API/CLI/UI/job/startup/migration). For a purely internal change with no user-facing or executable surface, keep the section and state `No user-facing usage change; internal-only change.`

See the `Usage And Impact After Implementation` content rule for the per-role walkthrough, entry commands/API examples, backward-compatibility impact, and anti-duplication rules.

### 3. Requirement Shape

- actor
- trigger
- expected behavior
- explicit scope boundary

### 4. Repository Context And Architecture Fit

Must include:
- current relevant modules/files
- existing architecture pattern to follow
- ownership and dependency boundaries
- frontend impact: the affected frontend app(s) and the closest existing routes/components, or `No frontend impact` with a reason
- constraints from runtime, docs, tests, or workflows
- matching or related PRDs found in `tasks/pending/` and relevant prior PRDs from `tasks/archive/`

If no related PRDs are found, state that explicitly.
If related PRDs are found, identify whether this PRD:

- duplicates existing pending work and should update that PRD instead
- depends on another pending PRD
- blocks another pending PRD
- can run independently

Reflect dependency decisions in the first-section `Delivery Dependencies` block.

### 5. Recommendation

Must include:
- **Recommended Approach**
- why this is the best fit for the current architecture
- rationale for rejecting redundant abstractions
- **Alternatives Considered** only when a plausible non-trivial alternative exists

### 6. Implementation Guide

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

### 7. Acceptance Checklist

Include:
- a dedicated section named `Acceptance Checklist`
- grouped checklist headings such as `Architecture Acceptance`, `Dependency Acceptance`, `Behavior Acceptance`, `Frontend Acceptance` (when a frontend app changes), `Documentation Acceptance`, `Validation Acceptance`, and `Delivery Readiness` (the overall delivery gate formerly in Definition Of Done) when relevant
- concrete, repository-verifiable checkbox items
- exact paths, API contracts, commands, or search assertions where applicable
- at least one `Validation Acceptance` item that exercises the changed behavior through the highest feasible real entry point; if no real entry-point validation is included, the PRD must explicitly document that the change is pure internal refactoring with no executable surface, and this justification must be reviewed in the Decision Log
- this checklist is the single completion gate; do not replace any item with a vague summary bullet or local requirement acceptance notes

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
- **Chosen** must match the recommendation in Section 5.
- **Rejected** must name the concrete alternative from Section 5 when one is documented, not a vague "other approaches".
- **Rationale** must be one concrete sentence — not "fits the architecture" but why specifically.
- Assign sequential IDs: D-01, D-02, …
- Minimum one row per PRD. Add rows for major trade-offs or alternatives explicitly resolved in Section 5.

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
Frontend (per the repo's frontend app(s))
Tests
Docs
```

内容重点覆盖：SQL 变化、字段变化、数据流变化、API 变化、ORM 变化、Domain Logic 变化、前端组件/路由/状态变化、前端调用后端 API 的客户端代码、UI 展示变化、类型同步、测试同步。

当需求触达用户可见界面时，Change Impact Tree 必须包含仓库实际前端 app 的具体改动：组件、路由/页面、状态、调用后端 API 的客户端代码、类型同步。禁止用一行"更新 UI"代替前端规划。

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

### H. Usage And Impact After Implementation

Describe the post-implementation world from the consumer's point of view, written at PRD time as a concrete target end state — not a post-hoc log filled in after delivery. It gives reviewers and the implementer a usage script to build toward and to verify against later.

Include this section when the change is user-visible or has executable behavior (API/CLI/UI/job/startup/migration). When the change is purely internal with no user-facing or executable surface, state instead:
- `No user-facing usage change; internal-only change.`

When included, cover only what the abstract sections cannot:
- **Per-role usage walkthrough:** for each affected role that actually applies (end user, admin, developer, operator), the concrete steps to use the delivered capability — which page/route, which entry point, which fields, and the resulting identifier or output format. Anchor to real paths and entry points, not line numbers.
- **Entry commands / API examples:** copy-paste `curl`, CLI, or client snippets that exercise the new or changed entry points. This is usually the only place these concrete examples live.
- **Impact on existing behavior:** backward-compatibility and migration effects only — what stays the same for existing users/data/config, plus any new optional config/env and its default-off behavior.

Rules:
- This subsection is the concrete walkthrough; keep `Goals`, `Functional Requirements`, and `Requirement Shape` abstract and do not restate them verbatim here.
- Do not introduce a capability that has no matching `FR-n`; if writing this subsection surfaces one, add the `FR` first.
- The impact list covers backward-compatibility/migration, not the "won't build" scope — keep negative scope in `Non-Goals` and do not duplicate it here.

### I. Acceptance Checklist

This section is required even when Functional Requirements already include acceptance criteria.
It is the single completion artifact and also serves as the overall delivery-readiness gate (the former Definition Of Done); there is no separate Definition Of Done section.

Use grouped subsections.
For architecture-heavy or refactor work, prefer:
- `Architecture Acceptance`
- `Dependency Acceptance`
- `Behavior Acceptance`
- `Frontend Acceptance` (when a frontend app changes)
- `Documentation Acceptance`
- `Validation Acceptance`
- `Delivery Readiness` (recommended approach fully implemented; no open regression or rollout blocker — the former Definition Of Done)

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
* [ ] Searched existing `tasks/pending/` PRDs for duplicate, prerequisite, blocking, or downstream work before creating/updating this PRD
* [ ] Checked relevant `tasks/archive/` PRDs when prior decisions or completed related work could affect the plan
* [ ] Identified the closest existing code path
* [ ] Documented the Existing PRD Relationship in Section 4 and reflected sequencing decisions in Delivery Dependencies
* [ ] Handled critical unresolved questions correctly: asked the user only when repository evidence was insufficient and the answer would materially affect the PRD
* [ ] Compared a minimal-change option against a heavier option
* [ ] Justified every new abstraction, dependency, or file path
* [ ] Rejected redundant layers where reuse was sufficient
* [ ] Section 1 includes a concise proposed solution summary before measurable objectives, including who supplies required declarations/configuration/input, so the PRD does not jump from problem statement directly to validation or implementation detail
* [ ] Section 1 includes a tool-neutral Delivery Dependencies block, using explicit `none` values when no sequencing dependency exists
* [ ] Section 1 includes a `### Realistic Validation` reviewer-facing checklist (2-5 items), separate from the Section 6 Realistic Validation Plan
* [ ] Included a Change Impact Tree with architecture-fit reasoning
* [ ] **BLOCKER:** Stated frontend impact explicitly — for user-visible features named the affected frontend app(s) and their changes (components, routes, API wiring) in the Change Impact Tree; for backend-only work recorded `No frontend impact` with a reason; never omitted the frontend silently
* [ ] For user-visible changes, the Realistic Validation Plan includes a real frontend entry point (the repo's e2e/UI test command or a manual app run), not only component unit tests
* [ ] For user-visible or executable-behavior changes, included a `Usage And Impact After Implementation` section with a per-role usage walkthrough and entry commands/API examples; for purely internal changes recorded `No user-facing usage change`
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
* [ ] Ran a section compliance check, manually or with `rg -n "^## " <prd-file>`; all required sections are present in order
* [ ] Functional Requirements use `FR-1`, `FR-2`, … identifiers, and Non-Goals + Risks And Follow-Ups sections are present
* [ ] Included a dedicated `Acceptance Checklist` section (the single completion gate; no separate Definition Of Done) and did not collapse it into local requirement notes
* [ ] **BLOCKER:** All validation/search commands are copy-paste executable; repository searches prefer `rg`, and any `grep` alternation uses an explicit compatible mode
* [ ] **BLOCKER:** Validation Acceptance includes the highest feasible real entry-point validation or explicitly documents why the change is pure internal refactoring with no executable surface
* [ ] Recommended a full target state rather than leaving required work in `Phase 2`, `follow-up`, or temporary compatibility layers unless a hard constraint was explicitly documented
* [ ] Decision Log has at least one row for each major trade-off or documented alternative resolved in Section 5
* [ ] Each Decision Log row names a concrete rejected alternative (not a vague "other approaches")
