"""Repeat analyze with phase timings and pawploy_llm metrics, without remote writes.

Use --root to compare an extracted older revision on identical local snapshots.
AWS_PROFILE must be peony; outputs go to the supplied local JSON file.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--sources", required=True, help="JSON mapping of case names to source URIs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    if os.environ.get("AWS_PROFILE") != "peony":
        ap.error("AWS_PROFILE=peony is required")
    sys.path.insert(0, str(Path(args.root).resolve()))
    import boto3
    if boto3.client("sts", region_name="ap-northeast-2").get_caller_identity()["Account"] != "135808950984":
        raise RuntimeError("Unexpected AWS account")
    from agent import analyze, source
    from agent import brain as brain_module
    from agent.brain import StrandsBrain
    from agent.storage import NullStore
    from strands import Agent

    rows = []
    current = {}
    original_call = Agent.__call__

    def measured_call(self, *a, **kw):
        # Strands metrics accumulate across calls on the same agent (pre-PR9
        # recommendation followed by Dockerfile). Record per-call deltas.
        before = self.event_loop_metrics
        old_cycles = before.cycle_count
        old_usage = dict(before.accumulated_usage)
        old_latency = before.accumulated_metrics.get("latencyMs", 0)
        old_tools = {k: v.call_count for k, v in before.tool_metrics.items()}
        start = time.perf_counter()
        result = original_call(self, *a, **kw)
        m = result.metrics
        u = m.accumulated_usage
        row = {"pawploy_llm": kw.get("structured_output_model").__name__,
               "seconds": round(time.perf_counter() - start, 3), "cycles": m.cycle_count - old_cycles,
               "latency_ms": m.accumulated_metrics.get("latencyMs", 0) - old_latency,
               **{label: u[key] - old_usage.get(key, 0) if key in u else None for label, key in
                  [("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                   ("cache_read", "cacheReadInputTokens"), ("cache_write", "cacheWriteInputTokens")]},
               "tools": {k: v.call_count - old_tools.get(k, 0) for k, v in m.tool_metrics.items()
                         if v.call_count > old_tools.get(k, 0)}}
        current.setdefault("llm", []).append(row)
        print(json.dumps(row), flush=True)
        return result

    Agent.__call__ = measured_call
    # This harness emits the same metrics with per-call deltas, including on
    # older revisions without production logging. Avoid duplicate log entries.
    if hasattr(brain_module, "_log_metrics"):
        brain_module._log_metrics = lambda *a, **kw: None
    for module, name, label in [(source, "load", "load_seconds"),
                                (analyze, "run_scan", "scan_seconds")]:
        original = getattr(module, name)

        def timed(*a, _original=original, _label=label, **kw):
            start = time.perf_counter()
            try:
                return _original(*a, **kw)
            finally:
                current[_label] = round(time.perf_counter() - start, 3)

        setattr(module, name, timed)
    sources = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    for repeat in range(args.repeat):
        for case, uri in sources.items():
            current = {"case": case, "repeat": repeat + 1}
            print(json.dumps({"profile_start": case, "repeat": repeat + 1}), flush=True)
            start = time.perf_counter()
            try:
                result = analyze.run({"project_id": "speed-profile", "source_uri": uri},
                                     StrandsBrain(), NullStore())
                current["recommendation"] = result["recommendation"]
                current["build_files"] = result["build_files"]
            except Exception as exc:
                current["error"] = f"{type(exc).__name__}: {exc}"
            current["total_seconds"] = round(time.perf_counter() - start, 3)
            rows.append(current)
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in current.items() if k not in {"recommendation", "build_files"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
