const MERMAID_WRAPPER_CLASS = 'mermaid-copy-wrapper';
const MERMAID_BUTTON_CLASS = 'mermaid-copy-button';

function getMermaidSourceText(mermaidElement) {
  if (mermaidElement.dataset.mermaidSource) {
    return mermaidElement.dataset.mermaidSource;
  }

  const rawMermaidSourceText = mermaidElement.textContent?.trim() ?? '';
  mermaidElement.dataset.mermaidSource = rawMermaidSourceText;
  return rawMermaidSourceText;
}

async function copyTextToClipboard(rawMermaidSourceText) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(rawMermaidSourceText);
    return;
  }

  const temporaryTextareaElement = document.createElement('textarea');
  temporaryTextareaElement.value = rawMermaidSourceText;
  temporaryTextareaElement.setAttribute('readonly', 'true');
  temporaryTextareaElement.style.position = 'absolute';
  temporaryTextareaElement.style.left = '-9999px';
  document.body.appendChild(temporaryTextareaElement);
  temporaryTextareaElement.select();
  document.execCommand('copy');
  temporaryTextareaElement.remove();
}

function attachCopyButton(mermaidElement) {
  if (mermaidElement.parentElement?.classList.contains(MERMAID_WRAPPER_CLASS)) {
    return;
  }

  const mermaidWrapperElement = document.createElement('div');
  mermaidWrapperElement.className = MERMAID_WRAPPER_CLASS;

  const mermaidCopyButtonElement = document.createElement('button');
  mermaidCopyButtonElement.className = MERMAID_BUTTON_CLASS;
  mermaidCopyButtonElement.type = 'button';
  mermaidCopyButtonElement.textContent = 'copy';
  mermaidCopyButtonElement.setAttribute('aria-label', 'Copy Mermaid source');

  mermaidCopyButtonElement.addEventListener('click', async () => {
    const rawMermaidSourceText = getMermaidSourceText(mermaidElement);

    if (!rawMermaidSourceText) {
      return;
    }

    try {
      await copyTextToClipboard(rawMermaidSourceText);
      mermaidCopyButtonElement.dataset.copyState = 'copied';
      mermaidCopyButtonElement.textContent = 'copied';
      window.setTimeout(() => {
        mermaidCopyButtonElement.dataset.copyState = '';
        mermaidCopyButtonElement.textContent = 'copy';
      }, 1200);
    } catch (copyError) {
      mermaidCopyButtonElement.textContent = 'failed';
      window.setTimeout(() => {
        mermaidCopyButtonElement.textContent = 'copy';
      }, 1200);
      console.error('Failed to copy Mermaid source.', copyError);
    }
  });

  mermaidElement.parentElement?.insertBefore(mermaidWrapperElement, mermaidElement);
  mermaidWrapperElement.appendChild(mermaidCopyButtonElement);
  mermaidWrapperElement.appendChild(mermaidElement);
}

async function initializeMermaidDiagrams(rootDocument = document) {
  if (typeof mermaid === 'undefined') {
    return;
  }

  const mermaidElements = Array.from(rootDocument.querySelectorAll('.mermaid'));
  if (mermaidElements.length === 0) {
    return;
  }

  for (const mermaidElement of mermaidElements) {
    getMermaidSourceText(mermaidElement);
    attachCopyButton(mermaidElement);
  }

  await mermaid.run({ nodes: mermaidElements });
}

function bootMermaidCopyButtons() {
  if (typeof mermaid === 'undefined') {
    return;
  }

  mermaid.initialize({ startOnLoad: false, theme: 'default' });
  initializeMermaidDiagrams().catch((initializationError) => {
    console.error('Failed to initialize Mermaid diagrams.', initializationError);
  });

  if (typeof document$ !== 'undefined') {
    document$.subscribe(() => {
      initializeMermaidDiagrams().catch((initializationError) => {
        console.error('Failed to initialize Mermaid diagrams after navigation.', initializationError);
      });
    });
  }
}

window.addEventListener('DOMContentLoaded', bootMermaidCopyButtons);
