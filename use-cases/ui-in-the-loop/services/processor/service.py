"""Processor service - computes demand metrics from a user scenario choice.

Part of the ui-in-the-loop demo. Receives the user's choice (scenario + a
demand scaling factor) as inline JSON, applies a transparent, illustrative
computation, and returns the resulting metrics as inline JSON for the
summarizer. The numbers are arbitrary units chosen to make the demo legible,
not a model of any real grid.
"""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)

# Illustrative base demand series (arbitrary units), deliberately simple.
BASE_SERIES = [100, 120, 95, 140, 160, 130, 110, 90]


def _decode_inline_input(inputs: list[dict]) -> dict:
    """Decode the first inline JSON input, if any."""
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            return json.loads(base64.b64decode(inp.get("uri", "")).decode())
    return {}


def execute_Process(request: ExecuteRequest) -> ExecuteResponse:
    """Scale the base demand series by the user's factor and derive metrics."""
    cfg = _decode_inline_input(request.inputs)
    scenario = cfg.get("scenario", "baseline")
    try:
        factor = float(cfg.get("factor", 1.0))
    except (TypeError, ValueError):
        factor = 1.0

    series = [round(v * factor, 2) for v in BASE_SERIES]
    peak = max(series)
    total = round(sum(series), 2)
    average = round(total / len(series), 2)

    logger.info(f"Process: scenario={scenario} factor={factor} peak={peak}")

    result = {
        "scenario": scenario,
        "factor": factor,
        "peak": peak,
        "average": average,
        "total": total,
        "series": series,
    }
    encoded = base64.b64encode(json.dumps(result).encode()).decode()

    return ExecuteResponse(
        status="complete",
        output=DataReference(protocol="inline", uri=encoded, format="json"),
    )


if __name__ == "__main__":
    run(sys.modules[__name__])
