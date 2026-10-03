"""Read-only Bedrock profiling; artifacts are always stored locally.

AWS_PROFILE=peony python scripts/profile_terraform.py --label before --runs 2
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config, terraform
from agent.brain import StrandsBrain, TF_SYSTEM_PROMPT
from agent.schemas import TfFile
from agent.storage import LocalStore


class ProfileBrain(StrandsBrain):
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def _run(self, agent, prompt, out_model):
        from strands.types.agent import Limits
        start = time.perf_counter()
        result = agent(prompt, structured_output_model=out_model, limits=Limits(turns=config.MAX_TURNS))
        with self.lock:
            self.calls.append({"schema": out_model.__name__, "seconds": time.perf_counter() - start,
                               "prompt_chars": len(prompt), "usage": dict(result.metrics.accumulated_usage)})
        if result.structured_output is None:
            raise RuntimeError("Missing structured output")
        return result.structured_output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--legacy", action="store_true", help="Use full-file LLM generation and repair")
    parser.add_argument("--no-cache", action="store_true", help="Measure patch repair without prefix caching")
    args = parser.parse_args()
    if os.environ.get("AWS_PROFILE") != "peony":
        parser.error("AWS_PROFILE must be peony")
    import boto3
    account = boto3.client("sts", region_name=config.REGION).get_caller_identity()["Account"]
    if account != "135808950984":
        raise RuntimeError(f"Unexpected account: {account}")
    if args.legacy:
        config.TF_USE_REFERENCE = False
        config.TF_PATCH_FIX = False
    if args.no_cache:
        config.TF_CACHE_PROMPT = False
    directory = ROOT / "work" / "terraform-profile" / args.label
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    cases = [("sample", "examples/demo/1-analyze-sample.json", "examples/demo/4-fix-terraform.json"),
             ("judge", "examples/demo/judge/1-analyze.json", "examples/demo/judge/5-fix-terraform.json"),
             ("swa", "examples/demo/2-analyze-swa.json", None)]
    for name, rec_path, fix_path in cases:
        rec = json.loads((ROOT / rec_path).read_text(encoding="utf-8"))["output"]["recommendation"]
        for run in range(args.runs):
            store = LocalStore(str(directory / f"{name}-{run}"))
            payload = {"project_id": "prj_profile", "deploy_id": f"dep-{name}-{run}", "recommendation": rec}
            operations = [("gen", payload)]
            if fix_path:
                fix = json.loads((ROOT / fix_path).read_text(encoding="utf-8"))["payload"]
                operations.append(("fix", {**fix, "project_id": payload["project_id"], "deploy_id": payload["deploy_id"]}))
            for operation, request in operations:
                brain = ProfileBrain()
                start = time.perf_counter()
                output = getattr(terraform, operation)(request, brain, store)
                elapsed = time.perf_counter() - start
                targets = output["targets"] if operation == "gen" else [output]
                violations = [e for t in targets if t.get("files") for e in terraform.check_files(
                    [TfFile(name=k, content=v) for k, v in t["files"].items()], t["architecture"])[1]]
                row = {"case": name, "run": run, "operation": operation, "seconds": elapsed,
                       "status": output["status"], "violations": violations, "calls": brain.calls}
                rows.append(row)
                (directory / f"{name}-{run}-{operation}.json").write_text(
                    json.dumps({"measurement": row, "output": output}, ensure_ascii=False, indent=2), encoding="utf-8")
                (directory / "summary.json").write_text(json.dumps({"model": config.MODEL_ID,
                    "region": config.REGION, "account": account, "label": args.label,
                    "legacy": args.legacy, "cache": config.TF_CACHE_PROMPT if hasattr(config, "TF_CACHE_PROMPT") else False,
                    "system_chars": len(TF_SYSTEM_PROMPT),
                    "reference_chars": {a: len(terraform.reference(a)) for a in terraform.REFERENCES},
                    "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
