"""One bounded analysis process. All inputs come from the local API service."""

import os
import sys
from pathlib import Path
from threading import Thread
from time import monotonic, process_time, sleep

MAX_RESULT_BYTES = 3_000_000


def main(root, directory):
    from f1ri.api.contracts import AnalysisRequest
    from f1ri.api.research import ResearchError, run_analysis
    from f1ri.api.workspace import AnalysisDetails
    from f1ri.decision_board import canonical

    start = process_time()
    try:
        request = AnalysisRequest.model_validate_json((directory / "request.json").read_bytes())
        details = {}
        result = run_analysis(root, request, diagnostics=details)
        data = {"result": result.model_dump(mode="json"), "cpu_s": process_time()-start}
        if details:
            data["details"] = AnalysisDetails(analysis_id=result.analysis_id, result_sha256=result.result_sha256,
                                               **details).model_dump(mode="json")
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


def _guard(pid, birth, deadline):
    """Fail closed on supervisor loss, including an abrupt API process exit."""
    import psutil

    def monitor():
        while monotonic() < deadline:
            try:
                if psutil.Process(pid).create_time() != birth:
                    os._exit(125)
            except psutil.Error:
                os._exit(125)
            sleep(.10)
        os._exit(124)

    if os.name == "posix":
        import resource

        ceiling = max(1, min(35, int(deadline-monotonic())+1))
        resource.setrlimit(resource.RLIMIT_CPU, (ceiling, ceiling+1))
    Thread(target=monitor, daemon=True, name="analysis-deadline").start()


if __name__ == "__main__":
    if len(sys.argv) == 6:
        _guard(int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]))
    main(Path(sys.argv[1]), Path(sys.argv[2]))
