import { renderLineChart } from "../charts.js";

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

function verificationClass(status) {
  if (typeof status !== "string") {
    return "warn";
  }
  const safe = status.toLowerCase();
  if (safe === "pass" || safe === "warn" || safe === "fail" || safe === "insufficient_data") {
    return safe;
  }
  return "warn";
}

export function renderAnalytics(payload) {
  const kpis = payload.kpis || {};
  const analyticsCards = [
    ["Predicted Avg Wait", `${toNumber(kpis.predicted_avg_wait_minutes, 0).toFixed(2)} min`],
    ["Predicted Avg Time In System", `${toNumber(kpis.predicted_avg_time_in_system_minutes, 0).toFixed(2)} min`],
    ["Predicted Dock Utilization", `${toNumber(kpis.dock_utilization, 0).toFixed(1)}%`],
    ["Predicted Staging Overflow Risk", `${toNumber(kpis.staging_risk_pct, 0).toFixed(1)}%`],
    ["Throughput", `${toNumber(kpis.throughput_trucks_per_hour, 0).toFixed(2)} trucks/h`],
    ["Queue Length", `${toNumber(kpis.queue_length, 0)}`],
  ];

  const cardsTarget = document.getElementById("analyticsKpiCards");
  if (cardsTarget) {
    cardsTarget.innerHTML = analyticsCards.map(([label, value]) => metricCard(label, value)).join("");
  }

  const trends = payload.trends || {};
  renderLineChart("queueTrendChart", trends.queue_length || [], {
    color: "#31a7b5",
    xValues: trends.minutes || [],
    yLabel: "queue",
  });
  renderLineChart("stagingTrendChart", trends.max_staging_occupancy_pct || [], {
    color: "#d65959",
    maxHint: 100,
    xValues: trends.minutes || [],
    yLabel: "staging%",
  });
  renderLineChart("utilizationTrendChart", trends.dock_utilization_pct || [], {
    color: "#d8a13a",
    maxHint: 100,
    xValues: trends.minutes || [],
    yLabel: "util%",
  });

  const verification = payload.verification || {};
  const cards = Object.values(verification);
  const verifyTarget = document.getElementById("verificationCards");
  if (verifyTarget) {
    verifyTarget.innerHTML = cards.length
      ? cards
          .map((card) => {
            const status = String(card.status || "warn").toUpperCase();
            const cssClass = verificationClass(card.status);
            return `
              <article class="verify-card">
                <div class="verify-title">${card.title || "Verification"}</div>
                <div class="verify-status ${cssClass}">${status}</div>
                <div class="muted">${card.current_value || "-"}</div>
                <div class="muted">Target: ${card.target || "-"}</div>
              </article>
            `;
          })
          .join("")
      : `<article class="verify-card"><div class="muted">No verification data.</div></article>`;
  }
}
