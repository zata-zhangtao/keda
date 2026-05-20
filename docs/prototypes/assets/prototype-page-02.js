const prototypeShellElement = document.getElementById('prototype-shell');
const bannerElement = document.getElementById('banner');
const viewRootElement = document.getElementById('view-root');

const navButtonElementList = Array.from(document.querySelectorAll('.nav-button'));
const toolButtonElementList = Array.from(document.querySelectorAll('[data-action]'));

const initialState = {
  activeViewKey: 'evaluation-datasets',
  densityKey: 'comfortable',
  selectedDatasetId: 'ds_helpdesk_v3',
  selectedAgentId: 'agent_freight_assistant',
  isFrozen: false,
  datasetItemsCount: 120,
  runStarted: false,
  showLegacyNote: true,
};

let prototypeState = { ...initialState };

const datasetRowList = [
  {
    id: 'ds_helpdesk_v3',
    name: 'Helpdesk QA Gold',
    versionText: 'v3',
    itemCount: 120,
    isFrozen: false,
    updatedAt: '2026-03-04 10:21',
  },
  {
    id: 'ds_customs_v2',
    name: 'Customs Classification',
    versionText: 'v2',
    itemCount: 84,
    isFrozen: true,
    updatedAt: '2026-03-03 18:12',
  },
  {
    id: 'ds_bol_v4',
    name: 'B/L Clause Reasoning',
    versionText: 'v4',
    itemCount: 210,
    isFrozen: true,
    updatedAt: '2026-03-02 09:40',
  },
];

const agentOptionList = [
  { id: 'agent_freight_assistant', name: 'Freight Assistant (chat)' },
  { id: 'agent_support_alpha', name: 'Customer Support Alpha (chat)' },
  { id: 'agent_trade_ops', name: 'Trade Ops Copilot (chat)' },
];

function resolveDatasetIsFrozenById(datasetId) {
  const datasetRow = datasetRowList.find((row) => row.id === datasetId);
  if (!datasetRow) {
    return false;
  }
  if (datasetId === prototypeState.selectedDatasetId) {
    return prototypeState.isFrozen;
  }
  return datasetRow.isFrozen;
}

function resolveDatasetNameById(datasetId) {
  const datasetRow = datasetRowList.find((row) => row.id === datasetId);
  return datasetRow ? datasetRow.name : datasetId;
}

function renderOverviewViewHtml() {
  return `
    <div class="page">
      <section class="card">
        <div class="page-header">
          <div>
            <h3>Overview</h3>
            <p>Prototype baseline after separating dataset management from agent details.</p>
          </div>
        </div>
      </section>

      <section class="card two-col">
        <div class="stack">
          <span class="badge">Before</span>
          <p class="muted">AgentDetailPage mixed dataset create/import/freeze and run visualization in one block.</p>
        </div>
        <div class="stack">
          <span class="badge badge-good">After</span>
          <p class="muted">Evaluation datasets move to dedicated routes; AgentDetailPage focuses on run list/results.</p>
        </div>
      </section>
    </div>
  `;
}

function renderAgentsViewHtml() {
  return `
    <div class="page">
      <section class="card">
        <div class="page-header">
          <div>
            <h3>Agent Detail (Transitional)</h3>
            <p>Run history/results stay here in phase 1. Dataset management points to new Evaluation pages.</p>
          </div>
          <div class="inline-row">
            <button class="button button-secondary" data-action="toggle-legacy-note">
              ${prototypeState.showLegacyNote ? 'Hide' : 'Show'} Deprecation Note
            </button>
          </div>
        </div>

        ${prototypeState.showLegacyNote ? `
          <div class="callout" style="margin-top: 10px;">
            Dataset creation/import/freeze moved to <strong>Evaluation &gt; Datasets</strong>. Keep this page for run status and results.
          </div>
        ` : ''}
      </section>

      <section class="card table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>Run ID</th>
              <th>Dataset</th>
              <th>Status</th>
              <th>EM</th>
              <th>F1</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td class="mono">run_3bc9f0ad71a2</td>
              <td>${resolveDatasetNameById(prototypeState.selectedDatasetId)}</td>
              <td><span class="badge badge-good">completed</span></td>
              <td>0.812</td>
              <td>0.867</td>
              <td><button class="button button-secondary">View Results</button></td>
            </tr>
            <tr>
              <td class="mono">run_15edaa409dbe</td>
              <td>Customs Classification</td>
              <td><span class="badge badge-warn">running</span></td>
              <td>-</td>
              <td>-</td>
              <td><button class="button button-secondary">View Results</button></td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  `;
}

