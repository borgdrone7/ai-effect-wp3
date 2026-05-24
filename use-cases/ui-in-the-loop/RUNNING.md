# Running UI in the Loop

## Prerequisites

- Docker and Docker Compose
- The orchestrator running (see `orchestrator/`), reachable on host port 18000
- The shared network `ai-effect-services` (the orchestrator and start scripts
  create it; otherwise `docker network create ai-effect-services`)

## Step 1: Start the orchestrator

```bash
cd orchestrator
docker compose up -d
curl -s http://localhost:18000/health
```

## Step 2: Start this use case

```bash
cd use-cases/ui-in-the-loop
docker compose up -d --build
```

This starts `processor` (18091), `summarizer` (18092), and `controller` (18090),
all on the `ai-effect-services` network. The controller reaches the orchestrator
API on the host via `host.docker.internal:18000`.

## Step 3: Use the UI

Open http://localhost:18090, pick a scenario and a factor, press **Run**. The
controller submits a workflow, waits for it, and shows the summary and
recommendation. Change the inputs and run again.

You can also drive it directly:

```bash
curl -s -X POST http://localhost:18090/run \
  -H "Content-Type: application/json" \
  -d '{"scenario":"heat wave","factor":1.4}' | jq .
```

## How it works

Each `POST /run` builds a small two-node blueprint (`processor` ->
`summarizer`), submits it to the orchestrator with the user's choice as an
inline-JSON input, polls `GET /workflows/{id}` until completion, then reads the
`summarizer` task's inline output from `GET /workflows/{id}/tasks`. There is no
cycle and no special "wait for user" step; the next iteration is just the next
submission.

## Cleanup

```bash
docker compose down
```

## Notes

- If the orchestrator requires a bearer token, set `ORCHESTRATOR_API_KEY` in the
  environment before `docker compose up` (it is passed to the controller).
- If your orchestrator is not on the host's port 18000, set `ORCHESTRATOR_URL`
  (e.g. `ORCHESTRATOR_URL=http://host.docker.internal:18000`).
