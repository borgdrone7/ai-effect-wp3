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
from pathlib import Path

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


# The page HTML lives next to this file and is loaded from disk.
INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