function renderDatasetListCardHtml() {
  const datasetTableRowsHtml = datasetRowList
    .map((datasetRow) => {
      const rowIsSelected = datasetRow.id === prototypeState.selectedDatasetId;
      const rowIsFrozen = rowIsSelected ? prototypeState.isFrozen : datasetRow.isFrozen;
      const rowItemsCount = rowIsSelected ? prototypeState.datasetItemsCount : datasetRow.itemCount;

      return `
        <tr>
          <td>
            <strong>${datasetRow.name}</strong>
            <div class="muted mono" style="margin-top: 2px;">${datasetRow.id}</div>
          </td>
          <td>${datasetRow.versionText}</td>
          <td>${rowItemsCount}</td>
          <td>
            ${rowIsFrozen ? '<span class="badge badge-good">frozen</span>' : '<span class="badge badge-warn">draft</span>'}
          </td>
          <td>${datasetRow.updatedAt}</td>
          <td>
            <button class="button button-secondary" data-action="open-dataset" data-dataset-id="${datasetRow.id}">
              ${rowIsSelected ? 'Opened' : 'Open'}
            </button>
          </td>
        </tr>
      `;
    })
    .join('');

  return `
    <section class="card">
      <div class="page-header">
        <div>
          <h3>Evaluation Datasets</h3>
          <p>Top-level dataset management. Decoupled from any single agent detail page.</p>
        </div>
        <div class="inline-row">
          <button class="button button-primary" data-action="create-dataset">+ New Dataset</button>
        </div>
      </div>

      <div class="table-wrap" style="margin-top: 10px;">
        <table class="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Version</th>
              <th>Items</th>
              <th>State</th>
              <th>Updated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${datasetTableRowsHtml}
          </tbody>
        </table>
      </div>
    </section>
  `;
}

function renderDatasetDetailCardHtml() {
  const selectedDatasetName = resolveDatasetNameById(prototypeState.selectedDatasetId);
  const canStartRun = prototypeState.isFrozen;

  return `
    <section class="card stack">
      <div class="page-header">
        <div>
          <h3>Dataset Detail</h3>
          <p class="mono">/evaluation/datasets/${prototypeState.selectedDatasetId}</p>
        </div>
        <div class="inline-row">
          <span class="badge ${prototypeState.isFrozen ? 'badge-good' : 'badge-warn'}">
            ${prototypeState.isFrozen ? 'frozen snapshot' : 'draft editable'}
          </span>
        </div>
      </div>

      <div class="two-col">
        <div class="field">
          <label>Dataset Name</label>
          <input type="text" value="${selectedDatasetName}" readonly />
        </div>
        <div class="field">
          <label>Total Items</label>
          <input type="text" value="${prototypeState.datasetItemsCount}" readonly />
        </div>
      </div>

      <div class="inline-row">
        <button class="button button-secondary" data-action="import-csv" ${prototypeState.isFrozen ? 'disabled' : ''}>Import CSV (+10 rows)</button>
        <button class="button button-primary" data-action="freeze-dataset" ${prototypeState.isFrozen ? 'disabled' : ''}>Freeze Dataset</button>
      </div>

      ${prototypeState.isFrozen ? '' : '<p class="muted">Run creation requires frozen dataset snapshot.</p>'}

      <div class="card" style="padding: 12px;">
        <div class="stack">
          <h4>Run Launcher</h4>
          <div class="field">
            <label>Select Agent</label>
            <select id="agent-select">
              ${agentOptionList
                .map(
                  (agentOption) => `
                    <option value="${agentOption.id}" ${agentOption.id === prototypeState.selectedAgentId ? 'selected' : ''}>
                      ${agentOption.name}
                    </option>
                  `,
                )
                .join('')}
            </select>
          </div>
          <div class="inline-row">
            <button class="button button-primary" data-action="start-run" ${canStartRun ? '' : 'disabled'}>Start Evaluation Run</button>
            <span class="muted">Result view remains in Agent Detail in phase 1.</span>
          </div>
        </div>
      </div>

      ${prototypeState.runStarted ? `
        <div class="callout">
          Run created for <strong>${selectedDatasetName}</strong>. Transition target: <strong>/agents/:agentId</strong> for run progress and results.
        </div>
      ` : ''}
    </section>
  `;
}

