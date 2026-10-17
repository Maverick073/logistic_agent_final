const number = (value) => value === null || value === undefined ? "—" : Number(value).toLocaleString();

async function getJson(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "The request could not be completed.");
  return data;
}

function addCell(row, text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  row.append(cell);
}

function renderHistory(runs) {
  const body = document.getElementById("history");
  body.replaceChildren();
  if (!runs.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "empty";
    cell.textContent = "No runs yet.";
    row.append(cell);
    body.append(row);
    return;
  }
  for (const run of runs) {
    const row = document.createElement("tr");
    addCell(row, run.run_id);
    addCell(row, run.objective || run.guardrail_category || "—");
    addCell(row, run.evaluation_status || run.status);
    addCell(row, run.latency_ms === null || run.latency_ms === undefined ? "—" : `${number(run.latency_ms)} ms`);
    addCell(row, String(run.token_usage?.total_tokens ?? 0));
    body.append(row);
  }
}

async function refreshDashboard() {
  const data = await getJson("/dashboard/api");
  document.getElementById("runs").textContent = number(data.totals.completed_runs);
  document.getElementById("latency").textContent =
    data.totals.average_latency_ms === null ? "—" : `${number(data.totals.average_latency_ms)} ms`;
  document.getElementById("latency-sample").textContent =
    `${number(data.totals.latency_sample_size)} measured run(s)`;
  document.getElementById("input-tokens").textContent = number(data.token_usage.aggregate_input_tokens);
  document.getElementById("output-tokens").textContent = number(data.token_usage.aggregate_output_tokens);
  document.getElementById("guardrail-summary").textContent =
    `${number(data.guardrails.blocked_requests)} unsupported or invalid request(s) blocked.`;
  const list = document.getElementById("guardrail-list");
  list.replaceChildren();
  for (const control of data.guardrails.controls) {
    const item = document.createElement("li");
    item.textContent = control;
    list.append(item);
  }
  document.getElementById("scope").textContent = data.metrics_scope;
  renderHistory(data.recent_runs);
}

document.getElementById("run-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("run-error");
  error.textContent = "";
  const resultNode = document.getElementById("run-result");
  const deadline = document.getElementById("deadline").value || null;
  try {
    const result = await getJson("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        objective: document.getElementById("objective").value,
        amazon_deadline: deadline,
      }),
    });
    resultNode.textContent = JSON.stringify(result, null, 2);
    await refreshDashboard();
  } catch (cause) {
    error.textContent = cause.message;
  }
});

document.getElementById("refresh").addEventListener("click", () => {
  refreshDashboard().catch((cause) => {
    document.getElementById("guardrail-summary").textContent = cause.message;
  });
});

refreshDashboard().catch((cause) => {
  document.getElementById("guardrail-summary").textContent = cause.message;
});
