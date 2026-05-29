"""Branching UI-in-the-loop controller.

Drives a branching human-in-the-loop flow on the DAG orchestrator. The logical
graph is:

        A
       / \\
      B   C1
      |    |
      |   C2   (pause: human decides)
      |    |
       \\  /
        C3    (join: B + C2 + human)

The orchestrator has no in-DAG pause or loop, so the flow is run as two DAG
stages glued here:

  Stage 1 (pre-UI):  A -> {B, C1 -> C2}      (one workflow)
  human decides at C2:
     back     -> re-run Stage 1, feeding C2's output back into A
     continue -> Stage 2: C3, fed B's output + C2's output + the human input

The UI always shows the full five-node graph; C3 stays visible (blocked) during
Stage 1, and C2 is shown as "awaiting" so the pause is visible.
"""

import base64
import json
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from common.controller import (
    OrchestratorClient, inline, decode,
    blueprint_node, make_blueprint, make_dockerinfo, node_statuses, node_output,
)
from pathlib import Path

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://host.docker.internal:18000").rstrip("/")
ORCHESTRATOR_API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")

app = FastAPI(title="Branching UI-in-the-loop Controller", version="1.0.0")


# ---- full logical graph (for the UI) -------------------------------------
FULL_GRAPH = {
    "nodes": [
        {"key": "a",  "label": "A · Root",      "op": "Root"},
        {"key": "b",  "label": "B · Headroom",  "op": "Headroom"},
        {"key": "c1", "label": "C1 · Normalize", "op": "Normalize"},
        {"key": "c2", "label": "C2 · Assess",   "op": "Assess"},
        {"key": "c3", "label": "C3 · Finalize", "op": "Finalize"},
    ],
    "edges": [
        {"from": "a", "to": "b"},
        {"from": "a", "to": "c1"},
        {"from": "c1", "to": "c2"},
        {"from": "c2", "to": "c3"},
        {"from": "b", "to": "c3"},
    ],
}

NODE_KEY = {"a": "a:Root", "b": "b:Headroom", "c1": "c1:Normalize", "c2": "c2:Assess", "c3": "c3:Finalize"}


STAGE1_BLUEPRINT = make_blueprint("Branching HITL - Stage 1", [
    blueprint_node("a", "Root", "RootRequest", "RootResponse",
                   [("b", "Headroom"), ("c1", "Normalize")], "DataSource"),
    blueprint_node("b", "Headroom", "HeadroomRequest", "HeadroomResponse", [], "DataSink"),
    blueprint_node("c1", "Normalize", "NormalizeRequest", "NormalizeResponse",
                   [("c2", "Assess")], "MLModel"),
    blueprint_node("c2", "Assess", "AssessRequest", "AssessResponse", [], "DataSink"),
], pipeline_id="uitl-branching-s1")

STAGE2_BLUEPRINT = make_blueprint("Branching HITL - Stage 2", [
    blueprint_node("c3", "Finalize", "FinalizeRequest", "FinalizeResponse", [], "DataSink"),
], pipeline_id="uitl-branching-s2")

DOCKERINFO = make_dockerinfo([(cn, cn, 8080) for cn in ("a", "b", "c1", "c2", "c3")])


# ---- controller state (single-session, in memory) ------------------------
STATE = {
    "stage": "idle",        # idle | stage1 | awaiting | stage2 | done
    "stage1_wf": None, "stage2_wf": None,
    "b_output": None, "c2_output": None, "final": None,
    "factor": 1.0, "iteration": 0, "history": [],
}


VIEW_ID = "uitl-branching"
STAGE_MSG = {
    "idle": "Set a factor and start.",
    "stage1": "Stage 1 running: A fans out to B (above) and C1 -> C2 (below).",
    "awaiting": "Paused at C2 — held here awaiting a human decision. C3 is blocked.",
    "stage2": "Stage 2: C3 joins B and C2.",
    "done": "Finished.",
}


# Shared orchestrator client + inline helpers (see common/controller.py).
_oc = OrchestratorClient(base_url=ORCHESTRATOR_URL, api_key=ORCHESTRATOR_API_KEY)
_inline = inline
_decode = decode


def _publish_graph():
    nodes = [{"key": n["key"], "label": n["label"]} for n in FULL_GRAPH["nodes"]]
    _oc.publish_graph(VIEW_ID, "Branching UI in the Loop", nodes, FULL_GRAPH["edges"])


def _publish_state(statuses, stage, detail=None):
    _oc.publish_state(VIEW_ID, statuses, stage=stage, message=STAGE_MSG.get(stage, ""), detail=detail)


def _submit(blueprint, inputs):
    return _oc.submit(blueprint, DOCKERINFO, inputs)


class StartReq(BaseModel):
    factor: float = 1.0


class DecideReq(BaseModel):
    action: str            # "continue" | "back"
    factor: float | None = None
    note: str = ""


