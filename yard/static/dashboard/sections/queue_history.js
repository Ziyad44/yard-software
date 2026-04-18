function toFixed(value, digits) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return "-";
  }
  return parsed.toFixed(digits);
}

export function renderQueueHistory(payload) {
  const queueRows = Array.isArray(payload.queue_table) ? payload.queue_table : [];
  const gateRows = Array.isArray(payload.gate_history) ? payload.gate_history : [];

  const queueBody = document.getElementById("queueTableBody");
  if (queueBody) {
    queueBody.innerHTML = queueRows
      .map(
        (row) => `
          <tr>
            <td>${row.truck_id}</td>
            <td>${row.truck_type}</td>
            <td>${toFixed(row.load_units, 1)}</td>
            <td>${row.gate_arrival}</td>
          </tr>
        `,
      )
      .join("");
  }

  const queueNote = document.getElementById("queueEmptyNote");
  if (queueNote) {
    queueNote.textContent = queueRows.length
      ? `${queueRows.length} truck(s) currently waiting in queue.`
      : "No trucks currently waiting in queue.";
  }

  const gateBody = document.getElementById("gateHistoryTableBody");
  if (gateBody) {
    gateBody.innerHTML = gateRows
      .map(
        (row) => `
          <tr>
            <td>${row.truck_id}</td>
            <td>${row.truck_type}</td>
            <td>${toFixed(row.load_units, 1)}</td>
            <td>${row.gate_arrival}</td>
            <td>${row.departure_minute == null ? "-" : row.departure_minute}</td>
            <td>${row.waiting_time_minutes == null ? "-" : row.waiting_time_minutes}</td>
          </tr>
        `,
      )
      .join("");
  }

  const gateNote = document.getElementById("gateHistoryEmptyNote");
  if (gateNote) {
    gateNote.textContent = gateRows.length
      ? `${gateRows.length} truck(s) have completed staging.`
      : "No completed trucks yet.";
  }
}
