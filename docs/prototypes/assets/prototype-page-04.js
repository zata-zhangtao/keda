const prototypeShellElement = document.getElementById('prototype-shell');
const statusBannerElement = document.getElementById('status-banner');
const viewCategoryElement = document.getElementById('view-category');
const viewTitleElement = document.getElementById('view-title');
const viewActionsElement = document.getElementById('view-actions');
const kpiGridElement = document.getElementById('kpi-grid');
const primaryPanelElement = document.getElementById('primary-panel');
const secondaryPanelElement = document.getElementById('secondary-panel');

const navButtonElementList = Array.from(document.querySelectorAll('.nav-btn'));
const toolButtonElementList = Array.from(document.querySelectorAll('[data-action]'));

const initialPrototypeState = {
  activeViewKey: 'overview',
  densityKey: 'comfortable',
  alertVisible: false,
};

let prototypeState = { ...initialPrototypeState };

const viewConfigByKey = {
  overview: {
    categoryText: 'Dashboard',
    titleText: 'Overview',
    actionLabelList: ['Export', 'Deploy New Agent'],
    kpiDataList: [
      { labelText: 'Total Agents', valueText: '42' },
      { labelText: 'Active Sessions', valueText: '1,284' },
      { labelText: 'API Usage', valueText: '842.5k' },
      { labelText: 'Avg Latency', valueText: '240ms' },
    ],
    panelTitleText: 'Traffic Snapshot',
    rowDataList: [
      ['Window', 'Requests', 'P95'],
      ['Last 1h', '10,229', '232ms'],
      ['Last 6h', '54,108', '244ms'],
      ['Last 24h', '183,940', '248ms'],
    ],
    noteTitleText: 'Model Distribution',
    noteList: ['Gemini 3.1 Pro: 65%', 'Gemini 3 Flash: 25%', 'Custom Fine-tuned: 10%'],
  },
  agents: {
    categoryText: 'Management',
    titleText: 'Agents',
    actionLabelList: ['Create Agent'],
    kpiDataList: [
      { labelText: 'Active', valueText: '31' },
      { labelText: 'Idle', valueText: '8' },
      { labelText: 'Maintenance', valueText: '3' },
      { labelText: 'Success Rate', valueText: '98.5%' },
    ],
    panelTitleText: 'Agent Table',
    rowDataList: [
      ['Name', 'Status', '24h Requests'],
      ['Customer Support Alpha', 'Active', '12,402'],
      ['Data Analyst Pro', 'Active', '8,129'],
      ['Translation Engine', 'Maintenance', '0'],
    ],
    noteTitleText: 'Operational Signals',
    noteList: ['Use status badges in every row.', 'Expose model and owner in detail drawer.', 'Add bulk actions in phase 2 only.'],
  },
  skills: {
    categoryText: 'Capabilities',
    titleText: 'Skills',
    actionLabelList: ['Register Skill'],
    kpiDataList: [
      { labelText: 'Installed', valueText: '18' },
      { labelText: 'In Use', valueText: '13' },
      { labelText: 'Restricted', valueText: '2' },
      { labelText: 'Policy Blocks', valueText: '1' },
    ],
    panelTitleText: 'Top Skills',
    rowDataList: [
      ['Skill', 'Agents', 'Health'],
      ['Web Browsing', '42', 'Healthy'],
      ['Code Interpreter', '28', 'Healthy'],
      ['PII Redaction', '35', 'Degraded'],
    ],
    noteTitleText: 'Rollout Notes',
    noteList: ['Highlight safety-sensitive skills.', 'Track enablement by team scope.', 'Surface recent failures in toolbar.'],
  },
  prompts: {
    categoryText: 'Management',
    titleText: 'Universal Prompts',
    actionLabelList: ['New Template'],
    kpiDataList: [
      { labelText: 'Templates', valueText: '12' },
      { labelText: 'Latest Version', valueText: 'v3.0.1' },
      { labelText: 'Pending Reviews', valueText: '4' },
      { labelText: 'Hotfixes', valueText: '1' },
    ],
    panelTitleText: 'Prompt Inventory',
    rowDataList: [
      ['Template', 'Version', 'Updated'],
      ['Standard System Persona', 'v2.1.0', '2h ago'],
      ['Data Extraction Template', 'v1.4.2', '1d ago'],
      ['Creative Writing Guide', 'v3.0.1', '3d ago'],
    ],
    noteTitleText: 'Prompt Governance',
    noteList: ['All edits require version bumps.', 'Keep rollback pointers in metadata.', 'Show diff preview before publish.'],
  },
  mcp: {
    categoryText: 'Advanced',
    titleText: 'MCP Servers',
    actionLabelList: ['Connect Server'],
    kpiDataList: [
      { labelText: 'Connected', valueText: '7' },
      { labelText: 'Warnings', valueText: '1' },
      { labelText: 'Disconnected', valueText: '2' },
      { labelText: 'Avg Latency', valueText: '63ms' },
    ],
    panelTitleText: 'Server Health',
    rowDataList: [
      ['Server', 'Transport', 'State'],
      ['filesystem-toolkit', 'stdio', 'Healthy'],
      ['tracking-api-bridge', 'http_sse', 'Degraded'],
      ['crm-connect', 'http_sse', 'Healthy'],
    ],
    noteTitleText: 'MCP Rules',
    noteList: ['Show credential scope inline.', 'Separate degraded from disconnected.', 'Keep health check action near row.'],
  },
  evaluation: {
    categoryText: 'Quality Assurance',
    titleText: 'Agent Evaluation',
    actionLabelList: ['Run Benchmark'],
    kpiDataList: [
      { labelText: 'Avg Score', valueText: '92.4' },
      { labelText: 'Pass Rate', valueText: '88.5%' },
      { labelText: 'Runs 30d', valueText: '1,402' },
      { labelText: 'Cost / Run', valueText: '$0.42' },
    ],
    panelTitleText: 'Recent Runs',
    rowDataList: [
      ['Agent', 'Dataset', 'Score'],
      ['Customer Support Alpha', 'Helpdesk Bench v2', '98.5'],
      ['Data Analyst Pro', 'SQL Reasoning v1', '82.1'],
      ['Security Auditor', 'Vulnerability Scan', '88.7'],
    ],
    noteTitleText: 'Evaluation Signals',
    noteList: ['Mark pass/fail with explicit badges.', 'Link failed item details from score row.', 'Export run data for offline review.'],
  },
};

