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

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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


def _img(cn):
    return f"uitl-branching-{cn}:latest"


def _node(cn, op, inmsg, outmsg, connected, ntype):
    return {
        "container_name": cn, "proto_uri": f"{cn}.proto", "image": _img(cn), "node_type": ntype,
        "operation_signature_list": [{
            "operation_signature": {"operation_name": op, "input_message_name": inmsg, "output_message_name": outmsg},
            "connected_to": [{"container_name": t[0], "operation_signature": {"operation_name": t[1]}} for t in connected],
        }],
    }


STAGE1_BLUEPRINT = {
    "name": "Branching HITL - Stage 1", "pipeline_id": "uitl-branching-s1",
    "creation_date": "2026-05-01", "type": "pipeline-topology/v2", "version": "2.0",
    "nodes": [
        _node("a", "Root", "RootRequest", "RootResponse", [("b", "Headroom"), ("c1", "Normalize")], "DataSource"),
        _node("b", "Headroom", "HeadroomRequest", "HeadroomResponse", [], "DataSink"),
        _node("c1", "Normalize", "NormalizeRequest", "NormalizeResponse", [("c2", "Assess")], "MLModel"),
        _node("c2", "Assess", "AssessRequest", "AssessResponse", [], "DataSink"),
    ],
}
STAGE2_BLUEPRINT = {
    "name": "Branching HITL - Stage 2", "pipeline_id": "uitl-branching-s2",
    "creation_date": "2026-05-01", "type": "pipeline-topology/v2", "version": "2.0",
    "nodes": [_node("c3", "Finalize", "FinalizeRequest", "FinalizeResponse", [], "DataSink")],
}
DOCKERINFO = {"docker_info_list": [
    {"container_name": cn, "ip_address": cn, "port": "8080"} for cn in ("a", "b", "c1", "c2", "c3")
]}


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


def _headers():
    return {"Authorization": f"Bearer {ORCHESTRATOR_API_KEY}"} if ORCHESTRATOR_API_KEY else {}


def _publish_graph():
    """Register the full logical graph with the orchestrator (drawn by the dashboard)."""
    nodes = [{"key": n["key"], "label": n["label"]} for n in FULL_GRAPH["nodes"]]
    try:
        with httpx.Client(timeout=10.0) as c:
            c.put(f"{ORCHESTRATOR_URL}/solutions/{VIEW_ID}",
                  json={"name": "Branching UI in the Loop", "nodes": nodes, "edges": FULL_GRAPH["edges"]},
                  headers=_headers())
    except httpx.HTTPError:
        pass


def _publish_state(statuses, stage, detail=None):
    try:
        with httpx.Client(timeout=10.0) as c:
            c.put(f"{ORCHESTRATOR_URL}/solutions/{VIEW_ID}/state",
                  json={"statuses": statuses, "stage": stage,
                        "message": STAGE_MSG.get(stage, ""), "detail": detail},
                  headers=_headers())
    except httpx.HTTPError:
        pass


def _inline(obj):
    return {"protocol": "inline", "uri": base64.b64encode(json.dumps(obj).encode()).decode(), "format": "json"}


def _decode(ref):
    if ref and ref.get("protocol") == "inline":
        return json.loads(base64.b64decode(ref.get("uri", "")).decode())
    return {}


def _submit(blueprint, inputs):
    with httpx.Client(timeout=30.0) as c:
        r = c.post(f"{ORCHESTRATOR_URL}/workflows",
                   json={"blueprint": blueprint, "dockerinfo": DOCKERINFO, "inputs": inputs},
                   headers=_headers())
    if r.status_code != 200:
        raise HTTPException(502, f"Orchestrator rejected submit ({r.status_code}): {r.text}")
    return r.json().get("workflow_id")


