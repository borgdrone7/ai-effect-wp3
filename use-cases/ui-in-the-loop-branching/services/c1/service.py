"""Node C1 (below branch, step 1): normalize A's series."""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)


def _inputs(inputs: list[dict]) -> list[dict]:
    out = []
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            out.append(json.loads(base64.b64decode(inp.get("uri", "")).decode()))
    return out


def execute_Normalize(request: ExecuteRequest) -> ExecuteResponse:
    series, factor = [], 1.0
    for d in _inputs(request.inputs):
        if d.get("kind") == "root":
            series = d.get("series", [])
            factor = d.get("factor", 1.0)
    avg = round(sum(series) / len(series), 2) if series else 0
    res = {"kind": "normalized", "series": series, "avg": avg, "factor": factor}
    logger.info(f"Normalize: avg={avg}")
    return ExecuteResponse(status="complete", output=DataReference(
        protocol="inline", uri=base64.b64encode(json.dumps(res).encode()).decode(), format="json"))


if __name__ == "__main__":
    run(sys.modules[__name__])
