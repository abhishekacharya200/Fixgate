"""Print the progress of a benchmark run: finished issues, outcomes, agent steps and running issues.

    .venv/bin/python scripts/run_status.py mini50-haiku45
"""

import collections
import json
import sys
from pathlib import Path


def main(run_id: str, runs_dir: str = "runs") -> None:
    run = Path(runs_dir) / run_id
    finished = [json.loads(p.read_text()) for p in sorted((run / "instances").glob("*.json"))]
    total = len(json.loads((run / "run.json").read_text()).get("instances", [])) if (run / "run.json").is_file() else "?"
    outcomes = collections.Counter(f"{r['outcome']['category']}/{r['outcome']['subcategory']}" for r in finished)
    steps = sum(r["usage"].get("steps") or 0 for r in finished)
    api_cost = sum(r["usage"].get("cost_cached_estimate_usd") or 0 for r in finished)
    print(f"{run_id}: {len(finished)} of {total} issues finished")
    for outcome, count in outcomes.most_common():
        print(f"  {count:3d}  {outcome}")
    print(f"agent steps in finished issues: {steps} (mean {steps / len(finished):.1f})" if finished else "no issues finished")
    print(f"estimated API cost so far: ${api_cost:.2f}")
    done = {r["task_id"] for r in finished}
    for trace in sorted((run / "traces").glob("*/trace.jsonl")):
        name = trace.parent.name
        if name not in done and "." not in name:
            count = sum(1 for line in trace.read_text().splitlines() if '"kind": "step"' in line)
            print(f"running: {name} ({count} steps so far)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mini50-haiku45")
