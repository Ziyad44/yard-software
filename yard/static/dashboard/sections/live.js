function metricCard(label, value) {
  return `
    <article class="metric-card">
      <div class="metric-label">${label}</div>
      <div class="metric-value">${value}</div>
    </article>
  `;
}

function numberValue(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function renderLiveOperations(payload) {
  const live = payload.live_operations || {};
  const currentMinute = numberValue(live.current_minute, 0);
  const arrivalsLastStep = numberValue(live.arrivals_last_step, 0);
  const arrivalsTotal = numberValue(live.arrivals_total, 0);

  const metrics = [
    ["Simulation Timer", live.simulation_timer || "00:00"],
    ["Current Minute", currentMinute],
    ["Queue Length", numberValue(live.queue_length, 0)],
    ["Active Trucks", numberValue(live.active_trucks_count, 0)],
    ["Completed Trucks", numberValue(live.completed_trucks_count, 0)],
    ["Arrivals", `${arrivalsLastStep} step / ${arrivalsTotal} total`],
  ];

  const metricGrid = document.getElementById("liveMetricsGrid");
  if (metricGrid) {
    metricGrid.innerHTML = metrics.map(([label, value]) => metricCard(label, value)).join("");
  }

  const clock = document.getElementById("minuteClock");
  if (clock) {
    clock.textContent = `${live.simulation_timer || "00:00"} | M${currentMinute}`;
  }

  const supervisorInputs = payload.supervisor_inputs || {};
  const fieldIds = [
    "available_workers",
    "available_forklifts",
    "active_docks",
    "max_unloaders_per_dock",
  ];
  for (const fieldId of fieldIds) {
    const input = document.getElementById(fieldId);
    if (input) {
      const value = supervisorInputs[fieldId];
      input.value = value == null ? "" : String(value);
    }
  }
}

export function setPollingStatus(message) {
  const target = document.getElementById("pollingStatus");
  if (target) {
    target.textContent = message;
  }
}