function renderPrototype() {
  const activeViewConfig = viewConfigByKey[prototypeState.activeViewKey];

  viewCategoryElement.textContent = activeViewConfig.categoryText;
  viewTitleElement.textContent = activeViewConfig.titleText;

  viewActionsElement.innerHTML = activeViewConfig.actionLabelList
    .map((actionLabelText) => `<span class="view-chip">${actionLabelText}</span>`)
    .join('');

  kpiGridElement.innerHTML = activeViewConfig.kpiDataList
    .map(
      (kpiData) => `
        <article class="kpi-card">
          <p>${kpiData.labelText}</p>
          <strong>${kpiData.valueText}</strong>
        </article>
      `,
    )
    .join('');

  const tableRowHtmlText = activeViewConfig.rowDataList
    .map((rowValueList, rowIndex) => {
      if (rowIndex === 0) {
        return `<tr><th>${rowValueList[0]}</th><th>${rowValueList[1]}</th><th>${rowValueList[2]}</th></tr>`;
      }
      return `<tr><td>${rowValueList[0]}</td><td>${rowValueList[1]}</td><td>${rowValueList[2]}</td></tr>`;
    })
    .join('');

  primaryPanelElement.innerHTML = `<h3>${activeViewConfig.panelTitleText}</h3><table>${tableRowHtmlText}</table>`;

  const prototypeRelationshipHtml = prototypeState.activeViewKey === 'evaluation'
    ? `
      <div class="relationship-block callout">
        <p>
          <strong>Prototype relation:</strong>
          <span>\`Prototype Page 04\` is the parent IA prototype. \`Prototype Page 02\` is the Evaluation module detail prototype.</span>
        </p>
        <p style="margin-top: 6px;">
          <strong>Entry path:</strong>
          <span>Sidebar <strong>Evaluation</strong> -> <strong>Datasets</strong> (\`/evaluation/datasets\`).</span>
        </p>
        <div class="inline-row" style="margin-top: 8px;">
          <a class="btn btn-secondary btn-sm" href="./prototype-page-02.html">
            Open Dataset Management Prototype
          </a>
        </div>
      </div>
    `
    : '';

  secondaryPanelElement.innerHTML = `
    <h3>${activeViewConfig.noteTitleText}</h3>
    ${prototypeRelationshipHtml}
    <ul>
      ${activeViewConfig.noteList.map((noteText) => `<li>${noteText}</li>`).join('')}
    </ul>
  `;

  statusBannerElement.classList.toggle('hidden', !prototypeState.alertVisible);
  statusBannerElement.textContent = prototypeState.alertVisible
    ? 'Alert: Evaluation run_0ed31a5a8d72 is below threshold and requires review.'
    : '';

  prototypeShellElement.dataset.density = prototypeState.densityKey;

  navButtonElementList.forEach((navButtonElement) => {
    navButtonElement.classList.toggle('is-active', navButtonElement.dataset.view === prototypeState.activeViewKey);
  });
}

navButtonElementList.forEach((navButtonElement) => {
  navButtonElement.addEventListener('click', () => {
    prototypeState = {
      ...prototypeState,
      activeViewKey: navButtonElement.dataset.view || 'overview',
    };
    renderPrototype();
  });
});

toolButtonElementList.forEach((toolButtonElement) => {
  toolButtonElement.addEventListener('click', () => {
    const actionKey = toolButtonElement.dataset.action;

    if (actionKey === 'toggle-density') {
      prototypeState = {
        ...prototypeState,
        densityKey: prototypeState.densityKey === 'comfortable' ? 'dense' : 'comfortable',
      };
    }

    if (actionKey === 'toggle-alert') {
      prototypeState = {
        ...prototypeState,
        alertVisible: !prototypeState.alertVisible,
      };
    }

    if (actionKey === 'reset') {
      prototypeState = { ...initialPrototypeState };
    }

    renderPrototype();
  });
});

renderPrototype();