function renderEvaluationDatasetsViewHtml() {
  return `
    <div class="page">
      <section class="card">
        <div class="inline-row">
          <span class="badge badge-info">linked from studio</span>
          <span class="muted">
            Parent prototype: <strong>Prototype Page 04</strong> -> Sidebar <strong>Evaluation</strong> -> <strong>Datasets</strong>.
          </span>
          <a class="button button-secondary" href="./prototype-page-04.html">Back To Prototype Page 04</a>
        </div>
        <p class="relationship-path muted" style="margin-top: 8px;">
          Target entry route in product: <span class="mono">/evaluation/datasets</span> -> <span class="mono">/evaluation/datasets/:datasetId</span>
        </p>
      </section>
      ${renderDatasetListCardHtml()}
      ${renderDatasetDetailCardHtml()}
    </div>
  `;
}

function render() {
  if (prototypeState.activeViewKey === 'overview') {
    viewRootElement.innerHTML = renderOverviewViewHtml();
  }

  if (prototypeState.activeViewKey === 'agents') {
    viewRootElement.innerHTML = renderAgentsViewHtml();
  }

  if (prototypeState.activeViewKey === 'evaluation-datasets') {
    viewRootElement.innerHTML = renderEvaluationDatasetsViewHtml();
  }

  bannerElement.classList.toggle('hidden', !prototypeState.runStarted);
  bannerElement.textContent = prototypeState.runStarted
    ? 'Status: evaluation run queued. Use Agent Detail page for live run status and result tables.'
    : '';

  prototypeShellElement.dataset.density = prototypeState.densityKey;

  navButtonElementList.forEach((navButtonElement) => {
    navButtonElement.classList.toggle('is-active', navButtonElement.dataset.view === prototypeState.activeViewKey);
  });
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
    prototypeState = { ...initialState };
    render();
    return;
  }

  if (actionKey === 'open-dataset') {
    const datasetId = triggerElement.dataset.datasetId || initialState.selectedDatasetId;
    const datasetRow = datasetRowList.find((row) => row.id === datasetId);
    prototypeState = {
      ...prototypeState,
      activeViewKey: 'evaluation-datasets',
      selectedDatasetId: datasetId,
      isFrozen: datasetRow ? datasetRow.isFrozen : false,
      datasetItemsCount: datasetRow ? datasetRow.itemCount : initialState.datasetItemsCount,
      runStarted: false,
    };
    render();
    return;
  }

  if (actionKey === 'create-dataset') {
    prototypeState = {
      ...prototypeState,
      selectedDatasetId: 'ds_new_sample_v1',
      isFrozen: false,
      datasetItemsCount: 0,
      runStarted: false,
    };
    render();
    return;
  }

  if (actionKey === 'import-csv') {
    if (prototypeState.isFrozen) {
      return;
    }
    prototypeState = {
      ...prototypeState,
      datasetItemsCount: prototypeState.datasetItemsCount + 10,
      runStarted: false,
    };
    render();
    return;
  }

  if (actionKey === 'freeze-dataset') {
    if (prototypeState.isFrozen) {
      return;
    }
    prototypeState = {
      ...prototypeState,
      isFrozen: true,
      runStarted: false,
    };
    render();
    return;
  }

  if (actionKey === 'start-run') {
    if (!prototypeState.isFrozen) {
      return;
    }
    const agentSelectElement = document.getElementById('agent-select');
    const selectedAgentId = agentSelectElement instanceof HTMLSelectElement
      ? agentSelectElement.value
      : prototypeState.selectedAgentId;

    prototypeState = {
      ...prototypeState,
      selectedAgentId,
      runStarted: true,
    };
    render();
    return;
  }

  if (actionKey === 'toggle-legacy-note') {
    prototypeState = {
      ...prototypeState,
      showLegacyNote: !prototypeState.showLegacyNote,
    };
    render();
  }
}

navButtonElementList.forEach((navButtonElement) => {
  navButtonElement.addEventListener('click', () => {
    prototypeState = {
      ...prototypeState,
      activeViewKey: navButtonElement.dataset.view || 'evaluation-datasets',
      runStarted: false,
    };
    render();
  });
});

toolButtonElementList.forEach((toolButtonElement) => {
  toolButtonElement.addEventListener('click', () => {
    onAction(toolButtonElement.dataset.action, toolButtonElement);
  });
});

document.addEventListener('click', (event) => {
  const targetElement = event.target;
  if (!(targetElement instanceof Element)) {
    return;
  }

  const actionElement = targetElement.closest('[data-action]');
  if (!actionElement) {
    return;
  }

  const actionKey = actionElement.dataset.action;
  if (!actionKey) {
    return;
  }

  onAction(actionKey, actionElement);
});

render();
