function toNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function chip(label, value) {
  return `<span class="chip">${label}: ${value}</span>`;
}

function toActionLabel(actionName, targetDockId, holdGate) {
  switch (actionName) {
    case "keep_current_plan":
      return "Keep current operating plan";
    case "shift_one_worker_to_most_loaded_dock":
      return targetDockId ? `Add 1 worker to Dock ${targetDockId}` : "Add 1 worker to the highest-pressure dock";
    case "shift_one_forklift_to_most_loaded_dock":
      return targetDockId ? `Add 1 forklift to Dock ${targetDockId}` : "Add 1 forklift to the highest-pressure dock";
    case "prioritize_clearing_at_riskiest_dock":
      if (targetDockId && holdGate) {
        return `Prioritize clearing at Dock ${targetDockId} and hold gate release`;
      }
      return targetDockId
        ? `Prioritize clearing at Dock ${targetDockId}`
        : "Prioritize clearing at the riskiest dock";
    default:
      return actionName || "No active recommendation";
  }
}

function buildHeadline(recommendation) {
  if (!recommendation.selected_action_name) {
    return "No active recommendation.";
  }
  const targetDockId = toNumber(recommendation.selected_target_dock_id, 0) || null;
  const actionText = toActionLabel(
    recommendation.selected_action_name,
    targetDockId,
    Boolean(recommendation.hold_gate_release),
  );
  return `Recommended Action: ${actionText}`;
}

function buildShortExplanation(recommendation, triggerReason) {
  const parts = [];
  if (triggerReason && triggerReason !== "none") {
    parts.push(triggerReason);
  }
  if (recommendation.selection_note) {
    parts.push(recommendation.selection_note);
  } else if (recommendation.rationale && recommendation.rationale !== "Waiting for next trigger.") {
    parts.push(recommendation.rationale);
  }
  const waitDelta = recommendation.kpi_delta?.predicted_avg_wait_minutes;
  if (waitDelta) {
    parts.push(
      `Predicted wait: ${toNumber(waitDelta.before, 0).toFixed(1)} -> ${toNumber(waitDelta.after, 0).toFixed(1)} min.`,
    );
  }
  if (!parts.length) {
    return "Waiting for next trigger.";
  }
  return parts.join(" ");
}

function normalizeCandidates(recommendation) {
  const selectedActionName = recommendation.selected_action_name || "";
  if (Array.isArray(recommendation.top_candidates) && recommendation.top_candidates.length > 0) {
    return recommendation.top_candidates.slice(0, 3).map((candidate, index) => ({
      rank: toNumber(candidate.rank, index + 1),
      action_name: candidate.action_name,
      score: Number(candidate.score),
      is_selected: Boolean(candidate.is_selected) || candidate.action_name === selectedActionName,
    }));
  }
  const entries = Object.entries(recommendation.candidate_scores || {});
  return entries
    .sort((left, right) => Number(left[1]) - Number(right[1]))
    .slice(0, 3)
    .map(([actionName, score], index) => ({
      rank: index + 1,
      action_name: actionName,
      score: Number(score),
      is_selected: actionName === selectedActionName,
    }));
}

function formatMetricValue(metricName, value) {
  const numeric = toNumber(value, 0);
  if (metricName === "predicted_dock_utilization" || metricName === "predicted_staging_overflow_risk") {
    return `${(numeric * 100).toFixed(1)}%`;
  }
  if (metricName === "throughput_trucks_per_hour" || metricName === "effective_flow_rate_per_hour") {
    return numeric.toFixed(2);
  }
  return numeric.toFixed(2);
}

function formatDeltaClass(metricName, deltaValue) {
  const delta = toNumber(deltaValue, 0);
  if (Math.abs(delta) < 1e-6) {
    return "delta-flat";
  }
  const lowerIsBetter =
    metricName !== "throughput_trucks_per_hour" && metricName !== "effective_flow_rate_per_hour";
  if (lowerIsBetter) {
    return delta < 0 ? "delta-down" : "delta-up";
  }
  return delta > 0 ? "delta-down" : "delta-up";
}

function metricLabel(metricName) {
  const labels = {
    predicted_avg_wait_minutes: "Pred Wait (min)",
    predicted_avg_time_in_system_minutes: "Pred TIS (min)",
    predicted_queue_length: "Pred Queue",
    predicted_dock_utilization: "Pred Utilization",
    predicted_staging_overflow_risk: "Pred Staging Risk",
    throughput_trucks_per_hour: "Throughput (trucks/hr)",
    effective_flow_rate_per_hour: "Effective Flow (trucks/hr)",
  };
  return labels[metricName] || metricName;
}

