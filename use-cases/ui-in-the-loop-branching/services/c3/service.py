"""Node C3 (join): combine B's headroom (above branch), C2's assessment
(below branch) and the human's note into a final result.

Runs only when the human chooses to continue. Receives B's and C2's outputs
plus the human input as separate inline inputs.
"""

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


def execute_Finalize(request: ExecuteRequest) -> ExecuteResponse:
    by = {}
    for d in _inputs(request.inputs):
        by[d.get("kind")] = d
    headroom = by.get("headroom", {})
    assessment = by.get("assessment", {})
    human = by.get("human", {})

    res = {
        "kind": "final",
        "factor": assessment.get("factor"),
        "peak": assessment.get("peak"),
        "capacity": headroom.get("capacity"),
        "headroom": headroom.get("headroom"),
        "note": human.get("note", ""),
        "summary": (
            f"Accepted at factor {assessment.get('factor')}: peak "
            f"{assessment.get('peak')}, headroom {headroom.get('headroom')} "
            f"of {headroom.get('capacity')}."
            + (f" Note: {human.get('note')}" if human.get("note") else "")
        ),
    }
    logger.info(f"Finalize: {res['summary']}")
    return ExecuteResponse(status="complete", output=DataReference(
        protocol="inline", uri=base64.b64encode(json.dumps(res).encode()).decode(), format="json"))


if __name__ == "__main__":
    run(sys.modules[__name__])
