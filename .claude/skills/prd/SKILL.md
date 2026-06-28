---
name: prd
description: "[Updated 2026-06-28] Generate an architecture-aware technical PRD split into two altitudes — a human review layer (Part A) and an executor build layer (Part B) — with a risk-tiered human review map, a front-loaded interpretation lock, and a risk-map-ordered acceptance evidence package for a single end-of-flow human review. Triggers on: create a prd, write prd for, plan this feature. Prioritizes reuse, minimal-change plans, required output compliance, realistic validation, and conditional web research."
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
11. **Two-Altitude Output:** Structure every PRD as **Part A · Review Layer** (problem, user-facing value, human review map, requirement shape) and **Part B · Build Layer** (mechanism, change tree, validation commands, dependency metadata). Part A must let a human accept or reject the work *without* reading implementation mechanism, file paths, commands, or scheduling metadata; all executor detail lives in Part B. Do not front-load Part A with mechanism — the historical failure mode was a first section so full of code/test/scheduling detail that human review was hard.
12. **Risk-Tiered Human Review Map:** Part A must contain a Human Review Map that classifies each change point by architecture layer, assigns a risk tier (layer gives the default, then risk factors — irreversibility, blast radius, security/money, correctness-criticality — adjust it), and routes it to either **human confirmation** or **executor + automated gate** (hook / test / architecture check). Keep the human-confirm set short and principled; over-flagging defeats the map.
13. **Two-Touch Autonomy + Evidence Package:** The operating model is two batched human touches with autonomous execution between them — up front the human approves the Agent's interpretation (Section 1) and the acceptance oracles (Section 2); at the end the human reads a risk-map-ordered **Acceptance Evidence Package** (Section 9). There is no mid-flow human gate: the Agent self-verifies as deeply as needed (many rounds, adversarial checks — tokens are cheaper than human attention). So "human confirmation" means **high evidence burden** (the item tops the end package with an executable oracle), not an interruption; and every Review Map row — high or low — must name executable evidence that would fail if the change were wrong.

---

## Workflow

### Phase 0: Rewrite The Request As An Implementable Claim

State in plain language:
- who wants what behavior
- under which conditions
- what changes in system state, API, UI, or workflow

If you cannot rewrite the request concretely, call that out before generating a PRD.

This restatement is the **Interpretation Echo** recorded in Section 1 and is the human's first of two touches — they approve it (and the Section 2 acceptance oracles) before autonomous implementation begins. A wrong-but-unconfirmed interpretation is the one failure downstream evidence cannot catch, so make the reading explicit and falsifiable ("I read this as X, not Y").

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

Identify the highest-fidelity validation that proves the behavior through a real project entry point, and record it in the Section 7 **Realistic Validation Plan** (content rule F defines the table columns, mock boundary, opt-in live, and fallback rules). Mirror it in plain language as the Part A "如何证明它生效" note in the Human Review Map.

