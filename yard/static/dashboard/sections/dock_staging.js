function toNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function thresholdClass(occupancyPct) {
  if (occupancyPct >= 85) {
    return "threshold-high";
  }
  if (occupancyPct >= 70) {
    return "threshold-medium";
  }
  return "";
}

function phaseBadgeClass(phase) {
  const safePhase = typeof phase === "string" ? phase.toLowerCase() : "idle";
  return `badge status-${safePhase}`;
}

export function renderDockStaging(payload) {
  const rows = Array.isArray(payload.dock_status) ? payload.dock_status : [];
  const body = document.getElementById("dockStagingTableBody");
  if (!body) {
    return;
  }

  body.innerHTML = rows
    .map((row) => {
      const occupancyPct = toNumber(row.staging_occupancy_pct, 0);
      const occupancyUnits = toNumber(row.staging_occupancy_units, 0);
      const capacityUnits = toNumber(row.staging_capacity_units, 0);
      const progressClass = thresholdClass(occupancyPct);
      const width = Math.min(Math.max(occupancyPct, 0), 100);
      const phase = row.phase || "idle";
      const status = row.status || "idle";
      const truckId = row.truck_id || "-";
      const truckType = row.truck_type || "-";
      const remainingUnits = toNumber(row.remaining_load_units, 0).toFixed(1);

      return `
        <tr>
          <td>${row.dock_id}</td>
          <td><span class="badge status-${status}">${status}</span></td>
          <td><span class="${phaseBadgeClass(phase)}">${phase}</span></td>
          <td>${truckId}</td>
          <td>${truckType}</td>
          <td>${remainingUnits}</td>
          <td>${occupancyUnits.toFixed(1)} / ${capacityUnits.toFixed(1)}</td>
          <td>
            <div class="progress-wrap">
              <div class="progress-track">
                <div class="progress-fill ${progressClass}" style="width:${width.toFixed(1)}%"></div>
              </div>
              <div class="progress-label">${occupancyPct.toFixed(1)}%</div>
            </div>
          </td>
          <td>${toNumber(row.assigned_workers, 0)}</td>
          <td>${toNumber(row.assigned_forklifts, 0)}</td>
        </tr>
      `;
    })
    .join("");

  if (rows.length === 0) {
    body.innerHTML = `
      <tr>
        <td colspan="10" class="muted">No active dock rows available.</td>
      </tr>
    `;
  }
}
