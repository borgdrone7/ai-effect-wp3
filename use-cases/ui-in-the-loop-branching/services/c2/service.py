"""Node C2 (below branch, step 2): assess the peak. This is the decision point.

The flow pauses here for human input. C2's output is shown in the UI and is
carried either to C3 (continue) or back to A (loop-back).
"""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)
THRESHOLD = 250


def _inputs(inputs: list[dict]) -> list[dict]:
    out = []
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            out.append(json.loads(base64.b64decode(inp.get("uri", "")).decode()))
    return out


def execute_Assess(request: ExecuteRequest) -> ExecuteResponse:
    series, factor = [], 1.0
    for d in _inputs(request.inputs):
        if d.get("kind") == "normalized":
            series = d.get("series", [])
            factor = d.get("factor", 1.0)
    peak = max(series) if series else 0
    over = peak > THRESHOLD
    res = {
        "kind": "assessment", "peak": peak, "factor": factor,
        "threshold": THRESHOLD, "over_threshold": over,
        "message": (f"Peak {peak} exceeds the {THRESHOLD} threshold."
                    if over else f"Peak {peak} is within the {THRESHOLD} threshold."),
    }
    logger.info(f"Assess: peak={peak} over_threshold={over}")
    return ExecuteResponse(status="complete", output=DataReference(
        protocol="inline", uri=base64.b64encode(json.dumps(res).encode()).decode(), format="json"))


if __name__ == "__main__":
    run(sys.modules[__name__])
