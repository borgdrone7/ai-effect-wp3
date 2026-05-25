"""Node C1 (below branch, step 1): normalize A's series.

Uses the *concurrent* control-interface pattern with a deliberate ~30s delay,
so the 'running' state is clearly visible in the UI and the orchestrator polls
/control/status while the node works.
"""

import base64
import json
import logging
import sys
import time

from common.concurrent import (
    ExecuteRequest, ExecuteResponse, run, run_in_background, task_manager,
)

logger = logging.getLogger(__name__)
DELAY_SECONDS = 30


def _inputs(inputs: list[dict]) -> list[dict]:
    out = []
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            out.append(json.loads(base64.b64decode(inp.get("uri", "")).decode()))
    return out


def _work(task_id, request, tm):
    series, factor = [], 1.0
    for d in _inputs(request.inputs):
        if d.get("kind") == "root":
            series = d.get("series", [])
            factor = d.get("factor", 1.0)
    # Deliberate slow work so the running state is observable.
    for i in range(DELAY_SECONDS):
        time.sleep(1)
        tm.update_progress(task_id, int((i + 1) / DELAY_SECONDS * 100))
    avg = round(sum(series) / len(series), 2) if series else 0
    res = {"kind": "normalized", "series": series, "avg": avg, "factor": factor}
    tm.complete_task(task_id, {
        "protocol": "inline",
        "uri": base64.b64encode(json.dumps(res).encode()).decode(),
        "format": "json",
    })
    logger.info(f"Normalize: task {task_id} complete (avg={avg})")


def execute_Normalize(request: ExecuteRequest) -> ExecuteResponse:
    task_manager.register_task(request.task_id, request)
    run_in_background(request.task_id, _work, request)
    logger.info(f"Normalize: started async task {request.task_id} (~{DELAY_SECONDS}s)")
    return ExecuteResponse(status="running", task_id=request.task_id)


if __name__ == "__main__":
    run(sys.modules[__name__])
