"""One command for the whole eval: fresh ground truth, grader self-check, both arms, then the report.

    uv run --group eval python -m eval.run --pilot                 # 3 questions x 2 arms, to measure cost first
    uv run --group eval python -m eval.run                         # all 30 questions, arms permits + baseline
    uv run --group eval python -m eval.run --arms permits,baseline,baseline_ids --reps 3

Needs ANTHROPIC_API_KEY (e.g. `uv run --env-file .env ...`), and for the baseline arms a one-time
`npm ci --ignore-scripts` in eval/baseline. Every other harness flag (--questions, --effort, --concurrency,
--run-id) is passed through.
"""

from __future__ import annotations

import sys

from . import grade, ground_truth, harness


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return harness.main(["--help"])
    selection: list[str] = []
    if "--pilot" in argv:
        selection = ["--questions", ",".join(harness.PILOT)]
    elif "--questions" in argv:
        selection = ["--questions", argv[argv.index("--questions") + 1]]
    resuming = "--run-id" in argv and (harness.RUNS_DIR / argv[argv.index("--run-id") + 1] / "meta.json").exists()
    if not resuming and ground_truth.main(selection) != 0:
        print("Ground truth failed; not running the eval against a broken answer key.", file=sys.stderr)
        return 1
    if grade.main(["--selfcheck"]) != 0:
        return 1
    status = harness.main(argv)
    if status != 0:
        return status
    run_id = argv[argv.index("--run-id") + 1] if "--run-id" in argv else None
    return grade.main([run_id] if run_id else [])


if __name__ == "__main__":
    sys.exit(main())
