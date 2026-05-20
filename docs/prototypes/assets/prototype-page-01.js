const sidebarLinkElementList = Array.from(document.querySelectorAll('.sidebar-link'));
const prototypeRootElement = document.getElementById('prototype-root');
const prototypeBannerElement = document.getElementById('prototype-banner');
const topbarSectionElement = document.getElementById('topbar-section');
const topbarTitleElement = document.getElementById('topbar-title');
const topbarDateElement = document.getElementById('topbar-date');

function formatShortDateText() {
  return new Date().toLocaleDateString(undefined, {
    month: 'short',
    day: '2-digit',
    year: 'numeric',
  });
}

topbarDateElement.textContent = formatShortDateText();

const initialPrototypeState = {
  routeKey: 'overview',
  emptyModeEnabled: false,
  errorModeEnabled: false,
  denseModeEnabled: false,
  showKnowledgeCreateForm: false,
  showKnowledgeDetail: false,
  showKnowledgeAdvancedOptions: false,
  showAgentsCreateForm: false,
  showAgentDetail: false,
  selectedAgentType: 'chat',
  showMcpEditMode: false,
};

let prototypeState = { ...initialPrototypeState };

const knowledgeSourceRecordList = [
  {
    id: 'ks-001',
    name: 'Product Documentation',
    description: 'Public product docs and onboarding notes',
    storage_type: 'minio',
    status: 'active',
    default_chunking_strategy: 'fixed_size',
    created_at: '2026-03-01',
  },
  {
    id: 'ks-002',
    name: 'Sales Playbook',
    description: 'Pricing policy and objection handling',
    storage_type: 'minio',
    status: 'active',
    default_chunking_strategy: 'sentence',
    created_at: '2026-02-25',
  },
];

const agentRecordList = [
  {
    id: 'agent-001',
    name: 'Freight Assistant',
    description: 'Primary chat agent for logistics support',
    agent_type: 'chat',
    model_name: 'gpt-4.1-mini',
    status: 'active',
    created_at: '2026-02-20',
  },
  {
    id: 'agent-002',
    name: 'Invoice ETL',
    description: 'CSV cleanup and normalization pipeline',
    agent_type: 'etl',
    model_name: null,
    status: 'inactive',
    created_at: '2026-02-18',
  },
];

const mcpServerRecordList = [
  {
    id: 'mcp-001',
    name: 'filesystem-toolkit',
    description: 'Reads approved docs from mounted directory',
    transport_type: 'stdio',
    status: 'active',
    health_status: 'healthy',
  },
  {
    id: 'mcp-002',
    name: 'tracking-api-bridge',
    description: 'Queries shipment status from upstream API',
    transport_type: 'http_sse',
    status: 'active',
    health_status: 'degraded',
  },
];

const evaluationRunRecordList = [
  {
    id: 'run_6f29cbf4c39e',
    status: 'completed',
    items_done: 120,
    items_total: 120,
    score_em: 0.781,
    score_f1: 0.842,
    score_llm_judge: 0.884,
    gating_status: 'pass',
    created_at: '2026-03-02 16:02',
  },
  {
    id: 'run_0ed31a5a8d72',
    status: 'running',
    items_done: 44,
    items_total: 120,
    score_em: null,
    score_f1: null,
    score_llm_judge: null,
    gating_status: 'not_evaluated',
    created_at: '2026-03-03 09:50',
  },
];

function resolveRouteMetaFromRouteKey(routeKey) {
  if (routeKey === 'overview') {
    return { sectionLabel: 'Dashboard', pageTitle: 'Overview', pathText: '/' };
  }

  if (routeKey === 'knowledge-sources' || routeKey === 'knowledge-source-detail') {
    return {
      sectionLabel: 'Knowledge',
      pageTitle: 'Knowledge Sources',
      pathText: routeKey === 'knowledge-sources' ? '/knowledge-sources' : '/knowledge-sources/ks-001',
    };
  }

  if (routeKey === 'agents' || routeKey === 'agent-detail') {
    return {
      sectionLabel: 'Management',
      pageTitle: 'Agents',
      pathText: routeKey === 'agents' ? '/agents' : '/agents/agent-001',
    };
  }

  if (routeKey === 'mcp-servers') {
    return { sectionLabel: 'Advanced', pageTitle: 'MCP Servers', pathText: '/mcp-servers' };
  }

  return { sectionLabel: 'Admin', pageTitle: 'Workspace', pathText: '/' };
}