export function renderRecommendationPanel(payload) {
  const recommendation = payload.recommendation || {};
  const selectedAction = recommendation.selected_action_name || "none";
  const scoreText = recommendation.score == null ? "-" : toNumber(recommendation.score, 0).toFixed(3);
  const robustScoreText =
    recommendation.robust_score == null ? "-" : toNumber(recommendation.robust_score, 0).toFixed(3);
  const holdGate = recommendation.hold_gate_release ? "ON" : "OFF";
  const latestTriggerType = recommendation.latest_trigger_type || "none";
  const latestTriggerReason = recommendation.latest_trigger_reason || "none";
  const baseline = recommendation.selected_baseline_metrics || {};

  const headline = document.getElementById("recommendationHeadline");
  if (headline) {
    headline.textContent = buildHeadline(recommendation);
  }

  const rationale = document.getElementById("recommendationRationale");
  if (rationale) {
    rationale.textContent = buildShortExplanation(recommendation, latestTriggerReason);
  }

  const dockReason = document.getElementById("recommendationDockReason");
  if (dockReason) {
    dockReason.textContent = `Why this dock: ${
      recommendation.selected_dock_reason || "No dock-specific reason available yet."
    }`;
  }

  const resourceReason = document.getElementById("recommendationResourceReason");
  if (resourceReason) {
    resourceReason.textContent = `Resource move: ${
      recommendation.resource_source_reason || "No resource move selected."
    }`;
  }

  const meta = document.getElementById("recommendationMeta");
  if (meta) {
    const chips = [
      chip("Trigger", latestTriggerType),
      chip("Action", selectedAction),
      chip("Score", scoreText),
      chip("Robust", robustScoreText),
      chip("Hold Gate", holdGate),
      chip("Decision", recommendation.decision_status || "none"),
      chip("Pred Wait", `${toNumber(baseline.predicted_avg_wait_minutes, 0).toFixed(2)}m`),
      chip("Pred TIS", `${toNumber(baseline.predicted_avg_time_in_system_minutes, 0).toFixed(2)}m`),
    ];
    meta.innerHTML = chips.join("");
  }

  const deltaBody = document.getElementById("recommendationDeltaBody");
  const kpiDelta = recommendation.kpi_delta || {};
  const metricOrder = [
    "predicted_avg_wait_minutes",
    "predicted_avg_time_in_system_minutes",
    "predicted_queue_length",
    "predicted_dock_utilization",
    "predicted_staging_overflow_risk",
    "throughput_trucks_per_hour",
  ];
  if (deltaBody) {
    deltaBody.innerHTML = metricOrder
      .filter((metricName) => kpiDelta[metricName])
      .map((metricName) => {
        const row = kpiDelta[metricName];
        const deltaClass = formatDeltaClass(metricName, row.delta);
        const deltaSign = toNumber(row.delta, 0) > 0 ? "+" : "";
        return `
          <tr>
            <td>${metricLabel(metricName)}</td>
            <td>${formatMetricValue(metricName, row.before)}</td>
            <td>${formatMetricValue(metricName, row.after)}</td>
            <td class="${deltaClass}">${deltaSign}${formatMetricValue(metricName, row.delta)}</td>
          </tr>
        `;
      })
      .join("");
    if (!deltaBody.innerHTML) {
      deltaBody.innerHTML = `<tr><td colspan="4" class="muted">No KPI delta available.</td></tr>`;
    }
  }

  const assignmentBody = document.getElementById("recommendationAssignmentBody");
  const assignments = Array.isArray(recommendation.assignment_by_dock)
    ? recommendation.assignment_by_dock
    : [];
  if (assignmentBody) {
    assignmentBody.innerHTML = assignments.length
      ? assignments
          .map(
            (row) => `
          <tr>
            <td>${row.dock_id}</td>
            <td>${toNumber(row.workers, 0)}</td>
            <td>${toNumber(row.forklifts, 0)}</td>
          </tr>
        `,
          )
          .join("")
      : `<tr><td colspan="3" class="muted">No assignment recommendation.</td></tr>`;
  }

  const candidateBody = document.getElementById("recommendationCandidateBody");
  const candidates = normalizeCandidates(recommendation);
  if (candidateBody) {
    candidateBody.innerHTML = candidates.length
      ? candidates
          .map(
            (candidate) => `
          <tr>
            <td>#${toNumber(candidate.rank, 0)}</td>
            <td>${candidate.action_name}</td>
            <td>${toNumber(candidate.score, 0).toFixed(3)}</td>
            <td>${
              [candidate.rank === 1 ? "Top score" : "", candidate.is_selected ? "Selected" : ""]
                .filter(Boolean)
                .join(" / ") || "-"
            }</td>
          </tr>
        `,
          )
          .join("")
      : `<tr><td colspan="4" class="muted">No candidate scores.</td></tr>`;
  }
}
