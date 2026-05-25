# Running Branching UI in the Loop

## Prerequisites

- Docker and Docker Compose
- The orchestrator running (see `orchestrator/`), reachable on host port 18000
- The shared network `ai-effect-services` (orchestrator/start scripts create it;
  otherwise `docker network create ai-effect-services`)

## Start

```bash
# orchestrator first
cd orchestrator && docker compose up -d && cd ..

# this use case
cd use-cases/ui-in-the-loop-branching
docker compose up -d --build
```

This starts the five nodes (a, b, c1, c2, c3) and the controller, all on the
`ai-effect-services` network. The controller reaches the orchestrator API on the
host via `host.docker.internal:18000`.

## Use it

Open http://localhost:18510.

1. Set a demand factor and press **Start**. Watch Stage 1 run on the full graph:
   A -> B (above) and A -> C1 -> C2 (below); C3 stays **blocked**.
2. The flow **pauses at C2** (shown as *awaiting*). You see the peak assessment.
3. Choose:
   - **Continue** -> C3 runs and joins B + C2 + your note -> final result.
   - **Back to start** -> set a new factor; the flow loops back to A (C2's output
     is fed back in) and Stage 1 runs again.
4. The Iterations table records each loop (factor, peak, decision).

Try a high factor (e.g. 1.8) to push the peak over the threshold, loop back with
a lower factor, then continue once it is within threshold.

## Cleanup

```bash
docker compose down
```

## Notes

- The controller drives everything; the launcher/orchestrator dashboard would
  show each individual Stage 1 / Stage 2 workflow, while the controller shows the
  unified full graph with the pause.
- `connections.json` describes the full logical graph for export/portal display;
  the controller submits the two stage sub-graphs at runtime.
