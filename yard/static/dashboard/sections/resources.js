function metricCard(label, value) {
  return `
    <article class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
    </article>
  `;
}

function toNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function renderResourcePanel(payload) {
  const resources = payload.resource_summary || {};
  const supervisorInputs = payload.supervisor_inputs || {};
  const cards = [
    ["Total Workers", toNumber(resources.workers_total, 0)],
    ["Assigned Workers", toNumber(resources.workers_assigned, 0)],
    ["Idle Workers", toNumber(resources.workers_idle, 0)],
    ["Total Forklifts", toNumber(resources.forklifts_total, 0)],
    ["Assigned Forklifts", toNumber(resources.forklifts_assigned, 0)],
    ["Idle Forklifts", toNumber(resources.forklifts_idle, 0)],
    ["Active Docks", toNumber(supervisorInputs.active_docks, 0)],
  ];

  const target = document.getElementById("resourceSummaryGrid");
  if (!target) {
    return;
  }
  target.innerHTML = cards.map(([label, value]) => metricCard(label, value)).join("");
}
