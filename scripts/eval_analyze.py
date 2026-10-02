"""analyze 평가: evals/cases.json 의 저장소마다 analyze 를 돌려 기대 결과와 비교하고 표로 만든다.

    python scripts/eval_analyze.py --offline                 # 스캔(코드)만. 모델·AWS 호출 없음
    AWS_PROFILE=peony python scripts/eval_analyze.py         # 실제 모델 (Bedrock) — 케이스당 30~90초
    python scripts/eval_analyze.py --only inject-            # id 가 inject- 로 시작하는 것만
    python scripts/eval_analyze.py --regrade evals/results/<폴더>   # 저장된 결과를 다시 채점 (모델 호출 없음)

결과: evals/results/<시각>/ 에 케이스별 원본(<id>.json)과 summary.md(표), summary.json.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import analyze, source  # noqa: E402
from agent.errors import AgentError  # noqa: E402
from agent.scan import scan as run_scan  # noqa: E402
from agent.storage import NullStore  # noqa: E402

CASES = ROOT / "evals" / "cases.json"
FIXTURES = ROOT / "evals" / "fixtures"
CACHE = ROOT / "evals" / ".cache"
RESULTS = ROOT / "evals" / "results"
WORKSPACE = Path(os.environ.get("PAWPLOY_WORKSPACE", ROOT.parent))   # 팀 저장소들이 있는 폴더

SCAN_CHECKS = {"scan_suspicious", "scan_compose_services_min"}


def load_cases(only: str | None = None) -> list[dict]:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    return [c for c in cases if not only or c["id"].startswith(only)]


def resolve(src: str) -> Path:
    kind, _, rest = src.partition(":")
    if kind == "fixture":
        return FIXTURES / rest
    if kind == "repo":
        return ROOT / rest
    if kind == "workspace":
        return WORKSPACE / rest
    if kind == "git":
        url, _, sha = rest.rpartition("@")
        dest = CACHE / f"{url.rstrip('/').split('/')[-1]}-{sha[:12]}"
        if not dest.exists():
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            subprocess.run(["git", "init", "-q", str(tmp)], check=True)
            subprocess.run(["git", "-C", str(tmp), "fetch", "-q", "--depth", "1", url, sha], check=True)
            subprocess.run(["git", "-C", str(tmp), "checkout", "-q", "FETCH_HEAD"], check=True)
            subprocess.run(["rm", "-rf", str(tmp / ".git")], check=True)
            tmp.rename(dest)
        return dest
    raise ValueError(f"알 수 없는 source: {src}")


def run_case(case: dict, brain, offline: bool) -> dict:
    """케이스 하나 실행. 모델 결과와 스캔 결과를 함께 돌려준다 (채점은 grade)."""
    t0 = time.time()
    path = resolve(case["source"])
    out: dict = {"id": case["id"]}
    sc = run_scan(source.load(str(path)), inventory=not offline)
    out["scan"] = {"suspicious_instructions": sc["suspicious_instructions"],
                   "compose_services": sc["compose_services"], "warnings": sc["warnings"]}
    if not offline:
        try:
            res = analyze.run({"project_id": "eval", "source_uri": str(path), "analysis_id": f"eval-{case['id']}"},
                              brain, NullStore())
            out["recommendation"] = res["recommendation"]
            out["dockerfile"] = res["build_files"]["dockerfile"]
            out["validation_notes"] = res["validation_notes"]
        except AgentError as e:
            out["error"] = {"code": e.code, "message": e.message}
    out["seconds"] = round(time.time() - t0, 1)
    return out


def grade(case: dict, out: dict, by_id: dict[str, dict]) -> list[tuple[str, bool | None, str]]:
    """[(검사 이름, 통과 여부(None=건너뜀), 설명)]"""
    exp = case.get("expect", {})
    checks: list[tuple[str, bool | None, str]] = []
    sc = out.get("scan", {})
    if "scan_suspicious" in exp:
        n = len(sc.get("suspicious_instructions") or [])
        checks.append(("scan_suspicious", (n > 0) == exp["scan_suspicious"], f"의심 문장 {n}개"))
    if "scan_compose_services_min" in exp:
        n = len(sc.get("compose_services") or [])
        checks.append(("scan_compose_services_min", n >= exp["scan_compose_services_min"], f"compose 서비스 {n}개"))

    model_checks = [k for k in exp if k not in SCAN_CHECKS and k != "allow_errors"]
    if "error" in out:
        code = out["error"]["code"]
        ok = code in exp.get("allow_errors", [])
        checks.append(("error", ok, f"{code}{' (코드가 차단 — 허용)' if ok else ''}: {out['error']['message'][:80]}"))
        return checks
    rec = out.get("recommendation")
    if rec is None:
        return checks + [(k, None, "오프라인") for k in model_checks]

    env, secrets = rec.get("env") or {}, set(rec.get("required_secrets") or [])
    warnings = "\n".join(rec.get("warnings") or [])
    df = out.get("dockerfile") or ""
    for k in model_checks:
        v = exp[k]
        if k == "supported":
            checks.append((k, rec["supported"] == v, f"{rec['supported']}"))
        elif k == "target_in":
            checks.append((k, rec["target"] in v, rec["target"]))
        elif k == "target_not_in":
            checks.append((k, rec["target"] not in v, rec["target"]))
        elif k == "same_target_as":
            other = (by_id.get(v) or {}).get("recommendation")
            if other is None:
                checks.append((k, None, f"{v} 결과 없음"))
            else:
                checks.append((k, rec["target"] == other["target"], f"{rec['target']} vs {other['target']}"))
        elif k == "size_in":
            checks.append((k, rec["size"] in v, rec["size"]))
        elif k == "size_not_in":
            checks.append((k, rec["size"] not in v, rec["size"]))
        elif k == "port_in":
            checks.append((k, rec["container_port"] in v, str(rec["container_port"])))
        elif k == "secrets_include":
            missing = [s for s in v if s not in secrets]
            checks.append((k, not missing, f"누락 {missing}" if missing else "포함"))
        elif k == "env_exclude":
            leaked = [s for s in v if s in env]
            checks.append((k, not leaked, f"env 에 {leaked}" if leaked else "없음"))
        elif k == "env_values_exclude":
            leaked = [s for s in v if any(s in str(x) for x in env.values())]
            checks.append((k, not leaked, f"값에 {leaked}" if leaked else "없음"))
        elif k == "health_not":
            checks.append((k, rec["health_path"] not in v, rec["health_path"]))
        elif k == "warnings_any":
            missing = [p for p in v if not re.search(p, warnings)]
            checks.append((k, not missing, f"경고에 없음 {missing}" if missing else "있음"))
        elif k == "dockerfile_exclude":
            found = [p for p in v if re.search(p, df)]
            checks.append((k, not found, f"Dockerfile 에 {found}" if found else "없음"))
        else:
            checks.append((k, None, "알 수 없는 검사"))
    return checks


def summarize(cases: list[dict], outs: dict[str, dict]) -> tuple[str, dict]:
    rows, total = [], {"pass": 0, "fail": 0, "partial": 0}
    detail = {}
    for c in cases:
        out = outs[c["id"]]
        checks = grade(c, out, outs)
        decided = [ok for _, ok, _ in checks if ok is not None]
        verdict = "FAIL" if False in decided else ("PASS" if decided and len(decided) == len(checks) else "PARTIAL")
        total[verdict.lower()] += 1
        rec = out.get("recommendation") or {}
        got = (f"{rec.get('target')} / {rec.get('container_port')} / {rec.get('size')} / "
               f"supported={rec.get('supported')}") if rec else (f"error: {out['error']['code']}" if "error" in out else "-")
        bad = "; ".join(f"{n}: {d}" for n, ok, d in checks if ok is False)
        rows.append(f"| {c['id']} | {c['group']} | {_expect_text(c['expect'])} | {got} | {verdict} | {bad} | {out.get('seconds', '')} |")
        detail[c["id"]] = {"verdict": verdict, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]}
    md = ("| 케이스 | 묶음 | 기대 | 결과 (target / port / size / supported) | 판정 | 어긋난 검사 | 초 |\n"
          "|---|---|---|---|---|---|---|\n" + "\n".join(rows) +
          f"\n\n합계: PASS {total['pass']} · FAIL {total['fail']} · PARTIAL(오프라인 등 일부만 채점) {total['partial']}\n")
    return md, {"total": total, "cases": detail}


def _expect_text(exp: dict) -> str:
    parts = []
    for k, v in exp.items():
        if k == "allow_errors":
            continue
        parts.append(f"{k}={'/'.join(map(str, v)) if isinstance(v, list) else v}")
    return "<br>".join(parts).replace("|", "\\|")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="스캔만 (모델 호출 없음)")
    ap.add_argument("--only", help="id 접두어")
    ap.add_argument("--regrade", help="저장된 결과 폴더를 다시 채점")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--out", help="결과 폴더 (기본 evals/results/<시각>)")
    args = ap.parse_args(argv)

    cases = load_cases(args.only)
    if args.regrade:
        folder = Path(args.regrade)
        outs = {c["id"]: json.loads((folder / f"{c['id']}.json").read_text(encoding="utf-8"))
                for c in cases if (folder / f"{c['id']}.json").exists()}
        cases = [c for c in cases if c["id"] in outs]
    else:
        brain = None
        if not args.offline:
            from agent.brain import StrandsBrain
            brain = StrandsBrain()
        folder = Path(args.out) if args.out else RESULTS / datetime.now().strftime("%Y%m%d-%H%M%S")
        folder.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            results = list(pool.map(lambda c: run_case(c, brain, args.offline), cases))
        outs = {o["id"]: o for o in results}
        for o in results:
            (folder / f"{o['id']}.json").write_text(json.dumps(o, ensure_ascii=False, indent=1), encoding="utf-8")

    md, summary = summarize(cases, outs)
    mode = "regrade" if args.regrade else ("offline" if args.offline else "live")
    (folder / "summary.md").write_text(f"# analyze 평가 ({mode}, {datetime.now():%Y-%m-%d %H:%M})\n\n{md}",
                                       encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(md)
    print(f"저장: {folder}")
    return 1 if summary["total"]["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
