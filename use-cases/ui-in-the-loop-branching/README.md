# Branching UI in the Loop

A richer human-in-the-loop proof of concept: a **branching** pipeline that
**pauses at a UI node**, where the human decides to **loop back** or **continue**,
and a **join** that combines both branches.

## Logical graph

```
        A  (root: demand series scaled by a factor)
       / \
      B   C1
      |    |
      |   C2   *** pause: human decides ***
      |    |
       \  /
        C3   (join: B + C2 + human note)
```

- **A** fans out to the **above branch** `B` (capacity headroom) and the
  **below branch** `C1 -> C2 -> C3`.
- The flow **pauses at C2** (peak assessment). The human sees the result and:
  - **Back to start** -> re-run from A, feeding C2's output (and an optional new
    factor) back into A; or
  - **Continue** -> run **C3**, which joins B's headroom, C2's assessment, and
    the human's note into a final result.

## How it maps to the orchestrator (no in-DAG pause/loop)

The orchestrator runs plain DAGs, so the controller runs the flow as two stages:

- **Stage 1:** `A -> {B, C1 -> C2}` (one workflow). On completion the controller
  has B's and C2's outputs and shows the UI.
- **Human decision:** *back* re-submits Stage 1; *continue* submits **Stage 2**
  (`C3`) with B's output + C2's output + the human input.

The UI always shows the **full five-node graph**: during Stage 1, `C3` is shown
as **blocked**; at the pause, `C2` is shown as **awaiting** so you can see the
flow is held there. C2's output is carried to C3 (continue) or back to A (loop).

## Components

| Component | Role | Port |
|-----------|------|------|
| controller | Full-graph UI + two-stage runner | 18510 |
| a, b, c1, c2, c3 | Pipeline nodes (control-interface services) | 18511-18515 |

## Running

See [RUNNING.md](RUNNING.md). Open the controller UI at http://localhost:18510.
