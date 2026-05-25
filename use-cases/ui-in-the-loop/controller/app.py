"""UI-in-the-loop controller.

This is the "external controller" from the human-in-the-loop pattern: it serves
a small web UI and, on each user action, submits a fresh workflow to the
orchestrator, waits for it to finish, and shows the result. The user can change
their choice and run again. The loop lives here, not in the orchestrator; each
run is an independent DAG execution (processor -> summarizer).

Environment:
  ORCHESTRATOR_URL   Base URL of the orchestrator API (default
                     http://host.docker.internal:18000).
  ORCHESTRATOR_API_KEY  Optional bearer token if the orchestrator requires one.
  PORT               Port to serve on (default 8080).
"""

import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from common.controller import OrchestratorClient, inline

# The inner pipeline this controller runs on every user action.
BLUEPRINT = {
    "name": "UI-in-the-loop demand scenario",
    "pipeline_id": "ui-in-the-loop",
    "creation_date": "2026-05-01",
    "type": "pipeline-topology/v2",
    "version": "2.0",
    "nodes": [
        {
            "container_name": "processor",
            "proto_uri": "processor.proto",
            "image": "ui-in-the-loop-processor:latest",
            "node_type": "DataSource",
            "operation_signature_list": [
                {
                    "operation_signature": {
                        "operation_name": "Process",
                        "input_message_name": "ProcessRequest",
                        "output_message_name": "ProcessResponse",
                    },
                    "connected_to": [
                        {
                            "container_name": "summarizer",
                            "operation_signature": {"operation_name": "Summarize"},
                        }
                    ],
                }
            ],
        },
        {
            "container_name": "summarizer",
            "proto_uri": "summarizer.proto",
            "image": "ui-in-the-loop-summarizer:latest",
            "node_type": "DataSink",
            "operation_signature_list": [
                {
                    "operation_signature": {
                        "operation_name": "Summarize",
                        "input_message_name": "SummarizeRequest",
                        "output_message_name": "SummarizeResponse",
                    },
                    "connected_to": [],
                }
            ],
        },
    ],
}

DOCKERINFO = {
    "docker_info_list": [
        {"container_name": "processor", "ip_address": "processor", "port": "8080"},
        {"container_name": "summarizer", "ip_address": "summarizer", "port": "8080"},
    ]
}

app = FastAPI(title="UI-in-the-loop Controller", version="1.0.0")


class RunRequest(BaseModel):
    scenario: str = "baseline"
    factor: float = 1.0


oc = OrchestratorClient()


@app.post("/run")
def run(req: RunRequest):
    """Submit one workflow for the user's choice, wait, return the result."""
    workflow_id = oc.submit(BLUEPRINT, DOCKERINFO,
                            [inline({"scenario": req.scenario, "factor": req.factor})])

    deadline = time.time() + 60
    status = "pending"
    while time.time() < deadline:
        status, _ = oc.workflow(workflow_id)
        if status in ("completed", "failed"):
            break
        time.sleep(1.0)

    if status != "completed":
        return {"workflow_id": workflow_id, "status": status, "error": "failed or timed out"}

    result = oc.task_output(workflow_id, "summarizer")
    return {"workflow_id": workflow_id, "status": "completed", "result": result}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI-EFFECT — UI in the Loop</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; color: #1a1a2e; }
  h1 { font-size: 1.4rem; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 20px; margin-top: 16px; }
  label { display: block; margin: 12px 0 4px; font-weight: 600; }
  select, input { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 1rem; }
  button { margin-top: 18px; padding: 10px 18px; border: 0; border-radius: 6px; background: #3a3aff; color: #fff; font-size: 1rem; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  .result { margin-top: 18px; }
  .summary { font-size: 1.05rem; }
  .rec { margin-top: 8px; padding: 10px; background: #f3f3ff; border-radius: 6px; }
  .metrics { margin-top: 10px; font-family: monospace; color: #444; }
  .muted { color: #888; font-size: .85rem; }
</style>
</head>
<body>
  <h1>Demand scenario explorer</h1>
  <p class="muted">Pick a scenario, run it, read the result, adjust, run again.
     Each run submits a fresh workflow (processor → summarizer) to the orchestrator.
     The loop lives in this page, not in the orchestrator.</p>

  <div class="card">
    <label for="scenario">Scenario</label>
    <select id="scenario">
      <option value="baseline">Baseline</option>
      <option value="cold snap">Cold snap</option>
      <option value="heat wave">Heat wave</option>
      <option value="efficiency drive">Efficiency drive</option>
    </select>

    <label for="factor">Demand scaling factor (0.5 – 2.0)</label>
    <input id="factor" type="number" min="0.5" max="2.0" step="0.1" value="1.0" />

    <button id="run">Run</button>

    <div class="result" id="result"></div>
  </div>

<script>
const btn = document.getElementById('run');
const out = document.getElementById('result');

btn.addEventListener('click', async () => {
  btn.disabled = true;
  out.innerHTML = '<p class="muted">Running…</p>';
  try {
    const res = await fetch('run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scenario: document.getElementById('scenario').value,
        factor: parseFloat(document.getElementById('factor').value),
      }),
    });
    const data = await res.json();
    if (data.status !== 'completed') {
      out.innerHTML = '<p class="rec">Run ' + (data.status || 'failed') +
        (data.error ? (': ' + data.error) : '') + '</p>';
    } else {
      const r = data.result || {};
      out.innerHTML =
        '<div class="summary">' + (r.summary || '(no summary)') + '</div>' +
        '<div class="rec">' + (r.recommendation || '') + '</div>' +
        '<div class="metrics">' + JSON.stringify(r.metrics || {}) + '</div>' +
        '<div class="muted">workflow ' + data.workflow_id + '</div>';
    }
  } catch (e) {
    out.innerHTML = '<p class="rec">Error: ' + e.message + '</p>';
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>
"""