function resolveSidebarRouteFromRouteKey(routeKey) {
  if (routeKey === 'knowledge-source-detail') {
    return 'knowledge-sources';
  }

  if (routeKey === 'agent-detail') {
    return 'agents';
  }

  return routeKey;
}

function renderGlobalBanner() {
  if (prototypeState.errorModeEnabled) {
    prototypeBannerElement.innerHTML =
      '<div class="alert alert-error">API Error: Failed to fetch latest data snapshot (simulated 500).</div>';
    return;
  }

  const routeMeta = resolveRouteMetaFromRouteKey(prototypeState.routeKey);
  prototypeBannerElement.innerHTML =
    `<div class="alert alert-success">Current route: <span class="mono">${routeMeta.pathText}</span></div>`;
}

function renderOverviewPageHtml() {
  const shouldRenderEmpty = prototypeState.emptyModeEnabled;

  if (shouldRenderEmpty) {
    return `
      <div class="page-header">
        <div>
          <h1>Overview</h1>
          <p class="page-subtitle">Unified operational snapshot for agents, knowledge sources, and MCP servers.</p>
        </div>
        <button class="btn btn-secondary">Refresh</button>
      </div>

      <div class="card empty-state">
        <p>No dashboard data yet.</p>
      </div>
    `;
  }

  return `
    <div class="page-header">
      <div>
        <h1>Overview</h1>
        <p class="page-subtitle">Unified operational snapshot for agents, knowledge sources, and MCP servers.</p>
      </div>
      <button class="btn btn-secondary">Refresh</button>
    </div>

    <div class="kpi-grid">
      <article class="kpi-card"><p>Total Agents</p><strong>2</strong></article>
      <article class="kpi-card"><p>Active Sources</p><strong>2</strong></article>
      <article class="kpi-card"><p>Active MCP Servers</p><strong>2</strong></article>
      <article class="kpi-card"><p>Active Agents</p><strong>1</strong></article>
    </div>

    <div class="dashboard-grid" style="margin-top: 20px;">
      <section class="card">
        <div class="panel-header">
          <h3>Recent Agents</h3>
          <button class="btn-link" data-action="open-agents">View All</button>
        </div>
        <table class="table">
          <thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Created</th></tr></thead>
          <tbody>
            ${agentRecordList
              .map(
                (agentRecord) => `
              <tr>
                <td><button class="btn-link" data-action="open-agent-detail">${agentRecord.name}</button></td>
                <td><span class="badge badge-secondary">${agentRecord.agent_type}</span></td>
                <td><span class="badge ${agentRecord.status === 'active' ? 'badge-success' : 'badge-warning'}">${agentRecord.status}</span></td>
                <td>${agentRecord.created_at}</td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      </section>

      <section class="card">
        <div class="panel-header">
          <h3>Knowledge Sources</h3>
          <button class="btn-link" data-action="open-knowledge-sources">Open</button>
        </div>
        <table class="table">
          <thead><tr><th>Name</th><th>Chunking</th><th>Status</th></tr></thead>
          <tbody>
            ${knowledgeSourceRecordList
              .map(
                (sourceRecord) => `
              <tr>
                <td>${sourceRecord.name}</td>
                <td>${sourceRecord.default_chunking_strategy}</td>
                <td><span class="badge ${sourceRecord.status === 'active' ? 'badge-success' : 'badge-warning'}">${sourceRecord.status}</span></td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      </section>

      <section class="card">
        <div class="panel-header">
          <h3>MCP Servers</h3>
          <button class="btn-link" data-action="open-mcp-servers">Open</button>
        </div>
        <table class="table">
          <thead><tr><th>Name</th><th>Transport</th><th>Status</th><th>Health</th></tr></thead>
          <tbody>
            ${mcpServerRecordList
              .map(
                (serverRecord) => `
              <tr>
                <td>${serverRecord.name}</td>
                <td>${serverRecord.transport_type}</td>
                <td><span class="badge ${serverRecord.status === 'active' ? 'badge-success' : 'badge-warning'}">${serverRecord.status}</span></td>
                <td><span class="badge badge-secondary">${serverRecord.health_status || 'unknown'}</span></td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      </section>
    </div>
  `;
}

function renderKnowledgeSourcesPageHtml() {
  if (prototypeState.showKnowledgeDetail) {
    return renderKnowledgeSourceDetailPageHtml();
  }

  const shouldRenderEmpty = prototypeState.emptyModeEnabled;

  return `
    <div class="page-header">
      <div>
        <h1>Knowledge Sources</h1>
        <p class="page-subtitle">Configure source containers, default chunking strategy, and ingestion scope.</p>
      </div>
      <button class="btn btn-primary" data-action="toggle-knowledge-create">+ New Source</button>
    </div>

    ${prototypeState.showKnowledgeCreateForm ? `
      <div class="card" style="margin-bottom: 24px;">
        <h3 style="margin-bottom: 16px;">Create Knowledge Source</h3>
        <div class="form-group">
          <label>Name *</label>
          <input type="text" value="Customer FAQ" readonly />
        </div>
        <div class="form-group">
          <label>Description</label>
          <textarea rows="3" readonly>Optional description of this knowledge source</textarea>
        </div>
        <div class="form-group">
          <label>Chunking Strategy</label>
          <select><option selected>fixed_size</option><option>sentence</option><option>semantic</option></select>
        </div>
        <div class="inline-row">
          <button class="btn btn-primary">Create</button>
          <button class="btn btn-secondary" data-action="toggle-knowledge-create">Cancel</button>
        </div>
      </div>
    ` : ''}

    ${shouldRenderEmpty ? `
      <div class="card empty-state">
        <p>No knowledge sources yet.</p>
        <p style="margin-top: 8px; color: #999;">Create a knowledge source to start uploading documents.</p>
      </div>
    ` : `
      <div class="card">
        <table class="table">
          <thead>
            <tr>
              <th>Name</th><th>Type</th><th>Status</th><th>Chunking</th><th>Created</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            ${knowledgeSourceRecordList
              .map(
                (sourceRecord) => `
              <tr>
                <td>
                  <strong>${sourceRecord.name}</strong>
                  <div style="font-size: 12px; color: #666; margin-top: 4px;">${sourceRecord.description || ''}</div>
                </td>
                <td><span class="badge badge-secondary">${sourceRecord.storage_type}</span></td>
                <td><span class="badge ${sourceRecord.status === 'active' ? 'badge-success' : 'badge-warning'}">${sourceRecord.status}</span></td>
                <td><span class="badge badge-secondary">${sourceRecord.default_chunking_strategy}</span></td>
                <td>${sourceRecord.created_at}</td>
                <td><button class="btn btn-secondary btn-sm" data-action="open-knowledge-detail">Open</button></td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      </div>
    `}
  `;
}

function renderKnowledgeSourceDetailPageHtml() {
  return `
    <div class="breadcrumb">
      <button class="btn-link" data-action="back-to-knowledge-list">Knowledge Sources</button>
      <span>/</span>
      <span>Product Documentation</span>
    </div>

    <div class="page-header">
      <div>
        <h1>Product Documentation</h1>
        <p class="page-subtitle">Public product docs and onboarding notes</p>
        <div style="margin-top: 8px; display: flex; gap: 12px; align-items: center;">
          <span style="font-size: 13px; color: #666;">Default Chunking:</span>
          <span class="badge badge-secondary">fixed size</span>
        </div>
      </div>
      <div class="inline-row">
        <button class="btn btn-secondary">Check Model</button>
        <button class="btn btn-primary">Run Ingestion</button>
        <button class="btn btn-secondary btn-sm" data-action="toggle-knowledge-advanced">
          ${prototypeState.showKnowledgeAdvancedOptions ? 'Hide Advanced Options ▲' : 'Show Advanced Options ▼'}
        </button>
      </div>
    </div>

    ${prototypeState.showKnowledgeAdvancedOptions ? `
      <div class="card" style="margin-bottom: 24px; border-left: 4px solid #6c757d;">
        <h3 style="margin-bottom: 12px;">Ingestion Options</h3>
        <p style="font-size: 13px; color: #666; margin-bottom: 12px;">
          Override the default chunking strategy for this ingestion job.
        </p>
        <div class="form-group">
          <label>Chunking Strategy</label>
          <select><option selected>fixed_size</option><option>sentence</option><option>semantic</option></select>
        </div>
      </div>
    ` : ''}

    <div class="card" style="margin-bottom: 24px;">
      <h3 style="margin-bottom: 12px;">Latest Ingestion Job</h3>
      <div class="inline-row">
        <span class="badge badge-info">running</span>
        <span style="color: #666; font-size: 14px;">2/3 documents, 148/230 chunks</span>
        <span class="badge badge-secondary">fixed_size</span>
      </div>
    </div>

    <div class="card">
      <h3>Upload Documents</h3>
      <div class="soft-panel">Drop files here or click to upload. Supports PDF, DOCX, TXT, MD.</div>
    </div>

    <div class="card">
      <h3>Documents</h3>
      <table class="table">
        <thead><tr><th>File</th><th>Status</th><th>Size</th><th>Actions</th></tr></thead>
        <tbody>
          <tr><td>intro.md</td><td><span class="badge badge-success">indexed</span></td><td>12.4 KB</td><td><button class="btn btn-danger btn-sm">Delete</button></td></tr>
          <tr><td>pricing-guide.docx</td><td><span class="badge badge-info">processing</span></td><td>420 KB</td><td><button class="btn btn-danger btn-sm">Delete</button></td></tr>
        </tbody>
      </table>
    </div>
  `;
}

function renderAgentsPageHtml() {
  if (prototypeState.showAgentDetail) {
    return renderAgentDetailPageHtml();
  }

  const shouldRenderEmpty = prototypeState.emptyModeEnabled;

  return `
    <div class="page-header">
      <div>
        <h1>Agents</h1>
        <p class="page-subtitle">Manage assistant profiles, default model routing, and runtime behavior presets.</p>
      </div>
      <button class="btn btn-primary" data-action="toggle-agent-create">+ New Agent</button>
    </div>

    ${prototypeState.showAgentsCreateForm ? `
      <div class="card" style="margin-bottom: 24px;">
        <h3 style="margin-bottom: 16px;">Create Agent</h3>
        <div class="form-group"><label>Name *</label><input type="text" value="Customer Support Bot" readonly /></div>
        <div class="form-group"><label>Agent Type</label>
          <select data-action="toggle-agent-type">
            <option ${prototypeState.selectedAgentType === 'chat' ? 'selected' : ''}>chat</option>
            <option ${prototypeState.selectedAgentType === 'etl' ? 'selected' : ''}>etl</option>
            <option ${prototypeState.selectedAgentType === 'document_extraction' ? 'selected' : ''}>document_extraction</option>
          </select>
        </div>
        <div class="form-group"><label>System Prompt</label><textarea rows="4" readonly>You are a helpful assistant...</textarea></div>
        <div class="inline-row">
          <button class="btn btn-primary">Create Agent</button>
          <button class="btn btn-secondary" data-action="toggle-agent-create">Cancel</button>
        </div>
      </div>
    ` : ''}

    ${shouldRenderEmpty ? `
      <div class="card empty-state">
        <p>No agents yet.</p>
        <p style="margin-top: 8px; color: #999;">Create your first agent to get started.</p>
      </div>
    ` : `
      <div class="card">
        <table class="table">
          <thead><tr><th>Name</th><th>Type</th><th>Model</th><th>Status</th><th>Created</th><th>Actions</th></tr></thead>
          <tbody>
            ${agentRecordList
              .map(
                (agentRecord) => `
              <tr>
                <td><strong>${agentRecord.name}</strong><div style="font-size: 12px; color: #666; margin-top: 4px;">${agentRecord.description || ''}</div></td>
                <td><span class="badge ${agentRecord.agent_type === 'etl' ? 'badge-warning' : agentRecord.agent_type === 'document_extraction' ? 'badge-success' : 'badge-info'}">${agentRecord.agent_type}</span></td>
                <td>${agentRecord.model_name ? `<span class="badge badge-info">${agentRecord.model_name}</span>` : '<span class="badge badge-secondary">Default</span>'}</td>
                <td><span class="badge ${agentRecord.status === 'active' ? 'badge-success' : 'badge-warning'}">${agentRecord.status}</span></td>
                <td>${agentRecord.created_at}</td>
                <td><button class="btn btn-secondary btn-sm" data-action="open-agent-detail">Manage</button></td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      </div>
    `}
  `;
}

function renderAgentDetailPageHtml() {
  const isChatAgent = prototypeState.selectedAgentType === 'chat';

  return `
    <div class="breadcrumb">
      <button class="btn-link" data-action="back-to-agents-list">Agents</button>
      <span>/</span>
      <span>Freight Assistant</span>
    </div>

    <div class="page-header">
      <div>
        <h1>Freight Assistant</h1>
        <p class="page-subtitle">Primary chat agent for logistics support</p>
      </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
      <h3>Agent Details</h3>
      <div class="kv-grid">
        <div><strong>Model:</strong></div><div>gpt-4.1-mini</div>
        <div><strong>Status:</strong></div><div><span class="badge badge-success">active</span></div>
        <div><strong>Type:</strong></div><div>${prototypeState.selectedAgentType}</div>
        <div><strong>Created:</strong></div><div>2026-02-20</div>
      </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
      <h3>Knowledge Mounts</h3>
      <table class="table">
        <thead><tr><th>Source</th><th>Priority</th><th>Status</th></tr></thead>
        <tbody>
          <tr><td>Product Documentation</td><td>10</td><td><span class="badge badge-success">active</span></td></tr>
          <tr><td>Sales Playbook</td><td>7</td><td><span class="badge badge-success">active</span></td></tr>
        </tbody>
      </table>
    </div>

    <div class="card" style="margin-bottom: 24px;">
      <div class="panel-header">
        <h3>Evaluation</h3>
        <span class="badge badge-secondary">current-state</span>
      </div>

      ${isChatAgent ? `
        <div class="inline-row" style="margin-bottom: 12px;">
          <button class="btn btn-primary btn-sm">+ New Dataset</button>
          <button class="btn btn-secondary btn-sm">Freeze Dataset</button>
          <button class="btn btn-primary btn-sm">Start Evaluation Run</button>
        </div>

        <table class="table" style="margin-bottom: 12px;">
          <thead><tr><th>Run ID</th><th>Status</th><th>Progress</th><th>EM</th><th>F1</th><th>Judge</th><th>Gate</th><th>Created</th></tr></thead>
          <tbody>
            ${evaluationRunRecordList
              .map(
                (runRecord) => `
              <tr>
                <td class="mono">${runRecord.id.slice(0, 12)}</td>
                <td><span class="badge ${runRecord.status === 'completed' ? 'badge-success' : 'badge-info'}">${runRecord.status}</span></td>
                <td>${runRecord.items_done}/${runRecord.items_total}</td>
                <td>${runRecord.score_em === null ? '-' : runRecord.score_em.toFixed(3)}</td>
                <td>${runRecord.score_f1 === null ? '-' : runRecord.score_f1.toFixed(3)}</td>
                <td>${runRecord.score_llm_judge === null ? '-' : runRecord.score_llm_judge.toFixed(3)}</td>
                <td><span class="badge badge-secondary">${runRecord.gating_status}</span></td>
                <td>${runRecord.created_at}</td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      ` : `<div class="alert alert-error">Evaluation currently supports chat agents only.</div>`}
    </div>

    <div class="card">
      <h3>MCP Mounts</h3>
      <table class="table">
        <thead><tr><th>Server</th><th>Credential</th><th>Priority</th><th>Status</th></tr></thead>
        <tbody>
          <tr><td>filesystem-toolkit</td><td>system-default</td><td>5</td><td><span class="badge badge-success">active</span></td></tr>
          <tr><td>tracking-api-bridge</td><td>ops-team</td><td>3</td><td><span class="badge badge-success">active</span></td></tr>
        </tbody>
      </table>
    </div>
  `;
}

function renderMcpServersPageHtml() {
  const shouldRenderEmpty = prototypeState.emptyModeEnabled;

  return `
    <div class="page-header">
      <div>
        <h1>MCP Servers</h1>
        <p class="page-subtitle">Register external tool servers, configure transport, and validate health status.</p>
      </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
      <h3 style="margin-bottom: 16px;">${prototypeState.showMcpEditMode ? 'Edit MCP Server' : 'Create MCP Server'}</h3>
      <div class="form-group"><label>Name *</label><input type="text" value="${prototypeState.showMcpEditMode ? 'tracking-api-bridge' : ''}" readonly /></div>
      <div class="form-group"><label>Transport</label><select><option ${prototypeState.showMcpEditMode ? '' : 'selected'}>stdio</option><option ${prototypeState.showMcpEditMode ? 'selected' : ''}>http_sse</option></select></div>
      <div class="form-group"><label>Status</label><select><option selected>active</option><option>inactive</option></select></div>
      <div class="form-group"><label>Command *</label><input type="text" value="${prototypeState.showMcpEditMode ? 'bridge-server' : 'uvx'}" readonly /></div>
      <div class="form-group"><label>Args</label><input type="text" value="${prototypeState.showMcpEditMode ? '--transport sse --url https://example/mcp' : 'mcp-server-filesystem /tmp'}" readonly /></div>
      <div class="inline-row">
        <button class="btn btn-primary">${prototypeState.showMcpEditMode ? 'Update Server' : 'Create Server'}</button>
        <button class="btn btn-secondary" data-action="toggle-mcp-edit">${prototypeState.showMcpEditMode ? 'Cancel' : 'Load Edit Sample'}</button>
      </div>
    </div>

    <div class="card">
      <h3 style="margin-bottom: 16px;">Registered MCP Servers</h3>
      ${shouldRenderEmpty ? `
        <div class="empty-state"><p>No MCP servers registered yet.</p></div>
      ` : `
        <table class="table">
          <thead><tr><th>Name</th><th>Transport</th><th>Status</th><th>Health</th><th>Actions</th></tr></thead>
          <tbody>
            ${mcpServerRecordList
              .map(
                (serverRecord) => `
              <tr>
                <td><strong>${serverRecord.name}</strong><div style="color: #666; font-size: 12px; margin-top: 4px;">${serverRecord.description || ''}</div></td>
                <td>${serverRecord.transport_type}</td>
                <td><span class="badge ${serverRecord.status === 'active' ? 'badge-success' : 'badge-warning'}">${serverRecord.status}</span></td>
                <td>${serverRecord.health_status || 'unknown'}</td>
                <td>
                  <div class="inline-row">
                    <button class="btn btn-secondary btn-sm" data-action="toggle-mcp-edit">Edit</button>
                    <button class="btn btn-primary btn-sm">Check</button>
                    <button class="btn btn-danger btn-sm">Delete</button>
                  </div>
                </td>
              </tr>`,
              )
              .join('')}
          </tbody>
        </table>
      `}
    </div>
  `;
}

function renderRoutePageHtml() {
  if (prototypeState.routeKey === 'overview') {
    return renderOverviewPageHtml();
  }

  if (prototypeState.routeKey === 'knowledge-sources') {
    return renderKnowledgeSourcesPageHtml();
  }

  if (prototypeState.routeKey === 'agents') {
    return renderAgentsPageHtml();
  }

  if (prototypeState.routeKey === 'mcp-servers') {
    return renderMcpServersPageHtml();
  }

  return '<div class="loading">Unknown route</div>';
}

function renderPrototype() {
  const routeMeta = resolveRouteMetaFromRouteKey(
    prototypeState.routeKey === 'knowledge-source-detail'
      ? 'knowledge-source-detail'
      : prototypeState.routeKey === 'agent-detail'
        ? 'agent-detail'
        : prototypeState.routeKey,
  );

  topbarSectionElement.textContent = routeMeta.sectionLabel;
  topbarTitleElement.textContent = routeMeta.pageTitle;

  document.body.classList.toggle('body-dense', prototypeState.denseModeEnabled);

  const activeSidebarRouteKey = resolveSidebarRouteFromRouteKey(prototypeState.routeKey);
  sidebarLinkElementList.forEach((sidebarLinkElement) => {
    sidebarLinkElement.classList.toggle('is-active', sidebarLinkElement.dataset.route === activeSidebarRouteKey);
  });

  renderGlobalBanner();

  if (prototypeState.routeKey === 'knowledge-source-detail') {
    prototypeRootElement.innerHTML = renderKnowledgeSourceDetailPageHtml();
    return;
  }

  if (prototypeState.routeKey === 'agent-detail') {
    prototypeRootElement.innerHTML = renderAgentDetailPageHtml();
    return;
  }

  prototypeRootElement.innerHTML = renderRoutePageHtml();
}

function resetPrototypeState() {
  prototypeState = { ...initialPrototypeState };
  renderPrototype();
}

document.addEventListener('click', (event) => {
  const targetElement = event.target;
  if (!(targetElement instanceof Element)) {
    return;
  }

  const routeElement = targetElement.closest('[data-route]');
  if (routeElement) {
    const nextRouteKey = routeElement.dataset.route;
    if (!nextRouteKey) {
      return;
    }

    prototypeState = {
      ...prototypeState,
      routeKey: nextRouteKey,
      showKnowledgeDetail: false,
      showAgentDetail: false,
    };
    renderPrototype();
    return;
  }

  const actionElement = targetElement.closest('[data-action]');
  if (!actionElement) {
    return;
  }

  const actionKey = actionElement.dataset.action;

  if (actionKey === 'toggle-empty') {
    prototypeState = { ...prototypeState, emptyModeEnabled: !prototypeState.emptyModeEnabled };
    renderPrototype();
    return;
  }

  if (actionKey === 'toggle-error') {
    prototypeState = { ...prototypeState, errorModeEnabled: !prototypeState.errorModeEnabled };
    renderPrototype();
    return;
  }

  if (actionKey === 'toggle-density') {
    prototypeState = { ...prototypeState, denseModeEnabled: !prototypeState.denseModeEnabled };
    renderPrototype();
    return;
  }

  if (actionKey === 'reset-state') {
    resetPrototypeState();
    return;
  }

  if (actionKey === 'toggle-knowledge-create') {
    prototypeState = { ...prototypeState, showKnowledgeCreateForm: !prototypeState.showKnowledgeCreateForm };
    renderPrototype();
    return;
  }

  if (actionKey === 'open-knowledge-detail') {
    prototypeState = { ...prototypeState, routeKey: 'knowledge-source-detail', showKnowledgeDetail: true };
    renderPrototype();
    return;
  }

  if (actionKey === 'back-to-knowledge-list') {
    prototypeState = { ...prototypeState, routeKey: 'knowledge-sources', showKnowledgeDetail: false };
    renderPrototype();
    return;
  }

  if (actionKey === 'toggle-knowledge-advanced') {
    prototypeState = {
      ...prototypeState,
      showKnowledgeAdvancedOptions: !prototypeState.showKnowledgeAdvancedOptions,
    };
    renderPrototype();
    return;
  }

  if (actionKey === 'toggle-agent-create') {
    prototypeState = { ...prototypeState, showAgentsCreateForm: !prototypeState.showAgentsCreateForm };
    renderPrototype();
    return;
  }

  if (actionKey === 'open-agent-detail') {
    prototypeState = { ...prototypeState, routeKey: 'agent-detail', showAgentDetail: true };
    renderPrototype();
    return;
  }

  if (actionKey === 'back-to-agents-list') {
    prototypeState = { ...prototypeState, routeKey: 'agents', showAgentDetail: false };
    renderPrototype();
    return;
  }

  if (actionKey === 'toggle-agent-type') {
    const currentType = prototypeState.selectedAgentType;
    const nextType = currentType === 'chat' ? 'etl' : currentType === 'etl' ? 'document_extraction' : 'chat';
    prototypeState = { ...prototypeState, selectedAgentType: nextType };
    renderPrototype();
    return;
  }

  if (actionKey === 'toggle-mcp-edit') {
    prototypeState = { ...prototypeState, showMcpEditMode: !prototypeState.showMcpEditMode };
    renderPrototype();
    return;
  }

  if (actionKey === 'open-agents') {
    prototypeState = { ...prototypeState, routeKey: 'agents' };
    renderPrototype();
    return;
  }

  if (actionKey === 'open-knowledge-sources') {
    prototypeState = { ...prototypeState, routeKey: 'knowledge-sources' };
    renderPrototype();
    return;
  }

  if (actionKey === 'open-mcp-servers') {
    prototypeState = { ...prototypeState, routeKey: 'mcp-servers' };
    renderPrototype();
  }
});

renderPrototype();
