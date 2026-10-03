"""배포된 두 런타임에 같은 analyze 요청을 보내 시간·결과를 나란히 비교한다 (실제 AgentCore + Bedrock).

PR 을 머지하기 전에 테스트 런타임(scripts/deploy.py --name pawploy_agent_staging)에 올려서
지금 Main Server 가 쓰는 런타임과 비교하는 용도.

사용:
  AWS_PROFILE=peony .venv/Scripts/python scripts/compare_runtimes.py
  ... --base pawploy_agent --new pawploy_agent_staging --repeat 3
  ... --source s3://.../source/x/ --source s3://.../source/y/

--base/--new 는 런타임 이름 또는 ARN. 결과: evals/results/compare-<시각>/ (호출별 응답 + summary.md)
요청마다 새 세션이라 콜드 스타트까지 포함한 시간이다 (Main Server 와 같은 조건).
"""
import argparse
import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

REGION = "ap-northeast-2"
ROOT = Path(__file__).resolve().parent.parent
BUCKET = "pawploy-agent-135808950984"
DEFAULT_SOURCES = [f"s3://{BUCKET}/projects/prj_live/source/sample1/",   # 샘플 앱 (컨테이너 1개, scripts/demo.py 와 같음)
                   f"s3://{BUCKET}/projects/prj_swa/source/main/"]       # simple-web-app (컨테이너 여러 개)
PROJECT_ID = "prj_speedtest"            # 결과가 실제 프로젝트 경로와 섞이지 않게


def resolve_arn(name_or_arn: str) -> str:
    if name_or_arn.startswith("arn:"):
        return name_or_arn
    import boto3
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    for r in ctl.list_agent_runtimes().get("agentRuntimes", []):
        if r["agentRuntimeName"] == name_or_arn:
            return r["agentRuntimeArn"]
    raise SystemExit(f"런타임 {name_or_arn!r} 이 없습니다 (scripts/deploy.py --name {name_or_arn} 로 먼저 배포)")


def invoke(arn: str, payload: dict) -> tuple[float, dict]:
    import boto3
    from botocore.config import Config
    c = boto3.client("bedrock-agentcore", region_name=REGION, config=Config(read_timeout=900))
    t = time.time()
    try:
        r = c.invoke_agent_runtime(agentRuntimeArn=arn, runtimeSessionId=f"pawploy-cmp-{uuid.uuid4().hex}",
                                   payload=json.dumps(payload))
        out = json.loads(r["response"].read())
    except Exception as e:  # noqa: BLE001 — 비교표에 실패로 남긴다
        out = {"status": "error", "error": {"code": "invoke_failed", "message": f"{type(e).__name__}: {str(e)[:300]}"}}
    return time.time() - t, out


def brief(out: dict) -> dict:
    rec = out.get("recommendation") or {}
    bf = out.get("build_files") or {}
    return {"status": out.get("status"), "target": rec.get("target"), "port": rec.get("container_port"),
            "size": rec.get("size"), "supported": rec.get("supported"), "clues": len(rec.get("clues") or []),
            "secrets": sorted(rec.get("required_secrets") or []),
            "dockerfile": bool(bf.get("dockerfile") or bf.get("images")),
            "error": (out.get("error") or {}).get("code")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="pawploy_agent", help="기준 런타임 (이름 또는 ARN)")
    ap.add_argument("--new", default="pawploy_agent_staging", help="비교할 런타임 (이름 또는 ARN)")
    ap.add_argument("--source", action="append", help="analyze 할 source_uri (여러 번 가능, 기본: 샘플 앱·simple-web-app)")
    ap.add_argument("--repeat", type=int, default=2, help="소스마다 반복 횟수")
    args = ap.parse_args(argv)

    runtimes = {"base": resolve_arn(args.base), "new": resolve_arn(args.new)}
    sources = args.source or DEFAULT_SOURCES
    jobs = [(src, i, side) for src in sources for i in range(args.repeat) for side in runtimes]
    print(f"base={runtimes['base'].split('/')[-1]}  new={runtimes['new'].split('/')[-1]}  호출 {len(jobs)}번")

    def one(job):
        src, i, side = job
        sec, out = invoke(runtimes[side], {"mode": "analyze", "project_id": PROJECT_ID, "source_uri": src,
                                           "commit_sha": f"cmp-{side}-{i}"})
        print(f"  {side:<4} {sec:6.1f}s  {out.get('status')}  {src}", flush=True)
        return {"source": src, "run": i, "side": side, "seconds": round(sec, 1), **brief(out), "output": out}

    # 같은 소스·회차의 base/new 는 동시에 보낸다 (시간대에 따른 Bedrock 속도 차이를 줄인다)
    with ThreadPoolExecutor(max_workers=2) as ex:
        rows = list(ex.map(one, jobs))

    folder = ROOT / "evals" / "results" / f"compare-{datetime.now():%Y%m%d-%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# 런타임 비교 ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"- base: `{runtimes['base']}`", f"- new: `{runtimes['new']}`", "",
             "| 소스 | base 초 (중앙값) | new 초 (중앙값) | 줄어든 비율 | base 결과 | new 결과 | 결과 같음 |",
             "|---|---|---|---|---|---|---|"]
    keys = ("status", "target", "port", "size", "supported", "secrets", "dockerfile")
    for src in sources:
        by = {s: [r for r in rows if r["source"] == src and r["side"] == s] for s in runtimes}
        med = {s: statistics.median(r["seconds"] for r in by[s]) for s in runtimes}
        res = {s: {tuple(str(r[k]) for k in keys) for r in by[s]} for s in runtimes}
        show = {s: " / ".join(f"{r['status']} {r['target']}:{r['port']}" + (f" {r['error']}" if r["error"] else "")
                              for r in by[s]) for s in runtimes}
        cut = f"{(1 - med['new'] / med['base']) * 100:.0f}%" if med["base"] else "-"
        lines.append(f"| `{src.rstrip('/').rsplit('/', 3)[-3]}` | {med['base']:.1f} ({', '.join(str(r['seconds']) for r in by['base'])}) "
                     f"| {med['new']:.1f} ({', '.join(str(r['seconds']) for r in by['new'])}) | {cut} "
                     f"| {show['base']} | {show['new']} | {'예' if res['base'] == res['new'] else '**아니오**'} |")
    md = "\n".join(lines) + "\n"
    (folder / "summary.md").write_text(md, encoding="utf-8")
    print("\n" + md + f"저장: {folder}")
    return 1 if any(r["status"] != "ok" for r in rows if r["side"] == "new") else 0


if __name__ == "__main__":
    raise SystemExit(main())
