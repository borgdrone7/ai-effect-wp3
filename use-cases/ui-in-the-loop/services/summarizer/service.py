"""Summarizer service - turns demand metrics into a readable summary.

Part of the ui-in-the-loop demo. Receives the processor's metrics as inline
JSON and returns a short human-readable summary plus a recommendation, again
as inline JSON, which the controller UI shows to the user.
"""

import base64
import json
import logging
import sys

from common.sequential import DataReference, ExecuteRequest, ExecuteResponse, run

logger = logging.getLogger(__name__)


def _decode_inline_input(inputs: list[dict]) -> dict:
    for inp in inputs or []:
        if inp.get("protocol") == "inline" and inp.get("format") == "json":
            return json.loads(base64.b64decode(inp.get("uri", "")).decode())
    return {}


def execute_Summarize(request: ExecuteRequest) -> ExecuteResponse:
    """Format metrics into a summary + a threshold-based recommendation."""
    m = _decode_inline_input(request.inputs)
    scenario = m.get("scenario", "baseline")
    peak = m.get("peak", 0)
    average = m.get("average", 0)
    total = m.get("total", 0)

    if peak > 180:
        recommendation = (
            "Peak demand is high. Consider load shifting or adding capacity."
        )
    elif peak < 110:
        recommendation = "Peak demand is low. There is comfortable headroom."
    else:
        recommendation = (
            "Peak demand is moderate. Current capacity should be adequate."
        )

    summary = (
        f"Scenario '{scenario}': peak {peak}, average {average}, "
        f"total {total} (arbitrary units)."
    )

    logger.info(f"Summarize: scenario={scenario} peak={peak} -> {recommendation}")

    result = {
        "summary": summary,
        "recommendation": recommendation,
        "metrics": {
            "scenario": scenario,
            "peak": peak,
            "average": average,
            "total": total,
        },
    }
    encoded = base64.b64encode(json.dumps(result).encode()).decode()

    return ExecuteResponse(
        status="complete",
        output=DataReference(protocol="inline", uri=encoded, format="json"),
    )


if __name__ == "__main__":
    run(sys.modules[__name__])
