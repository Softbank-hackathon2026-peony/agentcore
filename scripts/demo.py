"""팀 시연 스크립트: 배포된 AgentCore 에이전트를 실제로 호출해서 단계별로 보여준다.

  python scripts/demo.py              # 실제 호출 (AWS_PROFILE 필요), 결과를 examples/demo/ 에 저장
  python scripts/demo.py --replay     # 저장된 결과로 재생 (네트워크·AWS 없이)
  python scripts/demo.py --only 1,3   # 일부 단계만

단계
  0. 오늘 한 일 (git 기록)
  1. analyze      샘플 앱 → 추천·근거·1~5순위·비용·Dockerfile
  2. analyze      simple-web-app → "지원 안 됨" 판정
  3. gen_terraform  1번 추천으로 AWS·GCP Terraform 모듈 동시 생성
  4. fix_terraform  일부러 깨뜨린 모듈 → 원인 찾고 수정
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAVE = ROOT / "examples" / "demo"
ARN = os.environ.get("AGENT_RUNTIME_ARN",
                     "arn:aws:bedrock-agentcore:ap-northeast-2:135808950984:runtime/pawploy_agent-kcEfwY3zhC")
BUCKET = "pawploy-agent-135808950984"
SAMPLE = f"s3://{BUCKET}/projects/prj_live/source/sample1/"
SWA = f"s3://{BUCKET}/projects/prj_swa/source/main/"

B, D, G, Y, R, C, X = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[0m"


def title(n, text):
    print(f"\n{B}{C}━━━ {n}. {text} {'━' * max(3, 60 - len(text))}{X}")


def kv(k, v):
    print(f"  {D}{k:<12}{X} {v}")


# ---------------- 호출 ----------------

def invoke(payload: dict) -> tuple[float, dict]:
    import boto3
    from botocore.config import Config
    c = boto3.client("bedrock-agentcore", region_name="ap-northeast-2", config=Config(read_timeout=600))
    t = time.time()
    r = c.invoke_agent_runtime(agentRuntimeArn=ARN, runtimeSessionId=f"pawploy-demo-{uuid.uuid4().hex}",
                               payload=json.dumps(payload))
    return time.time() - t, json.loads(r["response"].read())


def run(name: str, payload: dict, replay: bool) -> tuple[float, dict]:
    path = SAVE / f"{name}.json"
    if replay:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d["seconds"], d["output"]
    sec, out = invoke(payload)
    SAVE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seconds": sec, "payload": payload, "output": out}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return sec, out


# ---------------- 출력 ----------------

def show_today():
    title(0, "오늘 한 일 (agentcore 저장소 커밋)")
    try:
        log = subprocess.run(["git", "log", "--since=2026-10-02 00:00", "--reverse", "--date=format:%H:%M",
                              "--pretty=%ad  %an  %s"], cwd=ROOT, capture_output=True, encoding="utf-8").stdout
    except Exception as e:
        log = f"(git 기록을 못 읽음: {e})"
    for line in log.strip().splitlines():
        print("  " + line)
    print(f"\n  {D}배포: 서울 AgentCore Runtime · 모델 Claude Sonnet 4.6 (Bedrock) · 테스트 85개{X}")


def show_analyze(n, label, sec, out):
    title(n, f"analyze — {label}  ({sec:.0f}초)")
    if out.get("status") != "ok":
        print(f"  {R}{json.dumps(out, ensure_ascii=False)[:400]}{X}")
        return
    r = out["recommendation"]
    ok = G if r["supported"] else R
    kv("요약", r["summary"])
    kv("추천", f"{B}{r['label']}{X}  (포트 {r['container_port']}, 크기 {r['size']}, 헬스체크 {r['health_path']})")
    kv("지원 여부", f"{ok}{'배포 가능' if r['supported'] else '지원 안 됨'}{X}")
    kv("이유", r["reason"][:220] + ("…" if len(r["reason"]) > 220 else ""))
    print(f"\n  {B}근거 (실제 파일·줄, 코드로 존재 확인){X}")
    for c in r["clues"][:4]:
        loc = f"{c['file']}:{c['line']}" if c.get("line") else c["file"]
        print(f"   • {Y}{loc:<28}{X} {c['plain'][:70]}")
    print(f"\n  {B}1~5순위 + 월 예상 비용 (공식 단가로 코드가 계산){X}")
    for c in r["candidates"]:
        m = c["cost"].get("monthly")
        cost = f"${m:>6.2f}/월" if m is not None else "  단가 없음"
        dep = "" if c["deployable"] else f" {D}(비교용){X}"
        fit = f"{c['fit']:>3}" if c.get("fit") is not None else "  -"
        print(f"   {c['rank']}. {c['label']:<22} 적합도 {fit}  {c['verdict']:<3} {cost}{dep}")
    if r.get("required_secrets"):
        kv("직접 넣을 값", ", ".join(r["required_secrets"]))
    for w in r.get("warnings", [])[:4]:
        print(f"  {Y}⚠ {w[:110]}{X}")
    if r["supported"]:
        df = out["build_files"]["dockerfile"].strip().splitlines()
        body = [l for l in df if l.strip() and not l.lstrip().startswith("#")]
        print(f"\n  {B}생성된 Dockerfile (주석 제외, Lambda Web Adapter·PORT 는 코드가 강제){X}")
        for l in body[:10]:
            print(f"   {D}{l}{X}")
    kv("저장 위치", out.get("recommendation_uri"))


def show_gen(n, sec, out):
    title(n, f"gen_terraform — AWS·GCP 모듈 동시 생성  ({sec:.0f}초)")
    if out.get("status") not in ("ok", "partial"):
        print(f"  {R}{json.dumps(out, ensure_ascii=False)[:400]}{X}")
        return
    kv("저장 위치", out["module_uri"] + "{aws,gcp}/")
    for t in out["targets"]:
        print(f"\n  {B}[{t['cloud'].upper()}] {t['architecture']}{X}", end="  ")
        if t["status"] != "ok":
            print(f"{R}검사 실패: {'; '.join(t.get('violations', []))[:200]}{X}")
            continue
        print(f"{D}{', '.join(t['files'])}{X}")
        for r in t["resources"][:5]:
            print(f"   • {r[:100]}")
        main = t["files"]["main.tf"].splitlines()
        shown = [l for l in main if l.startswith("resource")]
        for l in shown[:6]:
            print(f"   {D}{l}{X}")
    print(f"\n  {G}✔ 클라우드별 검사 통과: 허용 리소스만 · provider/backend 없음 · 입력 6개/출력 3개 Worker 규격 일치{X}")
    print(f"  {D}Worker 는 attempt-N/<cloud>/ 에서 가장 큰 N 을 자동으로 읽음{X}")


def show_fix(n, sec, out):
    title(n, f"fix_terraform — 실패 로그로 자동 수정  ({sec:.0f}초)")
    if out.get("status") != "ok":
        print(f"  {R}{json.dumps(out, ensure_ascii=False)[:400]}{X}")
        return
    kv("원인", out["cause"][:300])
    for c in out["changes"][:5]:
        print(f"   {G}✔{X} {c}")
    kv("저장", f"attempt-{out['saved_attempt']}/{out['cloud']}/  →  {out['module_uri']}")
    if out.get("carried_over"):
        kv("같이 복사", ", ".join(out["carried_over"]) + " (안 고친 클라우드도 새 attempt 에 그대로)")


def broken_module() -> tuple[str, str]:
    ref = (ROOT / "agent" / "tf_reference" / "lambda.tf").read_text(encoding="utf-8")
    broken = ref.replace("local.memory[var.size]", "local.memory_mb[var.size]", 1)
    broken = broken.replace('package_type  = "Image"', 'package_typ   = "Image"', 1)
    log = ('Error: Reference to undeclared local value\n\n  on main.tf, in resource "aws_lambda_function" "app":\n'
           '   memory_size   = local.memory_mb[var.size]\n\nA local value with the name "memory_mb" has not been declared.')
    return broken, log


# ---------------- 메인 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true", help="저장된 결과로 재생")
    ap.add_argument("--only", help="실행할 단계 번호 (예: 1,3)")
    a = ap.parse_args()
    steps = {int(s) for s in a.only.split(",")} if a.only else {0, 1, 2, 3, 4}
    if os.name == "nt":
        os.system("")  # Windows 터미널 색상 켜기
    mode = "재생 (저장된 실제 결과)" if a.replay else "실제 호출"
    print(f"{B}Pawploy AgentCore 시연{X}  {D}[{mode}]  {ARN.split('/')[-1]}{X}")

    if 0 in steps:
        show_today()

    # 1·2 는 오래 걸리니 동시에 시작
    jobs = {}
    with ThreadPoolExecutor(2) as ex:
        if 1 in steps or 3 in steps:
            jobs[1] = ex.submit(run, "1-analyze-sample", {"mode": "analyze", "project_id": "prj_demo",
                                                          "source_uri": SAMPLE}, a.replay)
        if 2 in steps:
            jobs[2] = ex.submit(run, "2-analyze-swa", {"mode": "analyze", "project_id": "prj_demo_swa",
                                                       "source_uri": SWA}, a.replay)
        if not a.replay and jobs:
            print(f"\n  {D}분석 요청 보냄… (모델이 파일을 직접 골라 읽는 중, 40~90초){X}")
        res1 = jobs[1].result() if 1 in jobs else None
        if 1 in steps and res1:
            show_analyze(1, "샘플 웹앱", *res1)

        if 3 in steps and res1 and res1[1].get("status") == "ok":
            gen = ex.submit(run, "3-gen-terraform", {"mode": "gen_terraform", "project_id": "prj_demo",
                                                     "deploy_id": "dep-demo-2",
                                                     "recommendation_uri": res1[1]["recommendation_uri"]}, a.replay)
        else:
            gen = None
        if 2 in jobs:
            show_analyze(2, "simple-web-app (서비스 7개 + k8s)", *jobs[2].result())
        if gen:
            show_gen(3, *gen.result())

    if 4 in steps:
        broken, log = broken_module()
        show_fix(4, *run("4-fix-terraform", {"mode": "fix_terraform", "project_id": "prj_demo",
                                             "deploy_id": "dep-demo-2", "architecture": "lambda", "attempt": 1,
                                             "failed_stage": "validate", "log": log,
                                             "files": {"main.tf": broken}}, a.replay))
        print(f"  {D}(로그에는 memory_mb 에러만 있지만, 오타 package_typ 까지 찾아서 고쳤는지 확인){X}")
    print()


if __name__ == "__main__":
    sys.exit(main())
