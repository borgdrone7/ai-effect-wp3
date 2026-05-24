"""AI-EFFECT Launcher.

A small local web app that makes running a solution point-and-click, with no
command line:

  1. Upload a solution zip (blueprint.json + dockerinfo.json).
  2. See each service's readiness (the launcher is on the ai-effect-services
     network, so it can reach services by their docker names and ping /health).
  3. Click "Start workflow" to submit it to the local orchestrator, then watch
     the per-node status live.

Phase 1 assumes the services are already running (started from a registry image
or by hand). Starting services from images is a separate, later capability.

This app talks to the orchestrator's existing API only; it requires no changes
to the orchestrator. Run it as a container joined to the ai-effect-services
network (see docker-compose.yml).
"""

import io
import json
import os
import socket
import uuid
import zipfile

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_URL", "http://host.docker.internal:18000"
).rstrip("/")
ORCHESTRATOR_API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")

app = FastAPI(title="AI-EFFECT Launcher", version="1.0.0")

# In-memory store of uploaded solutions. This is a single-user local tool, so a
# process-lifetime dict is enough; nothing here needs to survive a restart.
SOLUTIONS: dict[str, dict] = {}


def _orch_headers() -> dict:
    return {"Authorization": f"Bearer {ORCHESTRATOR_API_KEY}"} if ORCHESTRATOR_API_KEY else {}


def _read_zip_member(zf: zipfile.ZipFile, name: str) -> dict | None:
    """Read a top-level JSON member from the zip, tolerating a leading folder."""
    candidates = [n for n in zf.namelist() if n.endswith(name)]
    # Prefer the shallowest match (e.g. "blueprint.json" over "x/blueprint.json").
    candidates.sort(key=lambda n: n.count("/"))
    if not candidates:
        return None
    with zf.open(candidates[0]) as f:
        return json.loads(f.read().decode())


def build_graph(blueprint: dict, status_by_key: dict | None = None) -> dict:
    """Build {nodes, edges} from a blueprint, keyed by container:operation."""
    status_by_key = status_by_key or {}
    nodes, edges = [], []
    for node in blueprint.get("nodes", []):
        container = node.get("container_name")
        node_type = node.get("node_type", "MLModel")
        op_list = node.get("operation_signature_list", []) or []
        if not op_list:
            nodes.append({"key": container, "container_name": container,
                          "operation": None, "node_type": node_type,
                          "status": status_by_key.get(container, "pending")})
            continue
        for op in op_list:
            op_name = (op.get("operation_signature") or {}).get("operation_name")
            key = f"{container}:{op_name}"
            nodes.append({"key": key, "container_name": container,
                          "operation": op_name, "node_type": node_type,
                          "status": status_by_key.get(key, "pending")})
            for conn in op.get("connected_to", []) or []:
                to_op = (conn.get("operation_signature") or {}).get("operation_name")
                edges.append({"from": key, "to": f"{conn.get('container_name')}:{to_op}"})
    return {"nodes": nodes, "edges": edges}


@app.post("/api/solutions")
async def upload_solution(file: UploadFile = File(...)):
    """Accept a solution zip, extract blueprint + dockerinfo, return a summary."""
    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Not a valid zip file")

    blueprint = _read_zip_member(zf, "blueprint.json")
    dockerinfo = _read_zip_member(zf, "dockerinfo.json")
    if not blueprint:
        raise HTTPException(400, "blueprint.json not found in zip")
    if not dockerinfo:
        raise HTTPException(400, "dockerinfo.json not found in zip")

    sid = uuid.uuid4().hex[:12]
    SOLUTIONS[sid] = {"blueprint": blueprint, "dockerinfo": dockerinfo,
                      "name": blueprint.get("name", file.filename)}

    services = [
        {"container_name": d.get("container_name"),
         "ip_address": d.get("ip_address"), "port": str(d.get("port"))}
        for d in dockerinfo.get("docker_info_list", [])
    ]
    return {"id": sid, "name": SOLUTIONS[sid]["name"],
            "services": services, "graph": build_graph(blueprint)}


def _probe(ip: str, port: int) -> str:
    """Return 'healthy' | 'up' | 'down' for a service address."""
    base = f"http://{ip}:{port}"
    try:
        r = httpx.get(f"{base}/health", timeout=2.0)
        return "healthy" if r.status_code < 400 else "up"
    except httpx.HTTPError:
        pass
    # No HTTP /health; fall back to a plain TCP connect to the port.
    try:
        with socket.create_connection((ip, port), timeout=2.0):
            return "up"
    except OSError:
        return "down"


@app.get("/api/solutions/{sid}/readiness")
def readiness(sid: str):
    """Probe each service's address and report up/down."""
    sol = SOLUTIONS.get(sid)
    if not sol:
        raise HTTPException(404, "Solution not found (re-upload it)")
    results = []
    for d in sol["dockerinfo"].get("docker_info_list", []):
        ip = d.get("ip_address")
        try:
            port = int(d.get("port"))
        except (TypeError, ValueError):
            results.append({"container_name": d.get("container_name"),
                            "address": f"{ip}:{d.get('port')}", "state": "down"})
            continue
        results.append({"container_name": d.get("container_name"),
                        "address": f"{ip}:{port}", "state": _probe(ip, port)})
    all_ready = bool(results) and all(r["state"] != "down" for r in results)
    return {"services": results, "all_ready": all_ready}


@app.post("/api/solutions/{sid}/run")
def run_solution(sid: str, body: dict | None = None):
    """Submit the solution's workflow to the orchestrator."""
    sol = SOLUTIONS.get(sid)
    if not sol:
        raise HTTPException(404, "Solution not found (re-upload it)")

    inputs = (body or {}).get("inputs") or []
    payload = {"blueprint": sol["blueprint"], "dockerinfo": sol["dockerinfo"], "inputs": inputs}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(f"{ORCHESTRATOR_URL}/workflows", json=payload, headers=_orch_headers())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach orchestrator at {ORCHESTRATOR_URL}: {e}")
    if r.status_code != 200:
        raise HTTPException(502, f"Orchestrator rejected submit ({r.status_code}): {r.text}")
    return {"workflow_id": r.json().get("workflow_id")}


@app.get("/api/workflows/{workflow_id}")
def workflow_status(workflow_id: str):
    """Proxy the orchestrator's status + tasks for live monitoring."""
    try:
        with httpx.Client(timeout=15.0) as client:
            s = client.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}", headers=_orch_headers())
            if s.status_code == 404:
                raise HTTPException(404, "Workflow not found")
            t = client.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}/tasks", headers=_orch_headers())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach orchestrator: {e}")
    status = s.json()
    tasks = t.json().get("tasks", []) if t.status_code == 200 else []
    return {"status": status.get("status"), "error": status.get("error"), "tasks": tasks}


@app.get("/health")
def health():
    return {"status": "ok", "orchestrator_url": ORCHESTRATOR_URL}


_ui_dir = Path(__file__).resolve().parent / "ui"
if _ui_dir.exists():
    app.mount("/", StaticFiles(directory=str(_ui_dir), html=True), name="ui")