def _wf(workflow_id):
    with httpx.Client(timeout=15.0) as c:
        s = c.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}", headers=_headers()).json()
        t = c.get(f"{ORCHESTRATOR_URL}/workflows/{workflow_id}/tasks", headers=_headers()).json()
    return s.get("status"), {task["node_key"]: task for task in t.get("tasks", [])}


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
            wstatus, tasks = _wf(STATE["stage1_wf"])
            for k in ("a", "b", "c1", "c2"):
                base[k] = tasks.get(NODE_KEY[k], {}).get("status", "pending")
            base["c3"] = "blocked"
            if wstatus == "completed":
                # capture outputs and move to the pause
                STATE["b_output"] = _decode((tasks.get(NODE_KEY["b"], {}).get("output_refs") or [None])[0])
                STATE["c2_output"] = _decode((tasks.get(NODE_KEY["c2"], {}).get("output_refs") or [None])[0])
                STATE["stage"] = "awaiting"
            elif wstatus == "failed":
                base = {**base, "_error": "Stage 1 failed"}
        if STATE["stage"] == "awaiting":
            base.update({"a": "completed", "b": "completed", "c1": "completed",
                         "c2": "awaiting", "c3": "blocked"})

    elif stage == "stage2":
        base.update({"a": "completed", "b": "completed", "c1": "completed", "c2": "completed"})
        wstatus, tasks = _wf(STATE["stage2_wf"])
        base["c3"] = tasks.get(NODE_KEY["c3"], {}).get("status", "running")
        if wstatus == "completed":
            STATE["final"] = _decode((tasks.get(NODE_KEY["c3"], {}).get("output_refs") or [None])[0])
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


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Branching UI in the Loop</title>
<style>
  :root{--bg:#0f1020;--panel:#1a1b33;--line:#2c2d4a;--txt:#e8e8f2;--muted:#9a9ab5;}
  *{box-sizing:border-box;} body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--txt);}
  header{padding:14px 22px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;}
  header h1{font-size:1.05rem;margin:0;} .muted{color:var(--muted);}
  main{max-width:900px;margin:0 auto;padding:22px;}
  .card{border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px;background:var(--panel);}
  h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:0 0 10px;}
  svg{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;}
  .node text{fill:#fff;font-size:12px;font-weight:600;} .node .st{fill:#e8e8f2;font-size:10px;opacity:.85;}
  .edge{stroke:var(--muted);stroke-width:1.6;fill:none;marker-end:url(#ar);}
  button{background:#3a78ff;border:0;color:#fff;padding:9px 16px;border-radius:7px;font-weight:600;cursor:pointer;}
  button.sec{background:var(--panel);border:1px solid var(--line);color:var(--txt);}
  button:disabled{opacity:.5;cursor:default;}
  input{background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:7px;width:120px;}
  .badge{display:inline-block;padding:2px 9px;border-radius:11px;font-size:.7rem;font-weight:700;color:#fff;}
  table{width:100%;border-collapse:collapse;font-size:.82rem;} td,th{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
</style></head>
<body>
<header><h1>Branching UI in the Loop</h1><span class="sp" style="flex:1"></span><span class="muted" id="orch"></span></header>
<main>
  <div class="card"><h2>Status</h2>
    <div class="muted" id="stagemsg"></div>
    <p style="margin-top:10px"><a href="http://localhost:18000/ui/" target="_blank" rel="noopener">See the live pipeline graph in the orchestrator dashboard &#8599;</a>
    <span class="muted">&nbsp;(Solutions tab &rarr; "Branching UI in the Loop")</span></p></div>

  <div class="card" id="controls"><h2>Control</h2><div id="ctl"></div></div>

  <div class="card"><h2>Iterations</h2>
    <table id="hist"><thead><tr><th>#</th><th>factor</th><th>peak</th><th>decision</th></tr></thead><tbody></tbody></table>
  </div>
</main>
<script>
const COL={pending:'#6b6b85',blocked:'#3a3a55',running:'#3a78ff',awaiting:'#e0a020',completed:'#27ae60',failed:'#e74c3c'};
const col=s=>COL[s]||'#6b6b85'; const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function j(p,o){const r=await fetch(p,o);if(!r.ok)throw new Error(await r.text());return r.json();}

function renderControls(s){
  const ctl=document.getElementById('ctl');
  if(s.stage==='idle'){
    ctl.innerHTML=`<div class="row"><label>Demand factor</label><input id="factor" type="number" step="0.1" min="0.5" max="2.5" value="1.0"/>
      <button id="startBtn">Start</button></div>`;
    document.getElementById('startBtn').onclick=async()=>{await j('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({factor:parseFloat(document.getElementById('factor').value)})});};
  } else if(s.stage==='stage1'){ ctl.innerHTML=`<div class="muted">Running stage 1 (A, B, C1, C2)…</div>`; }
  else if(s.stage==='awaiting'){
    const a=s.c2_output||{};
    ctl.innerHTML=`<div style="margin-bottom:10px"><span class="badge" style="background:${a.over_threshold?'#e74c3c':'#27ae60'}">${a.over_threshold?'over threshold':'within threshold'}</span>
      &nbsp; ${esc(a.message||'')} (factor ${esc(a.factor)})</div>
      <div class="row">
        <button id="contBtn">Continue → finalize (C3)</button>
        <input id="note" placeholder="note (optional)" style="width:200px"/>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="sec" id="backBtn">Back to start (A) with factor</button>
        <input id="newfactor" type="number" step="0.1" min="0.5" max="2.5" value="${esc(a.factor)}"/>
      </div>`;
    document.getElementById('contBtn').onclick=async()=>{await j('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'continue',note:document.getElementById('note').value})});};
    document.getElementById('backBtn').onclick=async()=>{await j('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'back',factor:parseFloat(document.getElementById('newfactor').value)})});};
  } else if(s.stage==='stage2'){ ctl.innerHTML=`<div class="muted">Running stage 2 (C3 join)…</div>`; }
  else if(s.stage==='done'){
    const f=s.final||{};
    ctl.innerHTML=`<div style="margin-bottom:10px"><span class="badge" style="background:#27ae60">done</span> ${esc(f.summary||'')}</div>
      <button class="sec" id="againBtn">Run again</button>`;
    document.getElementById('againBtn').onclick=async()=>{await j('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({factor:1.0})});};
  }
}

const STAGEMSG={idle:'Set a factor and press Start.',stage1:'Stage 1 running — A fans out to B (above) and C1→C2 (below).',
  awaiting:'Paused at C2 — the flow is held here awaiting your decision. C3 is blocked until you continue.',
  stage2:'Stage 2 — C3 joins B and C2.',done:'Finished.'};

let last='';
async function tick(){
  try{
    const s=await j('/api/state');
    document.getElementById('stagemsg').textContent=STAGEMSG[s.stage]||'';
    const sig=s.stage+JSON.stringify(s.c2_output)+JSON.stringify(s.final);
    if(sig!==last){renderControls(s);last=sig;}
    document.querySelector('#hist tbody').innerHTML=(s.history||[]).map(h=>
      `<tr><td>${h.iteration}</td><td>${esc(h.factor)}</td><td>${esc(h.peak)}</td><td>${esc(h.decision)}</td></tr>`).join('');
  }catch(e){/* keep polling */}
}
fetch('/health').then(r=>r.json()).then(h=>{document.getElementById('orch').textContent='orchestrator: '+h.orchestrator_url;}).catch(()=>{});
tick(); setInterval(tick,1500);
</script>
</body></html>
"""
