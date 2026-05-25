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

from orchestrator_client import OrchestratorClient

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_URL", "http://host.docker.internal:18000"
).rstrip("/")

app = FastAPI(title="AI-EFFECT Launcher", version="1.0.0")

# Shared orchestrator client (reads ORCHESTRATOR_URL / API key from env).
_oc = OrchestratorClient()

# In-memory store of uploaded solutions. This is a single-user local tool, so a
# process-lifetime dict is enough; nothing here needs to survive a restart.
SOLUTIONS: dict[str, dict] = {}


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


@app.post("/api/solutions/{sid}/start-services")
def start_services(sid: str):
    """Pull each service's image and run it on the ai-effect-services network.

    Phase 2: needs the Docker socket mounted into the launcher. Each service is
    run as a container named after its dockerinfo ip_address (so the orchestrator
    reaches it by that name), on the shared network, with a shared per-solution
    volume at /app/data (file-based pipelines exchange data through it; harmless
    otherwise).
    """
    sol = SOLUTIONS.get(sid)
    if not sol:
        raise HTTPException(404, "Solution not found (re-upload it)")

    try:
        import docker
        client = docker.from_env()
        client.ping()
    except Exception as e:
        raise HTTPException(
            503,
            "Docker is not available to the launcher. Starting services needs the "
            f"Docker socket mounted (see docker-compose.yml). Details: {e}",
        )

    images = {n.get("container_name"): n.get("image") for n in sol["blueprint"].get("nodes", [])}
    vol = f"aieffect_{sid}_data"
    results = []
    for d in sol["dockerinfo"].get("docker_info_list", []):
        cn, ip, port = d.get("container_name"), d.get("ip_address"), str(d.get("port"))
        image = images.get(cn)
        if not image:
            results.append({"container_name": cn, "name": ip, "state": "no image in blueprint"})
            continue
        try:
            client.images.pull(image)
            try:
                existing = client.containers.get(ip)
                existing.remove(force=True)
            except docker.errors.NotFound:
                pass
            client.containers.run(
                image, name=ip, detach=True, network="ai-effect-services",
                environment={"PORT": port},
                volumes={vol: {"bind": "/app/data", "mode": "rw"}},
                restart_policy={"Name": "unless-stopped"},
            )
            results.append({"container_name": cn, "name": ip, "image": image, "state": "started"})
        except Exception as e:
            results.append({"container_name": cn, "name": ip, "image": image,
                            "state": "error", "error": str(e)})
    return {"services": results}


@app.get("/api/solutions/{sid}/readiness")
def readiness(sid: str):
    """Probe each service's address and report up/down."""
    sol = SOLUTIONS.get(sid)
    if not sol:
        raise HTTPException(404, "Solution not found (re-upload it)")
    results = []
    for d in sol["dockerinfo"].get("docker_info_list", []):
        ip = d.get("ip_address")
        # Web UI link, if the service declared one: webui_url verbatim, else
        # built from the address and webui_port.
        webui = d.get("webui_url")
        if not webui and d.get("webui_port"):
            webui = f"http://{ip}:{d.get('webui_port')}"
        try:
            port = int(d.get("port"))
        except (TypeError, ValueError):
            results.append({"container_name": d.get("container_name"),
                            "address": f"{ip}:{d.get('port')}", "state": "down", "webui": webui})
            continue
        results.append({"container_name": d.get("container_name"),
                        "address": f"{ip}:{port}", "state": _probe(ip, port), "webui": webui})
    all_ready = bool(results) and all(r["state"] != "down" for r in results)
    return {"services": results, "all_ready": all_ready}


@app.post("/api/solutions/{sid}/run")
def run_solution(sid: str, body: dict | None = None):
    """Submit the solution's workflow to the orchestrator."""
    sol = SOLUTIONS.get(sid)
    if not sol:
        raise HTTPException(404, "Solution not found (re-upload it)")

    inputs = (body or {}).get("inputs") or []
    try:
        workflow_id = _oc.submit(sol["blueprint"], sol["dockerinfo"], inputs)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Orchestrator rejected submit ({e.response.status_code}): {e.response.text}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach orchestrator at {ORCHESTRATOR_URL}: {e}")
    return {"workflow_id": workflow_id}


@app.get("/api/workflows/{workflow_id}")
def workflow_status(workflow_id: str):
    """Proxy the orchestrator's status + tasks for live monitoring."""
    try:
        return _oc.workflow_view(workflow_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(404, "Workflow not found")
        raise HTTPException(502, f"Orchestrator error: {e}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach orchestrator: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "orchestrator_url": ORCHESTRATOR_URL}


_ui_dir = Path(__file__).resolve().parent / "ui"
if _ui_dir.exists():
    app.mount("/", StaticFiles(directory=str(_ui_dir), html=True), name="ui")
