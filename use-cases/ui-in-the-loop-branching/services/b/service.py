"""Node B (above branch): compute capacity headroom from A's series.

Its output is carried to the join at C3.
"""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)
CAPACITY = 300


def _inputs(inputs: list[dict]) -> list[dict]:
    out = []
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            out.append(json.loads(base64.b64decode(inp.get("uri", "")).decode()))
    return out


def execute_Headroom(request: ExecuteRequest) -> ExecuteResponse:
    series = []
    for d in _inputs(request.inputs):
        if d.get("kind") == "root":
            series = d.get("series", [])
    peak = max(series) if series else 0
    res = {"kind": "headroom", "capacity": CAPACITY, "peak": peak,
           "headroom": round(CAPACITY - peak, 2)}
    logger.info(f"Headroom: peak={peak} headroom={res['headroom']}")
    return ExecuteResponse(status="complete", output=DataReference(
        protocol="inline", uri=base64.b64encode(json.dumps(res).encode()).decode(), format="json"))


if __name__ == "__main__":
    run(sys.modules[__name__])
