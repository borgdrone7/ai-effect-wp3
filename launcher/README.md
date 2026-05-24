# AI-EFFECT Launcher

A small local web app for running a solution without the command line:

1. **Upload** a solution zip (the same blueprint.json + dockerinfo.json bundle
   you would import into the portal).
2. **See readiness** of each service. The launcher joins the
   `ai-effect-services` network, so it reaches services by their docker names
   and pings `/health` (falling back to a TCP connect for services without an
   HTTP health endpoint).
3. **Start workflow** with one click. It submits the blueprint to the local
   orchestrator and shows the per-node status live.

This is Phase 1: it assumes the services are **already running** (started from a
registry image or by hand) and the orchestrator is up. It only talks to the
orchestrator's existing API and needs no changes to it.

## Run

```bash
# Orchestrator must be running first (see ../orchestrator), and the
# ai-effect-services network must exist (the orchestrator/use-case scripts
# create it; otherwise: docker network create ai-effect-services).

cd launcher
docker compose up -d --build
```

Open http://localhost:18500

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `ORCHESTRATOR_URL` | `http://host.docker.internal:18000` | Where the orchestrator API is. The API is reached on the host, not on the services network. |
| `ORCHESTRATOR_API_KEY` | _(empty)_ | Bearer token, only if the orchestrator requires one. |

## Notes

- Readiness "down" means the service is not running or not attached to the
  `ai-effect-services` network. "up" means reachable but no HTTP `/health`;
  "healthy" means `/health` answered.
- For full logs and per-task output data, the run view links to the orchestrator
  dashboard (`/ui`).
- Starting services from their images (for solutions whose containers are not
  yet running) is a separate, later capability and is intentionally not in this
  app, which needs no Docker access.
