"""Vendored copy of use-cases/common/controller.py.

The launcher is a standalone top-level app with its own Docker build context,
so it carries a copy of the shared orchestrator client. Keep in sync with that canonical file.
"""

import base64
import json
import os

import httpx


def inline(obj) -> dict:
    """Build an inline-JSON DataReference from a Python object."""
    return {"protocol": "inline",
            "uri": base64.b64encode(json.dumps(obj).encode()).decode(),
            "format": "json"}


def decode(ref) -> dict:
    """Decode an inline-JSON DataReference back to a dict ({} if not inline)."""
    if ref and ref.get("protocol") == "inline":
        return json.loads(base64.b64decode(ref.get("uri", "")).decode())
    return {}


# ----- blueprint / dockerinfo builders -----
# Controllers that build pipelines in code can use these instead of writing the
# verbose JSON shapes by hand.

def blueprint_node(container, operation, input_message="Request",
                   output_message="Response", connected_to=(), node_type="MLModel",
                   image=None, proto_uri=None) -> dict:
    """Build one blueprint node.

    connected_to: iterable of (container, operation) pairs this node feeds into.
    image / proto_uri default to sensible values; they do not affect execution
    (the orchestrator reaches services via dockerinfo, not the image name).
    """
    return {
        "container_name": container,
        "proto_uri": proto_uri or f"{container}.proto",
        "image": image or f"{container}:latest",
        "node_type": node_type,
        "operation_signature_list": [{
            "operation_signature": {
                "operation_name": operation,
                "input_message_name": input_message,
                "output_message_name": output_message,
            },
            "connected_to": [
                {"container_name": c, "operation_signature": {"operation_name": o}}
                for c, o in connected_to
            ],
        }],
    }


def make_blueprint(name, nodes, pipeline_id=None, version="2.0") -> dict:
    """Wrap a list of nodes into a complete pipeline-topology/v2 blueprint."""
    import uuid
    from datetime import datetime
    return {
        "name": name,
        "pipeline_id": pipeline_id or uuid.uuid4().hex[:12],
        "creation_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": "pipeline-topology/v2",
        "version": version,
        "nodes": nodes,
    }


def make_dockerinfo(services) -> dict:
    """Build dockerinfo. services: iterable of (container, ip_address, port) tuples
    or ready-made dicts."""
    entries = []
    for s in services:
        if isinstance(s, dict):
            entries.append(s)
        else:
            container, ip, port = s
            entries.append({"container_name": container, "ip_address": ip, "port": str(port)})
    return {"docker_info_list": entries}


# ----- mapping orchestrator task state onto a logical graph -----
# Work on the `tasks` dict returned by OrchestratorClient.workflow(), so a
# controller can fetch the workflow once and reuse it.

def node_statuses(tasks: dict, node_key_map: dict) -> dict:
    """Map a workflow's tasks onto logical node keys.

    node_key_map: {logical_key: orchestrator_node_key}.
    Returns {logical_key: status} ("pending" when a task is absent).
    """
    return {logical: (tasks.get(nk) or {}).get("status", "pending")
            for logical, nk in node_key_map.items()}


def node_output(tasks: dict, node_key: str) -> dict:
    """Decoded inline output of a task by its orchestrator node_key ({} if none)."""
    refs = (tasks.get(node_key) or {}).get("output_refs") or []
    return decode(refs[0]) if refs else {}


class OrchestratorClient:
    """Thin client over the orchestrator's workflow + solution-view APIs."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get(
            "ORCHESTRATOR_URL", "http://host.docker.internal:18000")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("ORCHESTRATOR_API_KEY", "")
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    # ----- workflows -----
    def submit(self, blueprint: dict, dockerinfo: dict, inputs: list | None = None) -> str:
        """Submit a workflow; returns its workflow_id. Raises on HTTP error."""
        with httpx.Client(timeout=self.timeout) as c:
            r = c.post(f"{self.base_url}/workflows",
                       json={"blueprint": blueprint, "dockerinfo": dockerinfo, "inputs": inputs or []},
                       headers=self._headers())
        r.raise_for_status()
        return r.json().get("workflow_id")

    def workflow(self, workflow_id: str):
        """Return (workflow_status, {node_key: task_dict})."""
        with httpx.Client(timeout=self.timeout) as c:
            s = c.get(f"{self.base_url}/workflows/{workflow_id}", headers=self._headers())
            s.raise_for_status()
            t = c.get(f"{self.base_url}/workflows/{workflow_id}/tasks", headers=self._headers())
        tasks = {task["node_key"]: task
                 for task in (t.json().get("tasks", []) if t.status_code == 200 else [])}
        return s.json().get("status"), tasks

    def task_statuses(self, workflow_id: str) -> dict:
        """Return {node_key: status} for a workflow's tasks."""
        _, tasks = self.workflow(workflow_id)
        return {k: v.get("status") for k, v in tasks.items()}

    def workflow_view(self, workflow_id: str) -> dict:
        """Full status + error + task list, for a monitoring UI.

        Returns {"status", "error", "tasks": [...]}. Raises on HTTP error.
        """
        with httpx.Client(timeout=self.timeout) as c:
            s = c.get(f"{self.base_url}/workflows/{workflow_id}", headers=self._headers())
            s.raise_for_status()
            t = c.get(f"{self.base_url}/workflows/{workflow_id}/tasks", headers=self._headers())
        sj = s.json()
        return {"status": sj.get("status"), "error": sj.get("error"),
                "tasks": t.json().get("tasks", []) if t.status_code == 200 else []}

    def task_output(self, workflow_id: str, node_key_prefix: str) -> dict:
        """Return the decoded inline output of the first task whose node_key
        starts with node_key_prefix (e.g. a container name)."""
        _, tasks = self.workflow(workflow_id)
        for key, task in tasks.items():
            if key.startswith(node_key_prefix):
                refs = task.get("output_refs") or []
                if refs:
                    return decode(refs[0])
        return {}

    # ----- solution view (logical graph + live state for the dashboard) -----
    def publish_graph(self, view_id: str, name: str, nodes: list, edges: list) -> None:
        try:
            with httpx.Client(timeout=10.0) as c:
                c.put(f"{self.base_url}/solutions/{view_id}",
                      json={"name": name, "nodes": nodes, "edges": edges}, headers=self._headers())
        except httpx.HTTPError:
            pass

    def publish_state(self, view_id: str, statuses: dict, stage: str | None = None,
                      message: str | None = None, detail=None) -> None:
        try:
            with httpx.Client(timeout=10.0) as c:
                c.put(f"{self.base_url}/solutions/{view_id}/state",
                      json={"statuses": statuses, "stage": stage, "message": message, "detail": detail},
                      headers=self._headers())
        except httpx.HTTPError:
            pass
