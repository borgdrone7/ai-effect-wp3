"""FastAPI REST API for orchestration platform."""

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from redis import Redis

# Registry of all workflow ids the orchestrator has seen, for the dashboard's
# "list workflows" view (the core API never needed to enumerate them).
_WORKFLOWS_KEY = "workflows"

_bearer = HTTPBearer(auto_error=False)


def _verify_orchestrator_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    api_key = os.environ.get("ORCHESTRATOR_API_KEY")
    if not api_key:
        return
    if not credentials or credentials.credentials != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

from api.models import (
    DataReferenceResponse,
    ErrorResponse,
    HealthResponse,
    TaskListResponse,
    TaskStatusResponse,
    WorkflowStatusResponse,
    WorkflowSubmitRequest,
    WorkflowSubmitResponse,
)
from models.data_reference import DataReference
from services.blueprint_parser import BlueprintParseError, BlueprintParser
from services.dockerinfo_parser import DockerInfoParseError, DockerInfoParser
from services.state_store import WorkflowNotFoundError, TaskNotFoundError
from services.workflow_engine import WorkflowEngine


class OrchestratorAPI:
    """REST API for orchestration platform."""

    def __init__(
        self,
        engine: WorkflowEngine,
        blueprint_parser: BlueprintParser,
        dockerinfo_parser: DockerInfoParser,
        redis_client: Redis,
    ):
        """Initialize API with dependencies."""
        if engine is None:
            raise ValueError("engine is required")
        if blueprint_parser is None:
            raise ValueError("blueprint_parser is required")
        if dockerinfo_parser is None:
            raise ValueError("dockerinfo_parser is required")
        if redis_client is None:
            raise ValueError("redis_client is required")

        self._engine = engine
        self._blueprint_parser = blueprint_parser
        self._dockerinfo_parser = dockerinfo_parser
        self._redis = redis_client

    def create_app(self) -> FastAPI:
        """Create FastAPI application."""
        app = FastAPI(
            title="Orchestrator API",
            description="REST API for AI-Effect orchestration platform",
            version="1.0.0",
        )

        @app.post(
            "/workflows",
            response_model=WorkflowSubmitResponse,
            responses={400: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def submit_workflow(request: WorkflowSubmitRequest) -> WorkflowSubmitResponse:
            """Submit a new workflow."""
            # Parse blueprint
            try:
                graph = self._blueprint_parser.parse_json(request.blueprint)
            except BlueprintParseError as e:
                raise HTTPException(status_code=400, detail=f"Invalid blueprint: {e}")

            # Parse dockerinfo
            try:
                endpoints = self._dockerinfo_parser.parse_json(request.dockerinfo)
            except DockerInfoParseError as e:
                raise HTTPException(status_code=400, detail=f"Invalid dockerinfo: {e}")

            # Generate workflow ID
            workflow_id = f"wf-{uuid.uuid4().hex[:12]}"

            # Store services API key for worker to use when calling services
            if request.services_api_key:
                self._redis.set(f"services_key:{workflow_id}", request.services_api_key)

            # Store endpoints for worker lookup
            endpoints_key = f"endpoints:{workflow_id}"
            endpoints_data = {
                name: endpoint.model_dump_json()
                for name, endpoint in endpoints.items()
            }
            if endpoints_data:
                self._redis.hset(endpoints_key, mapping=endpoints_data)

            # Persist the raw blueprint so the dashboard can reconstruct the
            # graph later (the engine only keeps a node->task mapping), and
            # register the workflow id for the "list workflows" view.
            self._redis.set(f"blueprint:{workflow_id}", json.dumps(request.blueprint))
            self._redis.sadd(_WORKFLOWS_KEY, workflow_id)

            # Parse initial inputs for start nodes
            initial_inputs: list[DataReference] | None = None
            if request.inputs:
                try:
                    initial_inputs = [DataReference(**inp) for inp in request.inputs]
                except Exception as e:
                    raise HTTPException(
                        status_code=400, detail=f"Invalid inputs: {e}"
                    )

            # Initialize and start workflow
            self._engine.initialize_workflow(workflow_id, graph)
            self._engine.start_workflow(workflow_id, initial_inputs)

            return WorkflowSubmitResponse(workflow_id=workflow_id, status="running")

        @app.get(
            "/workflows/{workflow_id}",
            response_model=WorkflowStatusResponse,
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_workflow_status(workflow_id: str) -> WorkflowStatusResponse:
            """Get workflow status."""
            try:
                state = self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            return WorkflowStatusResponse(
                workflow_id=state.workflow_id,
                status=state.status.value,
                created_at=state.created_at,
                updated_at=state.updated_at,
                error=state.error,
            )

        @app.get(
            "/workflows/{workflow_id}/tasks",
            response_model=TaskListResponse,
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_workflow_tasks(workflow_id: str) -> TaskListResponse:
            """Get all tasks for a workflow."""
            # Check workflow exists
            try:
                self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            # Get all tasks
            tasks = self._engine.get_all_tasks(workflow_id)
            task_responses = [
                TaskStatusResponse(
                    task_id=task.task_id,
                    node_key=task.node_key,
                    status=task.status.value,
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                    error=task.error,
                    input_refs=[
                        DataReferenceResponse(
                            protocol=r.protocol.value,
                            uri=r.uri,
                            format=r.format if isinstance(r.format, str) else r.format.value,
                            metadata=r.metadata,
                        )
                        for r in task.input_refs
                    ],
                    output_refs=[
                        DataReferenceResponse(
                            protocol=r.protocol.value,
                            uri=r.uri,
                            format=r.format if isinstance(r.format, str) else r.format.value,
                            metadata=r.metadata,
                        )
                        for r in task.output_refs
                    ],
                )
                for task in tasks
            ]

            return TaskListResponse(workflow_id=workflow_id, tasks=task_responses)

        @app.get(
            "/workflows/{workflow_id}/tasks/{task_id}",
            response_model=TaskStatusResponse,
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_task_status(workflow_id: str, task_id: str) -> TaskStatusResponse:
            """Get task status."""
            try:
                task = self._engine._state_store.get_task(workflow_id, task_id)
            except (WorkflowNotFoundError, TaskNotFoundError):
                raise HTTPException(status_code=404, detail="Task not found")

            return TaskStatusResponse(
                task_id=task.task_id,
                node_key=task.node_key,
                status=task.status.value,
                created_at=task.created_at,
                updated_at=task.updated_at,
                error=task.error,
                input_refs=[
                    DataReferenceResponse(
                        protocol=r.protocol.value,
                        uri=r.uri,
                        format=r.format if isinstance(r.format, str) else r.format.value,
                        metadata=r.metadata,
                    )
                    for r in task.input_refs
                ],
                output_refs=[
                    DataReferenceResponse(
                        protocol=r.protocol.value,
                        uri=r.uri,
                        format=r.format if isinstance(r.format, str) else r.format.value,
                        metadata=r.metadata,
                    )
                    for r in task.output_refs
                ],
            )

        @app.delete(
            "/workflows/{workflow_id}",
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def delete_workflow(workflow_id: str) -> dict:
            """Delete a workflow."""
            try:
                self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            # Delete workflow state
            self._engine._state_store.delete_workflow(workflow_id)

            # Delete endpoints
            self._redis.delete(f"endpoints:{workflow_id}")

            # Delete persisted blueprint and deregister
            self._redis.delete(f"blueprint:{workflow_id}")
            self._redis.srem(_WORKFLOWS_KEY, workflow_id)

            # Clear queue
            self._engine._task_queue.clear_queue(workflow_id)

            return {"status": "deleted"}

        @app.get("/health", response_model=HealthResponse)
        def health_check() -> HealthResponse:
            """Health check endpoint."""
            return HealthResponse(status="ok")

        # ----- Dashboard endpoints (consumed by the bundled /ui) -----------

        @app.get("/workflows", dependencies=[Depends(_verify_orchestrator_key)])
        def list_workflows() -> dict:
            """List all known workflows with their status (newest first)."""
            ids = self._redis.smembers(_WORKFLOWS_KEY) or set()
            items = []
            for wid in ids:
                wid = wid.decode() if isinstance(wid, bytes) else wid
                try:
                    state = self._engine.get_workflow_status(wid)
                except WorkflowNotFoundError:
                    self._redis.srem(_WORKFLOWS_KEY, wid)
                    continue
                name = None
                raw = self._redis.get(f"blueprint:{wid}")
                if raw:
                    try:
                        name = json.loads(raw).get("name")
                    except (ValueError, TypeError):
                        name = None
                items.append({
                    "workflow_id": wid,
                    "name": name,
                    "status": state.status.value,
                    "created_at": state.created_at.isoformat(),
                    "updated_at": state.updated_at.isoformat(),
                    "error": state.error,
                })
            items.sort(key=lambda i: i["created_at"], reverse=True)
            return {"workflows": items, "total": len(items)}

        @app.get(
            "/workflows/{workflow_id}/graph",
            responses={404: {"model": ErrorResponse}},
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_workflow_graph(workflow_id: str) -> dict:
            """Reconstruct the workflow graph (nodes + edges) with live status."""
            try:
                state = self._engine.get_workflow_status(workflow_id)
            except WorkflowNotFoundError:
                raise HTTPException(status_code=404, detail="Workflow not found")

            raw = self._redis.get(f"blueprint:{workflow_id}")
            if not raw:
                raise HTTPException(status_code=404, detail="Blueprint not available")
            blueprint = json.loads(raw)

            # Map node_key -> status from the workflow's tasks.
            status_by_key: dict[str, str] = {}
            try:
                for task in self._engine.get_all_tasks(workflow_id):
                    status_by_key[task.node_key] = task.status.value
            except WorkflowNotFoundError:
                pass

            nodes, edges = [], []
            for node in blueprint.get("nodes", []):
                container = node.get("container_name")
                node_type = node.get("node_type", "MLModel")
                op_list = node.get("operation_signature_list", []) or []
                if not op_list:
                    key = container
                    nodes.append({
                        "key": key, "container_name": container,
                        "operation": None, "node_type": node_type,
                        "status": status_by_key.get(key, "pending"),
                    })
                    continue
                for op in op_list:
                    op_name = (op.get("operation_signature") or {}).get("operation_name")
                    key = f"{container}:{op_name}"
                    nodes.append({
                        "key": key, "container_name": container,
                        "operation": op_name, "node_type": node_type,
                        "status": status_by_key.get(key, "pending"),
                    })
                    for conn in op.get("connected_to", []) or []:
                        to_op = (conn.get("operation_signature") or {}).get("operation_name")
                        edges.append({"from": key, "to": f"{conn.get('container_name')}:{to_op}"})

            return {
                "workflow_id": workflow_id,
                "name": blueprint.get("name"),
                "status": state.status.value,
                "nodes": nodes,
                "edges": edges,
            }

        @app.get(
            "/workflows/{workflow_id}/logs",
            response_class=PlainTextResponse,
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_workflow_logs(workflow_id: str, lines: int = 200) -> str:
            """Return recent log lines mentioning this workflow.

            Merges the API and worker log files (they share a logs volume), so
            task-execution detail from the workers is included. Falls back to
            the unfiltered tail if nothing mentions the id.
            """
            log_dir = None
            for candidate in (os.environ.get("LOG_DIR"), "logs", "/app/src/logs"):
                if candidate and Path(candidate).is_dir():
                    log_dir = Path(candidate)
                    break
            if log_dir is None:
                return "(orchestrator log directory not found)"

            collected: list[str] = []
            for f in sorted(log_dir.glob("*.log")):
                try:
                    collected.extend(f.read_text(errors="replace").splitlines())
                except OSError:
                    continue
            # Lines are prefixed with an ISO-ish timestamp, so a lexicographic
            # sort orders them chronologically across files.
            collected.sort()

            matching = [ln for ln in collected if workflow_id in ln]
            chosen = matching[-lines:] if matching else collected[-lines:]
            header = "" if matching else "(no lines mention this workflow; showing recent log)\n"
            return header + "\n".join(chosen)

        @app.get(
            "/workflows/{workflow_id}/tasks/{task_id}/output-data",
            dependencies=[Depends(_verify_orchestrator_key)],
        )
        def get_task_output_data(workflow_id: str, task_id: str):
            """Resolve and return a task's output data (local convenience).

            inline -> decoded; http/https -> fetched by the orchestrator (which
            can reach services even when the browser cannot); other protocols
            are described but not fetched.
            """
            try:
                task = self._engine._state_store.get_task(workflow_id, task_id)
            except (WorkflowNotFoundError, TaskNotFoundError):
                raise HTTPException(status_code=404, detail="Task not found")

            if not task.output_refs:
                raise HTTPException(status_code=404, detail="Task has no output")

            ref = task.output_refs[0]
            protocol = ref.protocol.value if hasattr(ref.protocol, "value") else str(ref.protocol)
            fmt = ref.format if isinstance(ref.format, str) else getattr(ref.format, "value", "binary")
            media = {"json": "application/json", "csv": "text/csv", "text": "text/plain"}.get(fmt, "text/plain")

            if protocol == "inline":
                try:
                    data = base64.b64decode(ref.uri)
                except Exception:
                    data = ref.uri.encode()
                return Response(content=data, media_type=media)

            if protocol in ("http", "https"):
                headers = {}
                services_key = self._redis.get(f"services_key:{workflow_id}")
                if services_key:
                    services_key = services_key.decode() if isinstance(services_key, bytes) else services_key
                    headers["Authorization"] = f"Bearer {services_key}"
                try:
                    with httpx.Client(timeout=30.0) as client:
                        r = client.get(ref.uri, headers=headers)
                        r.raise_for_status()
                        return Response(content=r.content, media_type=media)
                except httpx.HTTPError as e:
                    raise HTTPException(status_code=502, detail=f"Could not fetch data: {e}")

            return {"protocol": protocol, "uri": ref.uri, "format": fmt,
                    "note": "Data is not inline or HTTP; open it with the appropriate client."}

        # ----- Solution views: a controller publishes a logical graph + live
        # state, and the dashboard renders it. This lets human-in-the-loop /
        # multi-stage controllers show a full graph (with paused/blocked nodes)
        # in the orchestrator UI without writing any graph-rendering code.

        @app.put("/solutions/{view_id}", dependencies=[Depends(_verify_orchestrator_key)])
        def put_solution_view(view_id: str, body: dict) -> dict:
            """Register/replace a logical graph. body: {name, nodes[], edges[]}."""
            self._redis.set(f"solutionview:{view_id}", json.dumps({
                "name": body.get("name", view_id),
                "nodes": body.get("nodes", []),
                "edges": body.get("edges", []),
            }))
            self._redis.sadd("solutionviews", view_id)
            return {"ok": True}

        @app.put("/solutions/{view_id}/state", dependencies=[Depends(_verify_orchestrator_key)])
        def put_solution_state(view_id: str, body: dict) -> dict:
            """Update live state. body: {statuses{key:status}, stage?, message?, detail?}."""
            if not self._redis.exists(f"solutionview:{view_id}"):
                raise HTTPException(status_code=404, detail="Solution view not found")
            from datetime import datetime, timezone
            body = dict(body)
            body["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._redis.set(f"solutionview:{view_id}:state", json.dumps(body))
            return {"ok": True}

        @app.get("/solutions", dependencies=[Depends(_verify_orchestrator_key)])
        def list_solution_views() -> dict:
            ids = self._redis.smembers("solutionviews") or set()
            items = []
            for vid in ids:
                vid = vid.decode() if isinstance(vid, bytes) else vid
                graw = self._redis.get(f"solutionview:{vid}")
                if not graw:
                    self._redis.srem("solutionviews", vid)
                    continue
                g = json.loads(graw)
                sraw = self._redis.get(f"solutionview:{vid}:state")
                st = json.loads(sraw) if sraw else {}
                items.append({"view_id": vid, "name": g.get("name"),
                              "stage": st.get("stage"), "updated_at": st.get("updated_at")})
            items.sort(key=lambda i: i.get("updated_at") or "", reverse=True)
            return {"solutions": items}

        @app.get("/solutions/{view_id}", dependencies=[Depends(_verify_orchestrator_key)])
        def get_solution_view(view_id: str) -> dict:
            graw = self._redis.get(f"solutionview:{view_id}")
            if not graw:
                raise HTTPException(status_code=404, detail="Solution view not found")
            g = json.loads(graw)
            sraw = self._redis.get(f"solutionview:{view_id}:state")
            st = json.loads(sraw) if sraw else {}
            return {"view_id": view_id, "name": g.get("name"),
                    "nodes": g.get("nodes", []), "edges": g.get("edges", []), "state": st}

        @app.delete("/solutions/{view_id}", dependencies=[Depends(_verify_orchestrator_key)])
        def delete_solution_view(view_id: str) -> dict:
            self._redis.delete(f"solutionview:{view_id}", f"solutionview:{view_id}:state")
            self._redis.srem("solutionviews", view_id)
            return {"ok": True}

        # ----- Serve the bundled dashboard UI ------------------------------
        ui_dir = Path(__file__).resolve().parent.parent / "ui"
        if ui_dir.exists():
            app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

        return app