**Hard rule:** if the PRD introduces or changes executable behavior (CLI, API, jobs, file output, external integrations, or user-visible frontend), the plan MUST contain at least one row exercising a real entry point; user-visible changes need at least one real frontend entry point (the repo's e2e/UI test command or a manual app run), not only a unit test. "Unit tests are sufficient" is acceptable only for pure internal refactoring with no executable surface. Do not require live external services by default — gate them behind opt-in env vars and document the no-credential fallback.

For delivery/archive readiness, run the bundled checker when a Python runtime is available (resolve `scripts/check_prd_acceptance_checklist.py` relative to this skill directory, do not hard-code a path):

```bash
python scripts/check_prd_acceptance_checklist.py --repo-root <repo-root> --all
```

Pending PRDs may keep unchecked acceptance items, so this completion checker is not a blocker for a normal newly generated PRD; for a pending PRD about to be archived, validate it with `--check-provided tasks/pending/<prd-file>.md`.

### Phase 3.6: Human Review Map Gate

Once the change surface is known (from the architecture analysis, recommendation, and Change Impact Tree), classify it for the Part A **Human Review Map** (Section 2). For each meaningful change point:

1. **Layer:** which architecture layer it lands in (`api` / `core` / `engines` / `infrastructure` / a frontend app). The layer sets the default intervention: `core` business logic leans human; `api` adapters and `infrastructure` plumbing lean executor + automated gate.
2. **Risk tier:** start from the layer default, then adjust with risk factors — **irreversibility, blast radius, security/money, correctness-criticality**.
3. **Intervention:** route to **human confirmation** or **executor + automated gate**, and name the concrete gate (a specific hook, test, or architecture check) when it is the latter.

Always flag these for human confirmation regardless of where the code lands:

- **Fixed zones:** core business logic / orchestration (`core/`); database structure / schema / migration (even under `infrastructure/`); security / auth / trust boundaries; external API contracts / breaking changes.
- **Cross-cutting triggers** (escalate any layer): money / billing / quota (when applicable); irreversible or destructive data operations (bulk delete, backfill, down-migration); concurrency / transaction / idempotency.

**Hard rule:** keep the human-confirm set short and justified — if everything is flagged, the map adds no signal. Anything not in a fixed zone and not hitting a trigger defaults to executor + automated gate; do not list it as human-confirm. In Section 2, name only the menu items the change actually hits and summarize all misses in one line — do not write `不涉及` for every unmatched zone. When a schema change is present, the ER diagram (Section 7.5) must be surfaced in the Human Review Map for human review. Every human-confirm change point must get a matching item under the `Human-Confirmed` group in the Section 9 Acceptance Checklist.

**Acceptance Oracle Lock (up front):** For every human-confirm row, name the executable oracle that locks its correct behavior — characterization/golden test for core logic; round-trip + migration up/down for schema; an actual unauthorized-access test for auth; contract/snapshot test for an external API. For executor + automated-gate rows, name a gate that genuinely discriminates *this* change's failure (a generic `build`/`lint` that would pass even if the change were wrong is not valid). These oracles are agreed up front, run continuously during autonomous implementation, and presented as the Section 9 Acceptance Evidence Package. A high-risk row with no definable oracle is flagged, not marked done.

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

This structure is the output contract for generated and updated PRDs. PRDs are organized into two altitudes, read top-down:

- **Part A · Review Layer** (Sections 1-4): what a human reads to accept or reject the work and to see where they must personally confirm. No implementation mechanism, file paths, commands, or scheduling metadata.
- **Part B · Build Layer** (Sections 5-13): what the executor (human or Agent) reads to implement. The human drills in only where the Part A Human Review Map points.

The PRD opens with a short two-altitude orientation note, then the heading `# Part A · 人审层 (Review Layer)`.

### 1. Introduction & Goals

Review-altitude only. Must include, in order:
- `### Problem Statement` — the pain, who feels it, why the current behavior is insufficient. Problem only; no solution, mechanism, files, or commands.
- `### Interpretation (解读回显)` — the Agent's plain-language restatement of how it read the request (from Phase 0), kept falsifiable ("read as X, not Y"); this is the human's up-front approval target, the first of the two human touches.
- `### What The User Gets` — plain-language description of the capability/behavior the consumer (end user / caller / operator) receives, from the consumer's point of view. No implementation mechanism or module paths — mechanism belongs in Section 6.
- `### Measurable Objectives` — measurable success criteria.

Do not place a proposed solution summary, validation commands, or delivery-dependency metadata here — those live in Sections 6, 7, and 8 respectively. The first section must stay reviewable without implementation detail.

### 2. Human Review Map (介入与风险地图)

The heart of the review layer: it decides how a human allocates attention. Must include:

- A **numbered reference menu** of fixed zones (① core business logic/orchestration `core/`; ② database structure/schema/migration even under `infrastructure/`; ③ security/auth/trust boundaries; ④ external API contracts/breaking changes) and cross-cutting triggers (⑤ money/billing/quota; ⑥ irreversible or destructive data operations; ⑦ concurrency/transaction/idempotency).
- A **命中的人审项 (hits)** list naming only the menu items this change actually triggers (or `本次无人工确认项` when none); each hit becomes a 人工确认 row in the table.
- A **未命中 (misses)** one-liner summarizing the remaining menu numbers as executor + automated gate — do not enumerate each miss as its own `不涉及` line.
- A **classification table** with columns: `改动点 | 架构层 | 风险 | 介入方式（人工确认=高证据负担 / 执行器+门禁=兜底） | 证据 / Oracle`. The last column **references the `rv-id`(s) in the Section 7.6 oracle block** (e.g. `rv-1, rv-3`), keeping Section 2 scannable; the oracle detail lives once in Section 7.6. Every human-confirm row must point to at least one `rv-id`; a low-risk executor row may name a failure-discriminating gate instead. A row with no nameable evidence is a red flag, not a pass.
- For each **未命中** item, add a one-line worst-case-if-wrong; if the worst case is severe or irreversible, it cannot be left as a miss.
- A **"如何证明它生效（真实入口，白话）"** note — the plain-language mirror of the Section 7.6 Realistic Validation Plan, without command-level detail.
- A **数据库结构评审** note: when schema changes, surface the ER diagram here for human review; otherwise state `本次无数据库结构变化。`

Keep the human-confirm set short and principled (see Core Rules 12-13 and Phase 3.6). Anything not in a fixed zone and not hitting a trigger defaults to executor + automated gate and must not be listed as human-confirm. "人工确认" here means **high evidence burden** — the item tops the Section 9 evidence package with an executable oracle for the single end-of-flow review — not a mid-flow interruption.

### 3. Usage And Impact After Implementation

Part of the review layer so reviewers see the concrete delivered outcome before requirement and implementation detail. Write it at PRD time as a target end state — a usage script to build toward and verify against — not a post-hoc log.

Required when the change is user-visible or has executable behavior (API/CLI/UI/job/startup/migration). For a purely internal change with no user-facing or executable surface, keep the section and state `No user-facing usage change; internal-only change.`

See the `Usage And Impact After Implementation` content rule for the per-role walkthrough, entry commands/API examples, backward-compatibility impact, and anti-duplication rules.

### 4. Requirement Shape

- actor
- trigger
- expected behavior
- explicit scope boundary

The PRD then begins the build layer with the heading `# Part B · 执行器层 (Build Layer)`.

### 5. Repository Context And Architecture Fit

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

Reflect dependency decisions in the Section 8 `Delivery Dependencies` block.

### 6. Recommendation

Must include:
- **Recommended Approach**
- why this is the best fit for the current architecture
- rationale for rejecting redundant abstractions
- a `### Proposed Solution Summary (实现机制)` that hands the implementer the mechanism: name the core mechanism or architecture path; state who supplies any required declaration/configuration/input and whether the system infers it or only consumes explicit data; identify the existing entry point, module boundary, API, workflow, or UI surface it plugs into; state the main system state/output/user-visible behavior change; and state the complexity intentionally avoided (new storage, parallel abstraction, changed state machine)
- **Alternatives Considered** only when a plausible non-trivial alternative exists

### 7. Implementation Guide

This section must start with this sentence or a close equivalent:

> This section is a living implementation guide based on current repository analysis. If implementation discovers additional affected files, hidden dependencies, edge cases, or a better path, update this PRD before proceeding.

Must include:
- **Core Logic:** how data and control move through the existing system
- **Change Impact Tree**
- **Executor Drift Guard** when hidden references or repository drift could affect implementation
- **Flow or Architecture Diagram**
- **ER Diagram** when the data model changes (this is the detail figure linked from the Section 2 schema-review note)
- **Realistic Validation Plan** (a structured YAML oracle block — see content rule F)
- **Low-Fidelity Prototype** when required
- **Interactive Prototype Change Log** when prototype files changed
- **External Validation** when web research was used

### 8. Delivery Dependencies

Tool-neutral sequencing metadata, not a tool-specific queue syntax. Use `none` explicitly when the task has no dependency.

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

### 9. Acceptance Checklist

Include:
- a dedicated section named `Acceptance Checklist`
- this section is the **single human-facing acceptance artifact** ("look once at the end"): organize it as an **Acceptance Evidence Package** ordered by the Human Review Map — high-risk oracle results first, then the Review Map's Predicted→Reconciled reconciliation, then adversarial-check results on the misses, then diffs against locked contracts, then folded low-risk gate results
- every checkbox must be **evidence-bearing**: name the command output, observation, or artifact that proves it, not a bare claim
- a `Human-Confirmed` group whose checkbox items correspond one-to-one to the human-confirm change points in the Section 2 Human Review Map
- grouped checklist headings such as `Architecture Acceptance`, `Dependency Acceptance`, `Behavior Acceptance`, `Frontend Acceptance` (when a frontend app changes), `Documentation Acceptance`, `Validation Acceptance`, and `Delivery Readiness` (the overall delivery gate formerly in Definition Of Done) when relevant
- concrete, repository-verifiable checkbox items
- exact paths, API contracts, commands, or search assertions where applicable
- at least one `Validation Acceptance` item that exercises the changed behavior through the highest feasible real entry point; if no real entry-point validation is included, the PRD must explicitly document that the change is pure internal refactoring with no executable surface, and this justification must be reviewed in the Decision Log
- this checklist is the single completion gate; do not replace any item with a vague summary bullet or local requirement acceptance notes

### 10. Functional Requirements

Use numbered requirements such as `FR-1`, `FR-2`.

### 11. Non-Goals

List explicit out-of-scope items.

### 12. Risks And Follow-Ups

List only unavoidable migration risk, rollout risk, or explicitly approved non-blocking follow-up.
Do not use this section to park work that is actually required for the recommended target state.

### 13. Decision Log

Record every key decision made during this PRD as a permanent reference that survives archival.

Rules:
- Each row answers one decision question (e.g. "which architecture pattern", "which storage backend").
- **Chosen** must match the recommendation in Section 6.
- **Rejected** must name the concrete alternative from Section 6 when one is documented, not a vague "other approaches".
- **Rationale** must be one concrete sentence — not "fits the architecture" but why specifically.
- Assign sequential IDs: D-01, D-02, …
- Minimum one row per PRD. Add rows for major trade-offs or alternatives explicitly resolved in Section 6.

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

### F. Realistic Validation Plan (Oracle block)

Every PRD with executable behavior must include this as a **structured YAML block** — the single machine-and-human-readable oracle source. Section 2's evidence column, the Section 9 evidence package, and any deterministic extractor reference/parse the `id`s here. Do not restate oracles as prose tables elsewhere.

Each entry:

```yaml
- id: rv-1
  behavior: <user-visible behavior this proves, plain language>
  real_entry: "<exact command / URL / entry point the real user runs>"   # not a unit test or helper
  expected: "<observable that proves it works>"
  mock_boundary: "<what may be mocked vs must be real>"   # the under-test boundary must NOT be mocked
  negative_control: "<command or seeded break that makes this entry go RED>"   # proves the test can fail
  expected_fail: "<what red looks like>"
  test_layer: unit|integration|e2e|smoke|sandbox|manual
  required_for_acceptance: true
```

Rules:
- One entry per real observable behavior; every Section 2 human-confirm row points to ≥1 `id`.
- `real_entry` is the highest-fidelity real entry point (not pytest/helpers); for user-visible changes at least one entry's `real_entry` is the repo's e2e/UI command or a manual app run.
- `negative_control` + `expected_fail` are **mandatory for human-confirm / high-risk entries** — a test that cannot be shown to fail proves nothing. A purely mechanical low-risk entry may instead name a discriminating gate.
- `real_entry` / `negative_control` commands must be copy-paste executable from the documented working directory; prefer `rg`; any `grep` alternation uses `grep -E 'a|b'`.
- Treat production / vendor / credential-dependent entries as `opt-in` / `post-merge`; document the no-credential fallback that still runs.
- Add a short failure-triage note beneath the block (first config/path/boundary to inspect).
- Deterministic tooling parses this block; if a PRD declares executable behavior but the block is missing or unparseable, that is a loud failure — never infer it.

If the change has no executable behavior, state:
- `No executable behavior changes; realistic validation is limited to documentation/build checks.`

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
* [ ] Documented the Existing PRD Relationship in Section 5 and reflected sequencing decisions in the Section 8 Delivery Dependencies block
* [ ] Handled critical unresolved questions correctly: asked the user only when repository evidence was insufficient and the answer would materially affect the PRD
* [ ] Compared a minimal-change option against a heavier option
* [ ] Justified every new abstraction, dependency, or file path
* [ ] Rejected redundant layers where reuse was sufficient
* [ ] **BLOCKER:** Structured as Part A (Review Layer, Sections 1-4) and Part B (Build Layer, Sections 5-13); Part A contains no implementation mechanism, file paths, commands, or scheduling metadata
* [ ] Section 1 stays review-altitude: Problem Statement, an `Interpretation (解读回显)` of how the request was read (the up-front approval target), What The User Gets, and Measurable Objectives only — no proposed solution summary, validation commands, or delivery-dependency metadata
* [ ] **BLOCKER:** Section 2 Human Review Map present: a numbered zone/trigger menu, a 命中的人审项 list (only hit items, or `本次无人工确认项`), a 未命中 one-liner for the rest, a per-change-point classification table (layer + risk tier + intervention + 证据/Oracle column), a plain-language "如何证明它生效" note, and an ER-diagram surfacing or `本次无数据库结构变化` note
* [ ] Every Section 2 Review Map row points its 证据/Oracle column to ≥1 `rv-id` in the Section 7.6 oracle block (or a failure-discriminating gate name for low-risk executor rows); rows with no nameable evidence are flagged, not passed
* [ ] Each Section 2 未命中 item carries a one-line worst-case-if-wrong; severe or irreversible worst cases are not left as misses
* [ ] Human-confirm set is short and principled (fixed zones + triggers only); ordinary low-risk changes are routed to executor + automated gate, not flagged for human review
* [ ] Section 6 Recommendation includes the `Proposed Solution Summary (实现机制)` carrying the mechanism that moved out of Section 1
* [ ] Section 8 includes a tool-neutral Delivery Dependencies block, using explicit `none` values when no sequencing dependency exists
* [ ] Section 9 Acceptance Checklist includes a `Human-Confirmed` group whose items map one-to-one to the Section 2 human-confirm change points
* [ ] **BLOCKER:** Section 9 is organized as a risk-map-ordered Acceptance Evidence Package with evidence-bearing items (oracle results / observations / artifacts named), suitable for a single end-of-flow human review
* [ ] Included a Change Impact Tree with architecture-fit reasoning
* [ ] **BLOCKER:** Stated frontend impact explicitly — for user-visible features named the affected frontend app(s) and their changes (components, routes, API wiring) in the Change Impact Tree; for backend-only work recorded `No frontend impact` with a reason; never omitted the frontend silently
* [ ] For user-visible changes, the Realistic Validation Plan includes a real frontend entry point (the repo's e2e/UI test command or a manual app run), not only component unit tests
* [ ] For user-visible or executable-behavior changes, included a `Usage And Impact After Implementation` section with a per-role usage walkthrough and entry commands/API examples; for purely internal changes recorded `No user-facing usage change`
* [ ] **BLOCKER:** Did not include line-number-dependent edit instructions; all fragile edits use semantic anchors and/or `rg` search commands
* [ ] Included at least one flow or architecture diagram
* [ ] Implementation Guide includes the required living implementation guide statement
* [ ] Included an Executor Drift Guard when hidden references, moved paths, config rewires, or repository-wide updates are likely
* [ ] **BLOCKER:** Included a Realistic Validation Plan as a structured YAML oracle block (`id` / `real_entry` / `expected` / `mock_boundary` / `negative_control` / `expected_fail`) parseable by deterministic tooling; human-confirm / high-risk entries carry a `negative_control` + `expected_fail`
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
* [ ] Decision Log has at least one row for each major trade-off or documented alternative resolved in Section 6
* [ ] Each Decision Log row names a concrete rejected alternative (not a vague "other approaches")
