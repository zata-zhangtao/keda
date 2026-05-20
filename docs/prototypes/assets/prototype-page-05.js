const topbarDateElement = document.getElementById('topbar-date');
const workspaceGridElement = document.getElementById('workspace-grid');
const toggleInspectorFocusButton = document.getElementById('toggle-inspector-focus');
const toggleWrapModeButton = document.getElementById('toggle-wrap-mode');
const resetLayoutButton = document.getElementById('reset-layout');
const tabRowElement = document.getElementById('tab-row');
const currentQueryParamMap = new URLSearchParams(window.location.search);
const isEmbedMode = currentQueryParamMap.get('embed') === '1' || window.self !== window.top;

if (isEmbedMode) {
  document.body.classList.add('prototype-embed-mode');
}

const layoutState = {
  inspectorFocusEnabled: isEmbedMode,
  wrapLongTextEnabled: false,
  activeTabKey: 'inbound',
};

function formatShortDateText() {
  return new Date().toLocaleDateString(undefined, {
    month: 'short',
    day: '2-digit',
    year: 'numeric',
  });
}

function applyLayoutState() {
  workspaceGridElement.classList.toggle('inspector-focus', layoutState.inspectorFocusEnabled);
  workspaceGridElement.classList.toggle('wrap-long-text', layoutState.wrapLongTextEnabled);

  toggleInspectorFocusButton.textContent = layoutState.inspectorFocusEnabled
    ? 'Balanced Columns'
    : 'Focus Inspector';
  toggleWrapModeButton.textContent = layoutState.wrapLongTextEnabled
    ? 'Clamp Messages'
    : 'Wrap Long Text';

  const tabButtonElementList = Array.from(tabRowElement.querySelectorAll('[data-tab]'));
  tabButtonElementList.forEach((tabButtonElement) => {
    const tabKey = tabButtonElement.getAttribute('data-tab');
    const panelElement = document.getElementById(`panel-${tabKey}`);
    const isActiveTab = tabKey === layoutState.activeTabKey;

    tabButtonElement.classList.toggle('btn-primary', isActiveTab);
    panelElement.classList.toggle('is-hidden', !isActiveTab);
  });
}

function resetLayoutState() {
  layoutState.inspectorFocusEnabled = isEmbedMode;
  layoutState.wrapLongTextEnabled = false;
  layoutState.activeTabKey = 'inbound';
  applyLayoutState();
}

toggleInspectorFocusButton.addEventListener('click', () => {
  layoutState.inspectorFocusEnabled = !layoutState.inspectorFocusEnabled;
  applyLayoutState();
});

toggleWrapModeButton.addEventListener('click', () => {
  layoutState.wrapLongTextEnabled = !layoutState.wrapLongTextEnabled;
  applyLayoutState();
});

resetLayoutButton.addEventListener('click', resetLayoutState);

tabRowElement.addEventListener('click', (event) => {
  const clickedTabButton = event.target.closest('[data-tab]');
  if (!clickedTabButton) {
    return;
  }
  layoutState.activeTabKey = clickedTabButton.getAttribute('data-tab') || 'inbound';
  applyLayoutState();
});

topbarDateElement.textContent = formatShortDateText();
applyLayoutState();
