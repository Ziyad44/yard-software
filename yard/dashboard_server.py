"""Local dashboard web server for the version-1 yard backend."""

from __future__ import annotations

import argparse
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .dashboard_runtime import DashboardRuntime


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Yard Control Dashboard v1</title>
  <style>
    :root {
      --bg: #f3f6f8;
      --card: #ffffff;
      --ink: #102231;
      --muted: #5f7382;
      --line: #d5dde3;
      --accent: #0c7b93;
      --accent-soft: #dff4f8;
      --good: #2c9d59;
      --warn: #d08a00;
      --bad: #c8473f;
      --shadow: 0 7px 20px rgba(16, 34, 49, 0.09);
      --radius: 14px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Trebuchet MS", "Gill Sans", "Calibri", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 15% 10%, #d6eef4 0, rgba(214,238,244,0.2) 34%, transparent 48%),
        linear-gradient(180deg, #f8fbfc 0%, var(--bg) 100%);
    }
    header {
      padding: 18px 20px 10px;
      border-bottom: 1px solid var(--line);
      background: #ffffffd8;
      backdrop-filter: blur(6px);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .header-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .title {
      margin: 0;
      font-size: 1.3rem;
      letter-spacing: 0.2px;
    }
    .clock {
      font-weight: 700;
      color: var(--accent);
      background: var(--accent-soft);
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 0.95rem;
    }
    main {
      padding: 14px;
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(12, minmax(0, 1fr));
    }
    .panel {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 12px;
    }
    .panel h2 {
      margin: 0 0 10px;
      font-size: 1rem;
      letter-spacing: 0.2px;
    }
    .span-4 { grid-column: span 4; }
    .span-5 { grid-column: span 5; }
    .span-6 { grid-column: span 6; }
    .span-7 { grid-column: span 7; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .grid-kpi {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .kpi {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px;
      background: #fcfeff;
    }
    .kpi .label {
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.4px;
    }
    .kpi .value {
      font-size: 1.15rem;
      font-weight: 700;
    }
    form .row {
      display: grid;
      grid-template-columns: 1fr 90px;
      gap: 6px;
      margin-bottom: 7px;
      align-items: center;
    }
    label { font-size: 0.9rem; color: var(--ink); }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 6px 8px;
      font-size: 0.92rem;
      font-family: inherit;
    }
    .button-row {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    button {
      border: none;
      border-radius: 9px;
      padding: 8px 11px;
      font-size: 0.88rem;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: white;
    }
    button.alt { background: #6c7f8a; }
    button.warn { background: var(--warn); color: #2b1b00; }
    button.ok { background: var(--good); }
    .rec-main {
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 8px;
      color: var(--accent);
    }
    .chips {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 6px;
      margin-bottom: 8px;
    }
    .chip {
      background: #eef3f6;
      color: #334f61;
      padding: 5px 8px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 700;
    }
    .staging-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 8px;
    }
    .staging-card {
      border-radius: 11px;
      border: 1px solid var(--line);
      padding: 9px;
      background: #fbfdff;
    }
    .dock-name {
      font-weight: 700;
      margin-bottom: 5px;
    }
    .light {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      display: inline-block;
      margin-right: 6px;
    }
    .light.green { background: var(--good); }
    .light.yellow { background: var(--warn); }
    .light.red { background: var(--bad); }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.87rem;
    }
    th, td {
      text-align: left;
      padding: 6px 7px;
      border-bottom: 1px solid var(--line);
      white-space: nowrap;
    }
    th {
      color: var(--muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.35px;
    }
    .chart-wrap {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .chart-card {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px;
      background: #fcfeff;
    }
    .chart-title {
      margin: 0 0 6px;
      font-size: 0.82rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }
    .chart-svg {
      width: 100%;
      height: 120px;
      background: linear-gradient(180deg, #fbfeff 0%, #f2f8fb 100%);
      border-radius: 8px;
      border: 1px solid #e3edf3;
    }
    .verification {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .verify {
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #fcfeff;
      padding: 10px;
    }
    .verify .name {
      color: var(--muted);
      font-size: 0.8rem;
      text-transform: uppercase;
    }
    .verify .status {
      margin-top: 5px;
      font-weight: 700;
    }
    .small-note {
      color: var(--muted);
      font-size: 0.8rem;
    }
    @media (max-width: 980px) {
      .span-4, .span-5, .span-6, .span-7, .span-8, .span-12 { grid-column: span 12; }
      .grid-kpi { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .chart-wrap { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="header-row">
      <h1 class="title">Yard Control Dashboard v1</h1>
      <div class="clock" id="minuteClock">Minute 0</div>
    </div>
  </header>
  <main>
    <section class="panel span-4">
      <h2>Supervisor Inputs</h2>
      <form id="supervisorForm">
        <div class="row"><label for="available_workers">Available Workers</label><input id="available_workers" type="number" min="0"/></div>
        <div class="row"><label for="available_forklifts">Available Forklifts</label><input id="available_forklifts" type="number" min="0"/></div>
        <div class="row"><label for="active_docks">Active Docks</label><input id="active_docks" type="number" min="1"/></div>
        <div class="row"><label for="max_unloaders_per_dock">Max Unloaders / Dock</label><input id="max_unloaders_per_dock" type="number" min="1"/></div>
        <div class="button-row">
          <button type="submit">Update Inputs</button>
          <button class="alt" type="button" id="refreshBtn">Refresh</button>
        </div>
      </form>
      <h2 style="margin-top:12px;">Simulation Controls</h2>
      <div class="button-row">
        <button type="button" id="stepBtn">Step 1 Minute</button>
        <button class="warn" type="button" id="run15Btn">Run 15 Minutes</button>
      </div>
    </section>

    <section class="panel span-8">
      <h2>Top KPI Cards</h2>
      <div class="grid-kpi" id="kpiGrid"></div>
    </section>

    <section class="panel span-7">
      <h2>Recommendation</h2>
      <div class="rec-main" id="recMain">No active recommendation.</div>
      <div id="recRationale" class="small-note">Waiting for next trigger.</div>
      <div class="chips" id="triggerChips"></div>
      <div class="chips" id="recStatusChips"></div>
      <div class="button-row">
        <button class="ok" type="button" id="applyRecBtn">Apply Recommendation</button>
        <button class="alt" type="button" id="keepPlanBtn">Keep Current Plan</button>
      </div>
    </section>

    <section class="panel span-5">
      <h2>Resource Summary</h2>
      <div id="resourceSummary"></div>
    </section>

    <section class="panel span-6">
      <h2>Staging Area Status</h2>
      <div class="staging-grid" id="stagingGrid"></div>
    </section>

    <section class="panel span-6">
      <h2>Dock Status</h2>
      <div style="overflow:auto;">
        <table>
          <thead>
            <tr>
              <th>Dock</th><th>Status</th><th>Truck</th><th>Type</th><th>Workers</th><th>Forklifts</th><th>Staging</th><th>ETA</th>
            </tr>
          </thead>
          <tbody id="dockTableBody"></tbody>
        </table>
      </div>
    </section>

    <section class="panel span-4">
      <h2>Verification</h2>
      <div class="verification" id="verificationCards"></div>
    </section>

    <section class="panel span-8">
      <h2>Trend Graphs</h2>
      <div class="chart-wrap">
        <div class="chart-card">
          <p class="chart-title">Queue Length</p>
          <svg id="queueChart" class="chart-svg" viewBox="0 0 300 120" preserveAspectRatio="none"></svg>
        </div>
        <div class="chart-card">
          <p class="chart-title">Arrivals / Minute</p>
          <svg id="arrivalChart" class="chart-svg" viewBox="0 0 300 120" preserveAspectRatio="none"></svg>
        </div>
        <div class="chart-card">
          <p class="chart-title">Max Staging Occupancy %</p>
          <svg id="stagingChart" class="chart-svg" viewBox="0 0 300 120" preserveAspectRatio="none"></svg>
        </div>
      </div>
    </section>
  </main>

  <script>
    async function apiGet(url) {
      const response = await fetch(url);
      if (!response.ok) throw new Error(await response.text());
      return response.json();
    }

    async function apiPost(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      });
      if (!response.ok) {
        const message = await response.text();
        throw new Error(message || "Request failed");
      }
      return response.json();
    }

    function renderKpis(kpis) {
      const defs = [
        ["Queue Length", String(kpis.queue_length)],
        ["Predicted Avg Wait", `${kpis.predicted_avg_wait_minutes.toFixed(2)} min`],
        ["Predicted Avg Time In System", `${kpis.predicted_avg_time_in_system_minutes.toFixed(2)} min`],
        ["Dock Utilization", `${kpis.dock_utilization.toFixed(1)}%`],
        ["Staging Risk", `${kpis.staging_risk_pct.toFixed(1)}%`],
        ["Recommended Action", kpis.recommended_action]
      ];
      document.getElementById("kpiGrid").innerHTML = defs.map(([label, value]) => `
        <div class="kpi"><div class="label">${label}</div><div class="value">${value}</div></div>
      `).join("");
    }

    function renderRecommendation(rec) {
      document.getElementById("recMain").textContent = rec.text;
      document.getElementById("recRationale").textContent = rec.rationale;
      const triggerHtml = (rec.trigger_source || []).map(source => `<span class="chip">${source}</span>`).join("");
      document.getElementById("triggerChips").innerHTML = triggerHtml || `<span class="chip">no recent trigger</span>`;

      const statusBits = [];
      if (rec.minute_generated != null) statusBits.push(`generated @ minute ${rec.minute_generated}`);
      statusBits.push(rec.is_applied ? "applied" : "not applied");
      statusBits.push(`decision: ${rec.decision_status}`);
      document.getElementById("recStatusChips").innerHTML = statusBits.map(bit => `<span class="chip">${bit}</span>`).join("");
    }

    function renderResources(resourceSummary) {
      const rows = [
        ["Total Workers", resourceSummary.workers_total],
        ["Assigned Workers", resourceSummary.workers_assigned],
        ["Idle Workers", resourceSummary.workers_idle],
        ["Total Forklifts", resourceSummary.forklifts_total],
        ["Assigned Forklifts", resourceSummary.forklifts_assigned],
        ["Idle Forklifts", resourceSummary.forklifts_idle]
      ];
      document.getElementById("resourceSummary").innerHTML = `
        <table>
          <tbody>
            ${rows.map(([k, v]) => `<tr><th style="width:65%;">${k}</th><td>${v}</td></tr>`).join("")}
          </tbody>
        </table>
      `;
    }

    function renderStaging(cards) {
      document.getElementById("stagingGrid").innerHTML = cards.map(card => `
        <div class="staging-card">
          <div class="dock-name"><span class="light ${card.traffic_light}"></span>Dock ${card.dock_id}</div>
          <div>${card.occupancy_pct.toFixed(1)}% occupied</div>
          <div class="small-note">${card.occupancy_units.toFixed(1)} / ${card.capacity_units.toFixed(1)} units</div>
        </div>
      `).join("");
    }

    function renderDockTable(rows) {
      document.getElementById("dockTableBody").innerHTML = rows.map(row => `
        <tr>
          <td>${row.dock_id}</td>
          <td>${row.status}</td>
          <td>${row.truck_id || "-"}</td>
          <td>${row.truck_type || "-"}</td>
          <td>${row.assigned_workers}</td>
          <td>${row.assigned_forklifts}</td>
          <td>${row.staging_occupancy_units.toFixed(1)} (${row.staging_occupancy_pct.toFixed(1)}%)</td>
          <td>${row.eta_text}</td>
        </tr>
      `).join("");
    }

    function renderVerification(verification) {
      const entries = Object.entries(verification || {});
      document.getElementById("verificationCards").innerHTML = entries.map(([name, card]) => `
        <div class="verify">
          <div class="name">${card.title || name.replaceAll("_", " ")}</div>
          <div class="status">${card.status || "unknown"}</div>
          <div class="small-note">${card.current_value || "-"}</div>
          <div class="small-note">Target: ${card.target || "-"}</div>
        </div>
      `).join("");
    }

    function renderLineChart(svgId, values, color, maxHint, xValues, yLabel) {
      const svg = document.getElementById(svgId);
      const width = 300;
      const height = 120;
      const margin = { top: 8, right: 8, bottom: 20, left: 34 };
      const usableW = width - margin.left - margin.right;
      const usableH = height - margin.top - margin.bottom;
      const data = values.length ? values : [0];
      const maxVal = Math.max(maxHint || 0, ...data, 1);
      const step = data.length > 1 ? usableW / (data.length - 1) : usableW;
      const points = data.map((value, i) => {
        const x = margin.left + i * step;
        const y = margin.top + usableH - (value / maxVal) * usableH;
        return `${x},${y}`;
      }).join(" ");
      const tickCount = 4;
      const yTicks = Array.from({ length: tickCount + 1 }, (_, i) => {
        const ratio = i / tickCount;
        const val = maxVal * (1 - ratio);
        const y = margin.top + usableH * ratio;
        return `<line x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" stroke="#dce8ef" stroke-width="1" />
                <text x="${margin.left - 4}" y="${y + 3}" text-anchor="end" font-size="8" fill="#6a7f8e">${val.toFixed(0)}</text>`;
      }).join("");
      const xStart = xValues && xValues.length ? xValues[0] : 0;
      const xEnd = xValues && xValues.length ? xValues[xValues.length - 1] : data.length - 1;
      svg.innerHTML = `
        ${yTicks}
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="2.5" />
        <line x1="${margin.left}" y1="${height - margin.bottom}" x2="${width - margin.right}" y2="${height - margin.bottom}" stroke="#bfd0da" stroke-width="1" />
        <line x1="${margin.left}" y1="${margin.top}" x2="${margin.left}" y2="${height - margin.bottom}" stroke="#bfd0da" stroke-width="1" />
        <text x="${margin.left}" y="${height - 4}" font-size="8" fill="#6a7f8e">t=${xStart}</text>
        <text x="${width - margin.right}" y="${height - 4}" text-anchor="end" font-size="8" fill="#6a7f8e">t=${xEnd}</text>
        <text x="${margin.left + 2}" y="${margin.top + 8}" font-size="8" fill="#6a7f8e">${yLabel || ""}</text>
      `;
    }

    function populateSupervisorInputs(inputs) {
      document.getElementById("available_workers").value = inputs.available_workers;
      document.getElementById("available_forklifts").value = inputs.available_forklifts;
      document.getElementById("active_docks").value = inputs.active_docks;
      document.getElementById("max_unloaders_per_dock").value = inputs.max_unloaders_per_dock;
    }

    function render(payload) {
      document.getElementById("minuteClock").textContent = `Minute ${payload.minute}`;
      populateSupervisorInputs(payload.supervisor_inputs);
      renderKpis(payload.kpis);
      renderRecommendation(payload.recommendation);
      renderResources(payload.resource_summary);
      renderStaging(payload.staging_status);
      renderDockTable(payload.dock_status);
      renderVerification(payload.verification);
      renderLineChart("queueChart", payload.trends.queue_length, "#0c7b93", null, payload.trends.minutes, "queue");
      renderLineChart("arrivalChart", payload.trends.arrivals, "#2f8f4e", null, payload.trends.minutes, "arrivals");
      renderLineChart("stagingChart", payload.trends.max_staging_occupancy_pct, "#c8473f", 100, payload.trends.minutes, "staging%");
    }

    async function refresh() {
      const payload = await apiGet("/api/state");
      render(payload);
    }

    async function step(minutes) {
      const payload = await apiPost("/api/step", { minutes });
      render(payload);
    }

    async function updateSupervisor(event) {
      event.preventDefault();
      const payload = {
        available_workers: Number(document.getElementById("available_workers").value),
        available_forklifts: Number(document.getElementById("available_forklifts").value),
        active_docks: Number(document.getElementById("active_docks").value),
        max_unloaders_per_dock: Number(document.getElementById("max_unloaders_per_dock").value),
      };
      const result = await apiPost("/api/supervisor", payload);
      render(result);
    }

    async function applyRecommendation() {
      const payload = await apiPost("/api/recommendation/apply", {});
      render(payload);
    }

    async function keepCurrentPlan() {
      const payload = await apiPost("/api/recommendation/keep", {});
      render(payload);
    }

    function wireEvents() {
      document.getElementById("supervisorForm").addEventListener("submit", async (event) => {
        try { await updateSupervisor(event); } catch (err) { alert(err.message); }
      });
      document.getElementById("stepBtn").addEventListener("click", async () => {
        try { await step(1); } catch (err) { alert(err.message); }
      });
      document.getElementById("run15Btn").addEventListener("click", async () => {
        try { await step(15); } catch (err) { alert(err.message); }
      });
      document.getElementById("applyRecBtn").addEventListener("click", async () => {
        try { await applyRecommendation(); } catch (err) { alert(err.message); }
      });
      document.getElementById("keepPlanBtn").addEventListener("click", async () => {
        try { await keepCurrentPlan(); } catch (err) { alert(err.message); }
      });
      document.getElementById("refreshBtn").addEventListener("click", async () => {
        try { await refresh(); } catch (err) { alert(err.message); }
      });
    }

    wireEvents();
    refresh().catch(err => alert(err.message));
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler exposing dashboard page and local JSON endpoints."""

    runtime: DashboardRuntime
    lock: threading.Lock

    def _write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _parse_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._write_html(HTML_PAGE)
            return
        if self.path == "/api/state":
            with self.lock:
                payload = self.runtime.get_dashboard_payload()
            self._write_json(payload)
            return
        self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._parse_json_body()
            with self.lock:
                if self.path == "/api/step":
                    minutes = int(payload.get("minutes", 1))
                    result = self.runtime.step(minutes=minutes)
                elif self.path == "/api/supervisor":
                    result = self.runtime.update_supervisor(payload)
                elif self.path == "/api/recommendation/apply":
                    result = self.runtime.apply_recommendation()
                elif self.path == "/api/recommendation/keep":
                    result = self.runtime.keep_current_plan()
                else:
                    self._write_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
                    return
            self._write_json(result)
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except json.JSONDecodeError:
            self._write_json({"error": "invalid json payload"}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def _build_handler(runtime: DashboardRuntime, lock: threading.Lock) -> type[DashboardRequestHandler]:
    class Handler(DashboardRequestHandler):
        pass

    Handler.runtime = runtime
    Handler.lock = lock
    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local yard dashboard server.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787, help="Port to bind (default: 8787).")
    args = parser.parse_args()

    runtime = DashboardRuntime.create_default()
    lock = threading.Lock()
    handler = _build_handler(runtime, lock)
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print(f"Dashboard running on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
