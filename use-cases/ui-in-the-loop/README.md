# UI in the Loop

A proof-of-concept showing the **human-in-the-loop** pattern on top of the
AI-EFFECT orchestrator: a user makes a choice in a web UI, a workflow runs, the
result is shown, and the user can adjust and run again.

The orchestrator executes a directed acyclic graph (no cycles, no pause/resume).
The iteration loop therefore lives in an external **controller**, not in the
orchestrator. Each user action submits a fresh, independent workflow.

```
  Browser            Controller (this use case)        Orchestrator        Services
  -------            --------------------------        ------------        --------
  pick scenario  ->  POST /run                     ->  POST /workflows  ->  processor
                                                                            -> summarizer
  see result     <-  result (summary + rec)        <-  poll status/tasks <-  (inline JSON)
  adjust + run   ->  (next workflow)
```

## Components

| Component | Role | Port (host) |
|-----------|------|-------------|
| `controller` | Serves the web UI and submits one workflow per user action | 18090 |
| `processor` | Pipeline node: scales a base demand series by the chosen factor, derives peak/average/total | 18091 |
| `summarizer` | Pipeline node: turns metrics into a readable summary and a recommendation | 18092 |

`processor` and `summarizer` implement the standard AI-EFFECT control interface
(`common.sequential`) and exchange data as inline JSON. The `controller` is a
plain web app and is intentionally **not** a pipeline node.

## The demo

Pick a scenario and a demand scaling factor (0.5 to 2.0), press Run. The
pipeline computes simple metrics from an illustrative base series (arbitrary
units, deliberately transparent rather than a real grid model) and returns a
one-line summary and a threshold-based recommendation. Change the inputs and run
again to see the loop.

## Web UI link in the portal

`connections.json` attaches a `webui_url` to the `processor` node pointing at
the controller UI (`http://localhost:18090`). When this use case is exported and
imported into the portal, the service shows a **Web UI** link that opens the
controller. This demonstrates the portal's per-service web UI link end to end.

## Running

See [RUNNING.md](RUNNING.md).