@app.post("/api/start")
def start(req: StartReq):
    STATE.update({"stage": "stage1", "stage2_wf": None, "b_output": None,
                  "c2_output": None, "final": None, "factor": req.factor,
                  "iteration": 1, "history": []})
    STATE["stage1_wf"] = _submit(STAGE1_BLUEPRINT, [_inline({"factor": req.factor, "kind": "init"})])
    _publish_graph()
    _publish_state({k: "pending" for k in NODE_KEY}, "stage1")
    return {"ok": True}


@app.post("/api/decide")
def decide(req: DecideReq):
    if STATE["stage"] != "awaiting":
        raise HTTPException(409, "Not waiting for a decision")

    if req.action == "continue":
        inputs = [_inline(STATE["b_output"]), _inline(STATE["c2_output"]),
                  _inline({"kind": "human", "accept": True, "note": req.note})]
        STATE["history"].append({"iteration": STATE["iteration"],
                                 "peak": STATE["c2_output"].get("peak"),
                                 "factor": STATE["c2_output"].get("factor"), "decision": "continue"})
        STATE["stage2_wf"] = _submit(STAGE2_BLUEPRINT, inputs)
        STATE["stage"] = "stage2"
        return {"ok": True}

    if req.action == "back":
        STATE["history"].append({"iteration": STATE["iteration"],
                                 "peak": STATE["c2_output"].get("peak"),
                                 "factor": STATE["c2_output"].get("factor"), "decision": "back"})
        # C2's output is fed back into A; an optional new factor overrides it.
        a_input = dict(STATE["c2_output"])
        a_input["kind"] = "loopback"
        if req.factor is not None:
            a_input["factor"] = req.factor
        STATE["iteration"] += 1
        STATE.update({"stage": "stage1", "b_output": None, "c2_output": None})
        STATE["stage1_wf"] = _submit(STAGE1_BLUEPRINT, [_inline(a_input)])
        return {"ok": True}

    raise HTTPException(400, "action must be 'continue' or 'back'")


def _status_map():
    """Return {logical_key: status} for the full graph, given the current stage."""
    base = {k: "pending" for k in NODE_KEY}
    stage = STATE["stage"]

    if stage in ("stage1", "awaiting"):
        if stage == "stage1":
            wstatus, tasks = _oc.workflow(STATE["stage1_wf"])
            base.update(node_statuses(tasks, {k: NODE_KEY[k] for k in ("a", "b", "c1", "c2")}))
            base["c3"] = "blocked"
            if wstatus == "completed":
                # capture outputs and move to the pause
                STATE["b_output"] = node_output(tasks, NODE_KEY["b"])
                STATE["c2_output"] = node_output(tasks, NODE_KEY["c2"])
                STATE["stage"] = "awaiting"
            elif wstatus == "failed":
                base = {**base, "_error": "Stage 1 failed"}
        if STATE["stage"] == "awaiting":
            base.update({"a": "completed", "b": "completed", "c1": "completed",
                         "c2": "awaiting", "c3": "blocked"})

    elif stage == "stage2":
        base.update({"a": "completed", "b": "completed", "c1": "completed", "c2": "completed"})
        wstatus, tasks = _oc.workflow(STATE["stage2_wf"])
        base["c3"] = (tasks.get(NODE_KEY["c3"]) or {}).get("status", "running")
        if wstatus == "completed":
            STATE["final"] = node_output(tasks, NODE_KEY["c3"])
            STATE["stage"] = "done"
            base["c3"] = "completed"

    elif stage == "done":
        base = {k: "completed" for k in NODE_KEY}

    return base


@app.get("/api/state")
def state():
    statuses = _status_map()
    # Publish live state to the orchestrator so the dashboard can render the
    # full graph (with paused/blocked nodes) without any graph code here.
    detail = (STATE["c2_output"] if STATE["stage"] == "awaiting"
              else STATE["final"] if STATE["stage"] == "done" else None)
    _publish_state(statuses, STATE["stage"], detail)
    return {
        "stage": STATE["stage"], "iteration": STATE["iteration"], "factor": STATE["factor"],
        "statuses": {k: v for k, v in statuses.items() if not k.startswith("_")},
        "c2_output": STATE["c2_output"] if STATE["stage"] == "awaiting" else None,
        "b_output": STATE["b_output"] if STATE["stage"] in ("awaiting", "stage2", "done") else None,
        "final": STATE["final"] if STATE["stage"] == "done" else None,
        "history": STATE["history"],
        "graph": FULL_GRAPH,
    }


@app.get("/api/graph")
def graph():
    return FULL_GRAPH


@app.get("/health")
def health():
    return {"status": "ok", "orchestrator_url": ORCHESTRATOR_URL}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


# The page HTML lives next to this file and is loaded from disk.
INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_text(encoding="utf-8")
