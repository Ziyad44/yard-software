import { apiGet, apiPost } from "./api.js";
import { initThemeToggle } from "./theme.js";
import { renderLiveOperations, setPollingStatus } from "./sections/live.js";
import { renderDockStaging } from "./sections/dock_staging.js";
import { renderResourcePanel } from "./sections/resources.js";
import { renderRecommendationPanel } from "./sections/recommendation.js";
import { renderAnalytics } from "./sections/analytics.js";
import { renderQueueHistory } from "./sections/queue_history.js";

const POLL_INTERVAL_MS = 2000;
let pollingHandle = null;
let writeInFlight = false;

function setPage(pageName) {
  const operationsPage = document.getElementById("operationsPage");
  const queueHistoryPage = document.getElementById("queueHistoryPage");
  const navOperations = document.getElementById("navOperations");
  const navQueueHistory = document.getElementById("navQueueHistory");
  const showQueueHistory = pageName === "queue_history";

  if (operationsPage) {
    operationsPage.classList.toggle("page-hidden", showQueueHistory);
  }
  if (queueHistoryPage) {
    queueHistoryPage.classList.toggle("page-hidden", !showQueueHistory);
  }
  if (navOperations) {
    navOperations.classList.toggle("active", !showQueueHistory);
  }
  if (navQueueHistory) {
    navQueueHistory.classList.toggle("active", showQueueHistory);
  }
}

function renderDashboard(payload) {
  renderLiveOperations(payload);
  renderDockStaging(payload);
  renderResourcePanel(payload);
  renderRecommendationPanel(payload);
  renderAnalytics(payload);
  renderQueueHistory(payload);
}

async function refreshState() {
  const payload = await apiGet("/api/state");
  renderDashboard(payload);
}

function stopPolling() {
  if (pollingHandle !== null) {
    window.clearInterval(pollingHandle);
    pollingHandle = null;
  }
}

function startPolling() {
  if (pollingHandle !== null) {
    return;
  }
  setPollingStatus("Auto-refresh active (2s)");
  pollingHandle = window.setInterval(async () => {
    if (writeInFlight) {
      return;
    }
    try {
      await refreshState();
    } catch (error) {
      setPollingStatus(`Auto-refresh error: ${error.message}`);
    }
  }, POLL_INTERVAL_MS);
}

async function runWriteAction(taskLabel, action) {
  if (writeInFlight) {
    return;
  }
  writeInFlight = true;
  stopPolling();
  setPollingStatus(`${taskLabel}...`);
  try {
    const payload = await action();
    renderDashboard(payload);
    setPollingStatus("Auto-refresh active (2s)");
  } catch (error) {
    window.alert(error.message);
    setPollingStatus("Action failed. Auto-refresh resumed.");
  } finally {
    writeInFlight = false;
    startPolling();
  }
}

function wireEvents() {
  const navOperations = document.getElementById("navOperations");
  const navQueueHistory = document.getElementById("navQueueHistory");
  const refreshBtn = document.getElementById("refreshBtn");
  const stepBtn = document.getElementById("stepBtn");
  const run15Btn = document.getElementById("run15Btn");
  const supervisorForm = document.getElementById("supervisorForm");
  const applyRecBtn = document.getElementById("applyRecBtn");
  const keepPlanBtn = document.getElementById("keepPlanBtn");

  if (navOperations) {
    navOperations.addEventListener("click", () => setPage("operations"));
  }
  if (navQueueHistory) {
    navQueueHistory.addEventListener("click", () => setPage("queue_history"));
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      try {
        await refreshState();
      } catch (error) {
        window.alert(error.message);
      }
    });
  }
  if (stepBtn) {
    stepBtn.addEventListener("click", () =>
      runWriteAction("Stepping simulation", () => apiPost("/api/step", { minutes: 1 })),
    );
  }
  if (run15Btn) {
    run15Btn.addEventListener("click", () =>
      runWriteAction("Running 15-minute simulation block", () => apiPost("/api/step", { minutes: 15 })),
    );
  }
  if (supervisorForm) {
    supervisorForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const payload = {
        available_workers: Number(document.getElementById("available_workers").value),
        available_forklifts: Number(document.getElementById("available_forklifts").value),
        active_docks: Number(document.getElementById("active_docks").value),
        max_unloaders_per_dock: Number(document.getElementById("max_unloaders_per_dock").value),
      };
      runWriteAction("Updating supervisor inputs", () => apiPost("/api/supervisor", payload));
    });
  }
  if (applyRecBtn) {
    applyRecBtn.addEventListener("click", () =>
      runWriteAction("Applying recommendation", () => apiPost("/api/recommendation/apply", {})),
    );
  }
  if (keepPlanBtn) {
    keepPlanBtn.addEventListener("click", () =>
      runWriteAction("Keeping current plan", () => apiPost("/api/recommendation/keep", {})),
    );
  }
}

async function init() {
  const themeToggle = document.getElementById("themeToggle");
  if (themeToggle) {
    initThemeToggle(themeToggle);
  }
  wireEvents();
  setPage("operations");
  await refreshState();
  startPolling();
}

init().catch((error) => {
  window.alert(error.message);
  setPollingStatus(`Initialization failed: ${error.message}`);
});
