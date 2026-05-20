const scenarioStepsMap = {
  normal: [
    {
      phaseTitle: "需求确认",
      phaseDescription: "完成目标确认和边界梳理，输出可执行范围。",
      riskLabel: "Low",
    },
    {
      phaseTitle: "技术拆解",
      phaseDescription: "拆分任务并确认依赖，形成实施路径。",
      riskLabel: "Low",
    },
    {
      phaseTitle: "开发与联调",
      phaseDescription: "完成主干功能并执行联调验证。",
      riskLabel: "Medium",
    },
    {
      phaseTitle: "验收交付",
      phaseDescription: "执行验收清单并完成交付确认。",
      riskLabel: "Low",
    },
  ],
  edge: [
    {
      phaseTitle: "需求确认",
      phaseDescription: "关键决策人缺席，范围暂定为最小可行版本。",
      riskLabel: "Medium",
    },
    {
      phaseTitle: "技术拆解",
      phaseDescription: "出现外部接口未文档化，需补齐对接策略。",
      riskLabel: "High",
    },
    {
      phaseTitle: "开发与联调",
      phaseDescription: "上游服务返回格式变化，触发兼容处理。",
      riskLabel: "High",
    },
    {
      phaseTitle: "验收交付",
      phaseDescription: "以降级策略交付并记录后续收敛计划。",
      riskLabel: "Medium",
    },
  ],
};

const prototypeUiState = {
  selectedScenarioKey: "normal",
  currentStepNumber: 0,
};

const phaseTitleElement = document.getElementById("phaseTitle");
const phaseDescriptionElement = document.getElementById("phaseDescription");
const stepNumberElement = document.getElementById("stepNumber");
const riskLabelElement = document.getElementById("riskLabel");
const progressFillElement = document.getElementById("progressFill");
const timelineListElement = document.getElementById("timelineList");
const startButtonElement = document.getElementById("startBtn");
const nextButtonElement = document.getElementById("nextBtn");
const resetButtonElement = document.getElementById("resetBtn");
const scenarioButtonElements = Array.from(document.querySelectorAll(".toggle-btn[data-scenario]"));

function getSelectedScenarioSteps() {
  return scenarioStepsMap[prototypeUiState.selectedScenarioKey] || scenarioStepsMap.normal;
}

function getCurrentStepRecord() {
  if (prototypeUiState.currentStepNumber === 0) {
    return null;
  }

  const selectedScenarioSteps = getSelectedScenarioSteps();
  const selectedStepRecord = selectedScenarioSteps[prototypeUiState.currentStepNumber - 1];
  return selectedStepRecord || null;
}

function renderScenarioButtons() {
  scenarioButtonElements.forEach((scenarioButtonElement) => {
    const scenarioKey = scenarioButtonElement.getAttribute("data-scenario");
    const isActiveScenario = scenarioKey === prototypeUiState.selectedScenarioKey;
    scenarioButtonElement.classList.toggle("is-active", isActiveScenario);
    scenarioButtonElement.setAttribute("aria-pressed", isActiveScenario ? "true" : "false");
  });
}

function renderMainPanel() {
  const selectedScenarioSteps = getSelectedScenarioSteps();
  const totalStepCount = selectedScenarioSteps.length;
  const currentStepRecord = getCurrentStepRecord();

  if (!currentStepRecord) {
    phaseTitleElement.textContent = "待开始";
    phaseDescriptionElement.textContent = "点击 Start 开始模拟需求到交付的流程。";
    riskLabelElement.textContent = "Low";
  } else {
    phaseTitleElement.textContent = currentStepRecord.phaseTitle;
    phaseDescriptionElement.textContent = currentStepRecord.phaseDescription;
    riskLabelElement.textContent = currentStepRecord.riskLabel;
  }

  stepNumberElement.textContent = `${prototypeUiState.currentStepNumber} / ${totalStepCount}`;
  const progressPercent = Math.round((prototypeUiState.currentStepNumber / totalStepCount) * 100);
  progressFillElement.style.width = `${progressPercent}%`;

  startButtonElement.disabled = prototypeUiState.currentStepNumber > 0;
  nextButtonElement.disabled =
    prototypeUiState.currentStepNumber === 0 ||
    prototypeUiState.currentStepNumber >= totalStepCount;
  resetButtonElement.disabled = prototypeUiState.currentStepNumber === 0;
}

