"""One bounded analysis process. All inputs come from the local API service."""

import os
import sys
from pathlib import Path
from time import process_time

from f1ri.api.contracts import AnalysisRequest
from f1ri.api.research import ResearchError, run_analysis
from f1ri.decision_board import canonical

MAX_RESULT_BYTES = 3_000_000


def main(root, directory):
    start = process_time()
    try:
        request = AnalysisRequest.model_validate_json((directory / "request.json").read_bytes())
        result = run_analysis(root, request)
        data = {"result": result.model_dump(mode="json"), "cpu_s": process_time()-start}
        raw = canonical(data)
        if len(raw) > MAX_RESULT_BYTES:
            raise ResearchError("RESULT_LIMIT", "The analysis exceeds the local result size limit.")
    except ResearchError as error:
        raw = canonical({"code": error.code, "message": str(error), "cpu_s": process_time()-start})
    except Exception:  # noqa: BLE001 -- Worker failures have a stable public error envelope.
        # Keep paths, environment details and stack traces out of responses.
        raw = canonical({"code": "EXECUTION_FAILED", "message": "The research engine could not complete this run.",
                         "cpu_s": process_time()-start})
    pending = directory / "output.tmp"
    pending.write_bytes(raw)
    os.replace(pending, directory / "output.json")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
