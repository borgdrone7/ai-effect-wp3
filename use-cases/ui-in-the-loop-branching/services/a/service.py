"""Node A (root): produce a base demand series scaled by a factor.

On the first run the factor comes from the controller's initial input; on a
loop-back it comes from C2's previous output (which the controller feeds in).
"""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)
BASE = [100, 120, 95, 140, 160, 130, 110, 90]


def _inputs(inputs: list[dict]) -> list[dict]:
    out = []
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            out.append(json.loads(base64.b64decode(inp.get("uri", "")).decode()))
    return out


def execute_Root(request: ExecuteRequest) -> ExecuteResponse:
    factor = 1.0
    for d in _inputs(request.inputs):
        if "factor" in d:
            try:
                factor = float(d["factor"])
            except (TypeError, ValueError):
                pass
    series = [round(v * factor, 2) for v in BASE]
    res = {"kind": "root", "factor": factor, "series": series}
    logger.info(f"Root: factor={factor} peak={max(series)}")
    return ExecuteResponse(status="complete", output=DataReference(
        protocol="inline", uri=base64.b64encode(json.dumps(res).encode()).decode(), format="json"))


if __name__ == "__main__":
    run(sys.modules[__name__])
