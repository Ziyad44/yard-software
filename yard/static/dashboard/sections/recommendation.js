function toNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function chip(label, value) {
  return `<span class="chip">${label}: ${value}</span>`;
}

function normalizeCandidates(recommendation) {
  if (Array.isArray(recommendation.top_candidates) && recommendation.top_candidates.length > 0) {
    return recommendation.top_candidates.slice(0, 3);
  }
  const entries = Object.entries(recommendation.candidate_scores || {});
  return entries
    .sort((left, right) => Number(left[1]) - Number(right[1]))
    .slice(0, 3)
    .map(([actionName, score]) => ({ action_name: actionName, score: Number(score) }));
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
    headline.textContent = recommendation.text || "No active recommendation.";
  }

  const rationale = document.getElementById("recommendationRationale");
  if (rationale) {
    rationale.textContent = recommendation.rationale || "Waiting for next trigger.";
  }

  const meta = document.getElementById("recommendationMeta");
  if (meta) {
    const chips = [
      chip("Trigger", latestTriggerType),
      chip("Reason", latestTriggerReason),
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
            <td>${candidate.action_name}</td>
            <td>${toNumber(candidate.score, 0).toFixed(3)}</td>
          </tr>
        `,
          )
          .join("")
      : `<tr><td colspan="2" class="muted">No candidate scores.</td></tr>`;
  }
}
