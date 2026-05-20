const prototypeShellElement = document.getElementById('prototype-shell');
const bannerElement = document.getElementById('banner');
const viewRootElement = document.getElementById('view-root');

const navButtonElementList = Array.from(document.querySelectorAll('.nav-button'));
const toolButtonElementList = Array.from(document.querySelectorAll('[data-action]'));

const skillPackageRecord = {
  skillName: 'planning-with-files',
  version: '2.4.0',
  description:
    'Manus-style file-based planning skill. Stores active planning files under .claude/planning/current/ and archives resets under .claude/planning/sessions/.',
  userInvocable: true,
  allowedToolNameList: ['Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'WebFetch', 'WebSearch'],
  fileBuckets: {
    docs: [
      { path: 'SKILL.md', purpose: 'Skill contract, frontmatter, rules, and workflow.' },
      { path: 'reference.md', purpose: 'Manus context engineering principles and architecture notes.' },
      { path: 'examples.md', purpose: 'Concrete usage examples and error recovery patterns.' },
    ],
    templates: [
      { path: 'templates/task_plan.md', purpose: 'Task phases and completion summary template.' },
      { path: 'templates/findings.md', purpose: 'Research and decisions template.' },
      { path: 'templates/progress.md', purpose: 'Execution log and reboot checklist template.' },
    ],
    scripts: [
      { path: 'scripts/init-session.sh', purpose: 'Initialize planning files in .claude/planning/current/.' },
      { path: 'scripts/check-complete.sh', purpose: 'Check whether all phases are complete.' },
      { path: 'scripts/update_phase_status.py', purpose: 'Auto-advance and status report helper.' },
      { path: 'scripts/task_plan.md', purpose: 'Script-side task_plan sample.' },
      { path: 'scripts/findings.md', purpose: 'Script-side findings sample.' },
      { path: 'scripts/progress.md', purpose: 'Script-side progress sample.' },
    ],
  },
  hookMap: {
    SessionStart: [
      "echo '[planning-with-files] Ready...'",
      'python update_phase_status.py --status-report',
    ],
    PreToolUse: ['cat .claude/planning/current/task_plan.md | head -30'],
    PostToolUse: [
      "echo '[planning-with-files] File updated...'",
      'python update_phase_status.py --auto-advance',
      'python update_phase_status.py --status-report',
    ],
    Stop: ['scripts/check-complete.sh', 'python update_phase_status.py --status-report'],
  },
};

const filePreviewMap = {
  'SKILL.md': 'Contains frontmatter fields: name, version, description, allowed-tools, hooks.',
  'reference.md': 'Documents 6 Manus principles and context window strategy.',
  'examples.md': 'Includes research task, bugfix task, and completion summary examples.',
  'templates/task_plan.md': 'Defines Goal, Current Phase, Phases, Decisions, Errors, Completion Summary.',
  'templates/findings.md': 'Defines Requirements, Research Findings, Technical Decisions, Issues.',
  'templates/progress.md': 'Defines per-phase logs, tests, error log, and 5-question reboot check.',
  'scripts/init-session.sh': 'Bootstraps planning files into .claude/planning/current/ and archives resets.',
  'scripts/check-complete.sh': 'Checks if all phases are marked complete before stop.',
  'scripts/update_phase_status.py': 'Supports --status-report, --auto-advance, --phase updates.',
  'scripts/task_plan.md': 'Sample status tracker used by automation scripts.',
  'scripts/findings.md': 'Sample findings file used by scripts.',
  'scripts/progress.md': 'Sample progress file used by scripts.',
};

const initialPhaseList = [
  { name: 'Phase 1: Requirements & Discovery', status: 'in_progress' },
  { name: 'Phase 2: Planning & Structure', status: 'pending' },
  { name: 'Phase 3: Implementation', status: 'pending' },
  { name: 'Phase 4: Testing & Verification', status: 'pending' },
  { name: 'Phase 5: Delivery', status: 'pending' },
];

const generatedSkillFileList = [
  { path: 'shipment-planner/SKILL.md', reason: 'Generated from repository capability summary and agent workflow.' },
  { path: 'shipment-planner/templates/task_plan.md', reason: 'Bootstrapped from planning template pattern.' },
  { path: 'shipment-planner/templates/findings.md', reason: 'Supports research notes and decisions for this domain.' },
  { path: 'shipment-planner/scripts/init-session.sh', reason: 'Creates task memory files in target project.' },
  { path: 'shipment-planner/references/domain-model.md', reason: 'Extracted key entities from GitHub code and docs.' },
];

const initialState = {
  activeViewKey: 'overview',
  densityKey: 'comfortable',
  selectedBucketKey: 'docs',
  selectedFilePath: 'SKILL.md',
  githubRepoUrl: 'https://github.com/openai/skills',
  githubRepoPath: 'skills/.curated/planning-with-files',
  githubRef: 'main',
  githubTargetSkillName: 'shipment-planner',
  githubCreateStage: 'idle',
  githubGeneratedReady: false,
  sessionInitialized: false,
  twoActionCounter: 0,
  artifactStateMap: {
    '.claude/planning/current/task_plan.md': false,
    '.claude/planning/current/findings.md': false,
    '.claude/planning/current/progress.md': false,
  },
  phaseList: initialPhaseList,
  hookEnabledMap: {
    SessionStart: true,
    PreToolUse: true,
    PostToolUse: true,
    Stop: true,
  },
  executionLogList: [],
  bannerMessageText: '',
  bannerToneKey: 'success',
};

let prototypeState = {
  ...initialState,
  phaseList: initialPhaseList.map((phaseRecord) => ({ ...phaseRecord })),
  executionLogList: ['[boot] Loaded planning-with-files prototype.'],
};

function nowTimeText() {
  return new Date().toLocaleTimeString();
}

function appendLog(logMessageText) {
  prototypeState = {
    ...prototypeState,
    executionLogList: [`[${nowTimeText()}] ${logMessageText}`, ...prototypeState.executionLogList].slice(0, 16),
  };
}

function setBanner(messageText, toneKey) {
  prototypeState = {
    ...prototypeState,
    bannerMessageText: messageText,
    bannerToneKey: toneKey || 'success',
  };
}

function clearBanner() {
  prototypeState = {
    ...prototypeState,
    bannerMessageText: '',
    bannerToneKey: 'success',
  };
}

function resolveSelectedBucketFileList() {
  return skillPackageRecord.fileBuckets[prototypeState.selectedBucketKey] || [];
}

function resolveSelectedFileRecord() {
  const selectedBucketFileList = resolveSelectedBucketFileList();
  return selectedBucketFileList.find(
    (fileRecord) => fileRecord.path === prototypeState.selectedFilePath
  );
}

function resolveStatusBadgeClass(statusText) {
  if (statusText === 'complete') {
    return 'badge-success';
  }
  if (statusText === 'in_progress') {
    return 'badge-info';
  }
  return 'badge-warning';
}

function resolveGithubStageBadgeClass(stageText) {
  if (stageText === 'ready') {
    return 'badge-success';
  }
  if (stageText === 'generating' || stageText === 'analyzing') {
    return 'badge-info';
  }
  return 'badge-warning';
}

function renderOverviewViewHtml() {
  const docsCount = skillPackageRecord.fileBuckets.docs.length;
  const templatesCount = skillPackageRecord.fileBuckets.templates.length;
  const scriptsCount = skillPackageRecord.fileBuckets.scripts.length;
  const hooksCount = Object.keys(skillPackageRecord.hookMap).length;
  const artifactsReadyCount = Object.values(prototypeState.artifactStateMap).filter(Boolean).length;

  return `
    <div class="page">
      <section class="card">
        <div class="inline-row">
          <span class="badge badge-info">reference package</span>
          <span class="muted">This prototype is aligned with real files in <span class="mono">/home/atahang/.cc-switch/skills/planning-with-files/</span>.</span>
        </div>
      </section>

      <section class="metric-grid">
        <article class="metric-card"><p>Docs</p><strong>${docsCount}</strong></article>
        <article class="metric-card"><p>Templates</p><strong>${templatesCount}</strong></article>
        <article class="metric-card"><p>Scripts</p><strong>${scriptsCount}</strong></article>
        <article class="metric-card"><p>Hook Events</p><strong>${hooksCount}</strong></article>
        <article class="metric-card"><p>Project Artifacts</p><strong>${artifactsReadyCount}/3</strong></article>
      </section>

      <section class="card">
        <div class="page-header">
          <div>
            <h3>Skill Package Metadata</h3>
            <p>Skill in this model is a package contract, not only runtime mounts.</p>
          </div>
        </div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>Field</th>
                <th>Value</th>
              </tr>
            </thead>
            <tbody>
              <tr><td>name</td><td>${skillPackageRecord.skillName}</td></tr>
              <tr><td>version</td><td>${skillPackageRecord.version}</td></tr>
              <tr><td>user-invocable</td><td><span class="badge badge-success">${skillPackageRecord.userInvocable}</span></td></tr>
              <tr><td>description</td><td>${skillPackageRecord.description}</td></tr>
              <tr><td>allowed-tools</td><td>${skillPackageRecord.allowedToolNameList.join(', ')}</td></tr>
              <tr><td>github-agent-create</td><td>Enabled in prototype view: <span class="mono">GitHub Agent Create</span></td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  `;
}

function renderStructureViewHtml() {
  const selectedBucketFileList = resolveSelectedBucketFileList();
  const selectedFileRecord = resolveSelectedFileRecord();
  const selectedFilePreviewText = selectedFileRecord
    ? filePreviewMap[selectedFileRecord.path] || selectedFileRecord.purpose
    : 'No file selected.';

  const fileListHtml = selectedBucketFileList
    .map(
      (fileRecord) => `
        <button class="file-item ${fileRecord.path === prototypeState.selectedFilePath ? 'is-active' : ''}" data-action="select-file" data-file-path="${fileRecord.path}">
          <strong>${fileRecord.path}</strong>
          <p class="muted">${fileRecord.purpose}</p>
        </button>
      `
    )
    .join('');

  return `
    <div class="page">
      <section class="card">
        <div class="page-header">
          <div>
            <h3>Package Structure</h3>
            <p>Browse real package sections: docs, templates, and scripts.</p>
          </div>
          <div class="field" style="min-width: 260px;">
            <label for="bucket-selector">Section</label>
            <select id="bucket-selector">
              <option value="docs" ${prototypeState.selectedBucketKey === 'docs' ? 'selected' : ''}>docs</option>
              <option value="templates" ${prototypeState.selectedBucketKey === 'templates' ? 'selected' : ''}>templates</option>
              <option value="scripts" ${prototypeState.selectedBucketKey === 'scripts' ? 'selected' : ''}>scripts</option>
            </select>
          </div>
        </div>

        <div class="two-col">
          <div class="file-list">${fileListHtml}</div>
          <div class="card" style="margin-bottom: 0;">
            <h3>File Detail</h3>
            <p class="mono">${selectedFileRecord ? selectedFileRecord.path : '-'}</p>
            <p class="muted" style="margin-top: 8px;">${selectedFilePreviewText}</p>
          </div>
        </div>
      </section>
    </div>
  `;
}

function renderGithubCreateViewHtml() {
  const stage = prototypeState.githubCreateStage;
  const stageCardState = {
    ingest: stage !== 'idle',
    analyze: stage === 'analyzing' || stage === 'generating' || stage === 'ready',
    scaffold: stage === 'generating' || stage === 'ready',
    review: stage === 'ready',
  };

  const stageCardClass = (key) => {
    if (stageCardState[key] && key === 'review') {
      return 'stage-card is-done';
    }
    if (stageCardState[key]) {
      return 'stage-card is-active';
    }
    return 'stage-card';
  };

  const generatedFileRowsHtml = generatedSkillFileList
    .map(
      (fileRecord) => `
        <div class="generated-file-item">
          <strong class="mono">${fileRecord.path}</strong>
          <p class="muted">${fileRecord.reason}</p>
        </div>
      `
    )
    .join('');

  return `
    <div class="page">
      <section class="card">
        <div class="page-header">
          <div>
            <h3>GitHub Code -> Agent-assisted Skill Creation</h3>
            <p>Import repository code context, let agent draft a skill package, then review before publish.</p>
          </div>
          <span class="badge ${resolveGithubStageBadgeClass(stage)}">${stage}</span>
        </div>

        <div class="stage-grid">
          <article class="${stageCardClass('ingest')}">
            <strong>1. Repo Ingest</strong>
            <p>Collect repo URL, path, and ref.</p>
          </article>
          <article class="${stageCardClass('analyze')}">
            <strong>2. Agent Analyze</strong>
            <p>Map code structure into candidate skill boundaries.</p>
          </article>
          <article class="${stageCardClass('scaffold')}">
            <strong>3. Draft Scaffold</strong>
            <p>Generate SKILL.md + scripts/templates/references.</p>
          </article>
          <article class="${stageCardClass('review')}">
            <strong>4. Review & Publish</strong>
            <p>Human approves before final installation.</p>
          </article>
        </div>
      </section>

      <section class="card">
        <div class="input-grid">
          <div class="field">
            <label for="github-repo-url">GitHub Repo URL</label>
            <input id="github-repo-url" type="text" value="${prototypeState.githubRepoUrl}" />
          </div>
          <div class="field">
            <label for="github-repo-path">Skill/Code Path</label>
            <input id="github-repo-path" type="text" value="${prototypeState.githubRepoPath}" />
          </div>
          <div class="field">
            <label for="github-ref">Git Ref</label>
            <input id="github-ref" type="text" value="${prototypeState.githubRef}" />
          </div>
          <div class="field">
            <label for="github-target-skill-name">Target Skill Name</label>
            <input id="github-target-skill-name" type="text" value="${prototypeState.githubTargetSkillName}" />
          </div>
        </div>

        <div class="inline-row" style="margin-top: 12px;">
          <button class="button button-primary" data-action="github-start-analysis">Analyze Repo</button>
          <button class="button button-secondary" data-action="github-generate-skill" ${stage === 'analyzing' || stage === 'generating' || stage === 'ready' ? '' : 'disabled'}>Generate Skill Draft</button>
          <button class="button button-primary" data-action="github-approve-skill" ${stage === 'generating' || stage === 'ready' ? '' : 'disabled'}>Approve & Publish</button>
        </div>
      </section>

      ${
        prototypeState.githubGeneratedReady
          ? `
        <section class="card">
          <h3>Generated Skill Draft (${prototypeState.githubTargetSkillName})</h3>
          <div class="generated-file-list" style="margin-top: 10px;">
            ${generatedFileRowsHtml}
          </div>
        </section>
      `
          : ''
      }
    </div>
  `;
}

function renderWorkflowViewHtml() {
  const artifactRowsHtml = Object.entries(prototypeState.artifactStateMap)
    .map(
      ([fileName, isReady]) => `
        <tr>
          <td class="mono">${fileName}</td>
          <td><span class="badge ${isReady ? 'badge-success' : 'badge-warning'}">${isReady ? 'created' : 'missing'}</span></td>
        </tr>
      `
    )
    .join('');

  const phaseRowsHtml = prototypeState.phaseList
    .map(
      (phaseRecord) => `
        <div class="phase-item">
          <div class="inline-row">
            <strong>${phaseRecord.name}</strong>
            <span class="badge ${resolveStatusBadgeClass(phaseRecord.status)}">${phaseRecord.status}</span>
          </div>
        </div>
      `
    )
    .join('');

  return `
    <div class="page">
      <section class="card">
        <div class="page-header">
          <div>
            <h3>Execution Workflow Simulation</h3>
            <p>Simulates init-session, 2-action rule, and phase auto-advance from planning-with-files.</p>
          </div>
        </div>

        <div class="inline-row">
          <button class="button button-primary" data-action="init-session">Init Session</button>
          <button class="button button-secondary" data-action="simulate-two-actions">+2 View/Search Ops</button>
          <button class="button button-secondary" data-action="save-findings">Save Findings</button>
          <button class="button button-primary" data-action="auto-advance-phase">Auto Advance Phase</button>
          <span class="badge ${prototypeState.twoActionCounter >= 2 ? 'badge-warning' : 'badge-success'}">2-action counter: ${prototypeState.twoActionCounter}</span>
        </div>
      </section>

      <section class="two-col">
        <article class="card" style="margin-bottom: 0;">
          <h3>Project Artifacts</h3>
          <div class="table-wrap" style="margin-top: 10px;">
            <table class="table">
              <thead>
                <tr><th>File</th><th>Status</th></tr>
              </thead>
              <tbody>${artifactRowsHtml}</tbody>
            </table>
          </div>
        </article>

        <article class="card" style="margin-bottom: 0;">
          <h3>Phase Status</h3>
          <div class="phase-list">${phaseRowsHtml}</div>
        </article>
      </section>
    </div>
  `;
}

function renderHooksViewHtml() {
  const hookRowsHtml = Object.entries(skillPackageRecord.hookMap)
    .map(
      ([hookName, commandList]) => `
        <tr>
          <td>${hookName}</td>
          <td>${commandList.join(' ; ')}</td>
          <td><span class="badge ${prototypeState.hookEnabledMap[hookName] ? 'badge-success' : 'badge-warning'}">${prototypeState.hookEnabledMap[hookName] ? 'enabled' : 'disabled'}</span></td>
          <td>
            <div class="inline-row">
              <button class="button button-secondary" data-action="toggle-hook" data-hook-name="${hookName}">${prototypeState.hookEnabledMap[hookName] ? 'Disable' : 'Enable'}</button>
              <button class="button button-primary" data-action="trigger-hook" data-hook-name="${hookName}">Trigger</button>
            </div>
          </td>
        </tr>
      `
    )
    .join('');

  const logRowsHtml = prototypeState.executionLogList
    .map((logText) => `<div class="log-row">${logText}</div>`)
    .join('');

  return `
    <div class="page">
      <section class="card">
        <div class="page-header">
          <div>
            <h3>Hook Events Simulator</h3>
            <p>Simulate SessionStart / PreToolUse / PostToolUse / Stop hooks and inspect resulting logs.</p>
          </div>
        </div>

        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>Hook Event</th>
                <th>Commands</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>${hookRowsHtml}</tbody>
          </table>
        </div>
      </section>

      <section class="card" style="margin-bottom: 0;">
        <h3>Execution Log</h3>
        <div class="log-wrap">${logRowsHtml}</div>
      </section>
    </div>
  `;
}

function render() {
  if (prototypeState.activeViewKey === 'overview') {
    viewRootElement.innerHTML = renderOverviewViewHtml();
  }

  if (prototypeState.activeViewKey === 'structure') {
    viewRootElement.innerHTML = renderStructureViewHtml();
  }

  if (prototypeState.activeViewKey === 'github-create') {
    viewRootElement.innerHTML = renderGithubCreateViewHtml();
  }

  if (prototypeState.activeViewKey === 'workflow') {
    viewRootElement.innerHTML = renderWorkflowViewHtml();
  }

  if (prototypeState.activeViewKey === 'hooks') {
    viewRootElement.innerHTML = renderHooksViewHtml();
  }

  bannerElement.classList.toggle('hidden', !prototypeState.bannerMessageText);
  bannerElement.classList.toggle('banner-error', prototypeState.bannerToneKey === 'error');
  bannerElement.textContent = prototypeState.bannerMessageText;

  prototypeShellElement.dataset.density = prototypeState.densityKey;

  navButtonElementList.forEach((navButtonElement) => {
    navButtonElement.classList.toggle('is-active', navButtonElement.dataset.view === prototypeState.activeViewKey);
  });
}

function resetState() {
  prototypeState = {
    ...initialState,
    phaseList: initialPhaseList.map((phaseRecord) => ({ ...phaseRecord })),
    executionLogList: ['[boot] Loaded planning-with-files prototype.'],
  };
}

function onAction(actionKey, triggerElement) {
  if (actionKey === 'toggle-density') {
    prototypeState = {
      ...prototypeState,
      densityKey: prototypeState.densityKey === 'comfortable' ? 'dense' : 'comfortable',
    };
    render();
    return;
  }

  if (actionKey === 'reset') {
    resetState();
    render();
    return;
  }

  if (actionKey === 'select-file') {
    const nextFilePath = triggerElement.dataset.filePath;
    prototypeState = {
      ...prototypeState,
      selectedFilePath: nextFilePath || prototypeState.selectedFilePath,
    };
    clearBanner();
    render();
    return;
  }

  if (actionKey === 'github-start-analysis') {
    if (!prototypeState.githubRepoUrl.trim() || !prototypeState.githubRepoPath.trim()) {
      setBanner('Repository URL and path are required before analysis.', 'error');
      render();
      return;
    }
    prototypeState = {
      ...prototypeState,
      githubCreateStage: 'analyzing',
      githubGeneratedReady: false,
    };
    appendLog(
      `Agent analyzing ${prototypeState.githubRepoUrl} (${prototypeState.githubRepoPath}@${prototypeState.githubRef}).`
    );
    setBanner('Repository analysis started. Agent is deriving skill boundaries.', 'success');
    render();
    return;
  }

  if (actionKey === 'github-generate-skill') {
    if (prototypeState.githubCreateStage === 'idle') {
      setBanner('Run repository analysis first.', 'error');
      render();
      return;
    }
    prototypeState = {
      ...prototypeState,
      githubCreateStage: 'generating',
      githubGeneratedReady: true,
    };
    appendLog(`Generated draft skill package: ${prototypeState.githubTargetSkillName}.`);
    setBanner('Draft skill files generated. Review package content before publish.', 'success');
    render();
    return;
  }

  if (actionKey === 'github-approve-skill') {
    if (!prototypeState.githubGeneratedReady) {
      setBanner('No generated draft to approve yet.', 'error');
      render();
      return;
    }
    prototypeState = {
      ...prototypeState,
      githubCreateStage: 'ready',
    };
    appendLog(
      `Approved ${prototypeState.githubTargetSkillName}. Ready for installer flow from GitHub source.`
    );
    setBanner('Skill draft approved. Ready to install/publish to skills directory.', 'success');
    render();
    return;
  }

  if (actionKey === 'init-session') {
    prototypeState = {
      ...prototypeState,
      sessionInitialized: true,
      artifactStateMap: {
        '.claude/planning/current/task_plan.md': true,
        '.claude/planning/current/findings.md': true,
        '.claude/planning/current/progress.md': true,
      },
    };
    appendLog('scripts/init-session.sh created .claude/planning/current/task_plan.md, findings.md, and progress.md');
    setBanner('Session initialized. Planning files are now present in .claude/planning/current/.', 'success');
    render();
    return;
  }

  if (actionKey === 'simulate-two-actions') {
    prototypeState = {
      ...prototypeState,
      twoActionCounter: prototypeState.twoActionCounter + 2,
    };
    appendLog('2 view/search operations executed. Findings update expected by rule.');
    if (prototypeState.twoActionCounter >= 2) {
      setBanner('2-action rule hit: update findings.md now.', 'error');
    }
    render();
    return;
  }

  if (actionKey === 'save-findings') {
    if (!prototypeState.sessionInitialized) {
      setBanner('Initialize session first so findings.md exists.', 'error');
      render();
      return;
    }
    prototypeState = {
      ...prototypeState,
      twoActionCounter: 0,
    };
    appendLog('findings.md updated after search/view operations.');
    setBanner('Findings saved. 2-action counter reset.', 'success');
    render();
    return;
  }

  if (actionKey === 'auto-advance-phase') {
    if (!prototypeState.sessionInitialized) {
      setBanner('Initialize session before phase auto-advance.', 'error');
      render();
      return;
    }

    const nextPhaseList = prototypeState.phaseList.map((phaseRecord) => ({ ...phaseRecord }));
    const currentPhaseIndex = nextPhaseList.findIndex(
      (phaseRecord) => phaseRecord.status === 'in_progress'
    );

    if (currentPhaseIndex === -1) {
      const firstPendingIndex = nextPhaseList.findIndex((phaseRecord) => phaseRecord.status === 'pending');
      if (firstPendingIndex !== -1) {
        nextPhaseList[firstPendingIndex].status = 'in_progress';
        appendLog(`Auto-advance set ${nextPhaseList[firstPendingIndex].name} to in_progress.`);
      }
    } else {
      nextPhaseList[currentPhaseIndex].status = 'complete';
      const upcomingPhaseIndex = nextPhaseList.findIndex(
        (phaseRecord, phaseIndex) => phaseIndex > currentPhaseIndex && phaseRecord.status === 'pending'
      );
      if (upcomingPhaseIndex !== -1) {
        nextPhaseList[upcomingPhaseIndex].status = 'in_progress';
      }
      appendLog(`Auto-advance completed ${nextPhaseList[currentPhaseIndex].name}.`);
    }

    prototypeState = {
      ...prototypeState,
      phaseList: nextPhaseList,
    };
    setBanner('Phase status updated via update_phase_status.py logic.', 'success');
    render();
    return;
  }

  if (actionKey === 'toggle-hook') {
    const hookName = triggerElement.dataset.hookName;
    if (!hookName) {
      return;
    }
    prototypeState = {
      ...prototypeState,
      hookEnabledMap: {
        ...prototypeState.hookEnabledMap,
        [hookName]: !prototypeState.hookEnabledMap[hookName],
      },
    };
    appendLog(`${hookName} is now ${prototypeState.hookEnabledMap[hookName] ? 'enabled' : 'disabled'}.`);
    clearBanner();
    render();
    return;
  }

  if (actionKey === 'trigger-hook') {
    const hookName = triggerElement.dataset.hookName;
    if (!hookName) {
      return;
    }
    if (!prototypeState.hookEnabledMap[hookName]) {
      appendLog(`${hookName} skipped because hook is disabled.`);
      setBanner(`${hookName} is disabled; no hook command executed.`, 'error');
      render();
      return;
    }

    const commandList = skillPackageRecord.hookMap[hookName] || [];
    appendLog(`${hookName} triggered (${commandList.length} command(s)).`);
    commandList.forEach((commandText) => {
      appendLog(`run: ${commandText}`);
    });
    setBanner(`${hookName} hook executed and log appended.`, 'success');
    render();
  }
}

navButtonElementList.forEach((navButtonElement) => {
  navButtonElement.addEventListener('click', () => {
    prototypeState = {
      ...prototypeState,
      activeViewKey: navButtonElement.dataset.view || 'overview',
    };
    clearBanner();
    render();
  });
});

toolButtonElementList.forEach((toolButtonElement) => {
  toolButtonElement.addEventListener('click', () => {
    const actionKey = toolButtonElement.dataset.action;
    if (!actionKey) {
      return;
    }
    onAction(actionKey, toolButtonElement);
  });
});

viewRootElement.addEventListener('click', (event) => {
  const targetElement = event.target;
  if (!(targetElement instanceof HTMLElement)) {
    return;
  }
  const actionTriggerElement = targetElement.closest('[data-action]');
  if (!(actionTriggerElement instanceof HTMLElement)) {
    return;
  }
  const actionKey = actionTriggerElement.dataset.action;
  if (!actionKey) {
    return;
  }
  onAction(actionKey, actionTriggerElement);
});

viewRootElement.addEventListener('change', (event) => {
  const targetElement = event.target;
  if (!(targetElement instanceof HTMLSelectElement)) {
    return;
  }

  if (targetElement.id === 'bucket-selector') {
    const nextBucketKey = targetElement.value;
    const nextBucketFileList = skillPackageRecord.fileBuckets[nextBucketKey] || [];
    prototypeState = {
      ...prototypeState,
      selectedBucketKey: nextBucketKey,
      selectedFilePath: nextBucketFileList[0] ? nextBucketFileList[0].path : '',
    };
    clearBanner();
    render();
  }
});

viewRootElement.addEventListener('input', (event) => {
  const targetElement = event.target;
  if (!(targetElement instanceof HTMLInputElement)) {
    return;
  }

  if (targetElement.id === 'github-repo-url') {
    prototypeState = {
      ...prototypeState,
      githubRepoUrl: targetElement.value,
    };
    return;
  }

  if (targetElement.id === 'github-repo-path') {
    prototypeState = {
      ...prototypeState,
      githubRepoPath: targetElement.value,
    };
    return;
  }

  if (targetElement.id === 'github-ref') {
    prototypeState = {
      ...prototypeState,
      githubRef: targetElement.value,
    };
    return;
  }

  if (targetElement.id === 'github-target-skill-name') {
    prototypeState = {
      ...prototypeState,
      githubTargetSkillName: targetElement.value,
    };
  }
});

render();