function buildTimelineItemElement(stepRecord, itemStatusLabel, itemClassName) {
  const timelineItemElement = document.createElement("li");
  timelineItemElement.className = `timeline-item ${itemClassName}`;

  const timelineTitleElement = document.createElement("p");
  timelineTitleElement.className = "timeline-item-title";
  timelineTitleElement.textContent = stepRecord.phaseTitle;

  const timelineDescriptionElement = document.createElement("p");
  timelineDescriptionElement.className = "timeline-item-note";
  timelineDescriptionElement.textContent = stepRecord.phaseDescription;

  const timelineStatusElement = document.createElement("p");
  timelineStatusElement.className = "timeline-item-status";
  timelineStatusElement.textContent = itemStatusLabel;

  timelineItemElement.appendChild(timelineTitleElement);
  timelineItemElement.appendChild(timelineDescriptionElement);
  timelineItemElement.appendChild(timelineStatusElement);

  return timelineItemElement;
}

function renderTimelinePanel() {
  const selectedScenarioSteps = getSelectedScenarioSteps();
  timelineListElement.innerHTML = "";

  selectedScenarioSteps.forEach((stepRecord, stepIndex) => {
    const stepNumber = stepIndex + 1;
    const isCompletedStep = stepNumber < prototypeUiState.currentStepNumber;
    const isCurrentStep = stepNumber === prototypeUiState.currentStepNumber;

    let itemStatusLabel = "Pending";
    let itemClassName = "is-upcoming";

    if (isCompletedStep) {
      itemStatusLabel = "Completed";
      itemClassName = "is-complete";
    } else if (isCurrentStep) {
      itemStatusLabel = "In Progress";
      itemClassName = "is-current";
    }

    const timelineItemElement = buildTimelineItemElement(stepRecord, itemStatusLabel, itemClassName);
    timelineListElement.appendChild(timelineItemElement);
  });
}

function renderPrototypeView() {
  renderScenarioButtons();
  renderMainPanel();
  renderTimelinePanel();
}

function onStartButtonClick() {
  if (prototypeUiState.currentStepNumber === 0) {
    prototypeUiState.currentStepNumber = 1;
    renderPrototypeView();
  }
}

function onNextButtonClick() {
  const selectedScenarioSteps = getSelectedScenarioSteps();
  const totalStepCount = selectedScenarioSteps.length;

  if (
    prototypeUiState.currentStepNumber > 0 &&
    prototypeUiState.currentStepNumber < totalStepCount
  ) {
    prototypeUiState.currentStepNumber += 1;
    renderPrototypeView();
  }
}

function onResetButtonClick() {
  prototypeUiState.currentStepNumber = 0;
  renderPrototypeView();
}

function onScenarioButtonClick(event) {
  const selectedScenarioKey = event.currentTarget.getAttribute("data-scenario");
  if (!selectedScenarioKey || selectedScenarioKey === prototypeUiState.selectedScenarioKey) {
    return;
  }

  prototypeUiState.selectedScenarioKey = selectedScenarioKey;
  prototypeUiState.currentStepNumber = 0;
  renderPrototypeView();
}

function bindPrototypeEvents() {
  startButtonElement.addEventListener("click", onStartButtonClick);
  nextButtonElement.addEventListener("click", onNextButtonClick);
  resetButtonElement.addEventListener("click", onResetButtonClick);

  scenarioButtonElements.forEach((scenarioButtonElement) => {
    scenarioButtonElement.addEventListener("click", onScenarioButtonClick);
  });
}

function bootstrapPrototype() {
  bindPrototypeEvents();
  renderPrototypeView();
}

if (
  phaseTitleElement &&
  phaseDescriptionElement &&
  stepNumberElement &&
  riskLabelElement &&
  progressFillElement &&
  timelineListElement &&
  startButtonElement &&
  nextButtonElement &&
  resetButtonElement
) {
  bootstrapPrototype();
}
