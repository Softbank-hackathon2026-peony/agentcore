"""CodeBuild 로 이미지 한 번 빌드 (Main Server 가 할 start_build 를 그대로 흉내).

  python scripts/build.py --analyze-response examples/demo/1-analyze-sample.json
  python scripts/build.py --source s3://.../source/ --build-files s3://.../build/<id>/attempt-1/ --tag prj-x-abc

끝나면 ECR_IMAGE_URI / GCP_IMAGE_URI (여러 컨테이너면 IMAGE_DIGESTS, 모두 digest 고정) 를 출력한다. 실패하면 로그 마지막 부분을 출력한다
(이 로그를 그대로 fix_build 의 build_log 로 넘기면 된다).
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import boto3

REGION = "ap-northeast-2"
PROJECT = "pawploy-build"


def read_buildspec(build_files_uri: str) -> str:
    """빌드 파일 폴더의 buildspec.yml. 컨테이너 1개면 프로젝트 기본값과 같고, 여러 개면 이미지 목록을 도는 템플릿이다."""
    bucket, _, prefix = build_files_uri[len("s3://"):].partition("/")
    body = boto3.client("s3", region_name=REGION).get_object(Bucket=bucket, Key=prefix + "buildspec.yml")["Body"]
    return body.read().decode("utf-8")


def start(source_uri: str, build_files_uri: str, image_tag: str, buildspec: str | None = None) -> str:
    cb = boto3.client("codebuild", region_name=REGION)
    env = [{"name": "SOURCE_URI", "value": source_uri, "type": "PLAINTEXT"},
           {"name": "BUILD_FILES_URI", "value": build_files_uri, "type": "PLAINTEXT"},
           {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"}]
    # 프로젝트에 박힌 buildspec 은 컨테이너 1개용이라, 항상 그 앱의 buildspec 으로 덮어쓴다
    return cb.start_build(projectName=PROJECT, environmentVariablesOverride=env,
                          buildspecOverride=buildspec if buildspec is not None else read_buildspec(build_files_uri))["build"]["id"]


def wait(build_id: str) -> dict:
    cb = boto3.client("codebuild", region_name=REGION)
    last = None
    while True:
        b = cb.batch_get_builds(ids=[build_id])["builds"][0]
        if b["currentPhase"] != last:
            last = b["currentPhase"]
            print(f"  {time.strftime('%H:%M:%S')} {last}", flush=True)
        if b["buildComplete"]:
            return b
        time.sleep(5)


def log_tail(b: dict, n: int = 60) -> str:
    lg = b.get("logs") or {}
    if not lg.get("groupName") or not lg.get("streamName"):
        return ""
    ev = boto3.client("logs", region_name=REGION).get_log_events(
        logGroupName=lg["groupName"], logStreamName=lg["streamName"], limit=n, startFromHead=False)["events"]
    return "".join(e["message"] for e in ev)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyze-response", help="analyze 응답 JSON (scripts/demo.py 저장본 형식도 됨)")
    ap.add_argument("--source")
    ap.add_argument("--build-files")
    ap.add_argument("--tag")
    ap.add_argument("--buildspec", help="로컬 buildspec override (프로젝트 설정·S3 변경 없이 비교)")
    ap.add_argument("--result", help="phase 타임스탬프·build ID를 저장할 로컬 JSON 파일")
    a = ap.parse_args()
    if a.analyze_response:
        d = json.load(open(a.analyze_response, encoding="utf-8"))
        out, payload = d.get("output", d), d.get("payload", {})
        source = a.source or payload.get("source_uri")
        build_files = out["build_files"]["uri_prefix"]
        tag = a.tag or f"{out['project_id']}-{out['analysis_id']}"
    else:
        source, build_files, tag = a.source, a.build_files, a.tag
    if not (source and build_files and tag):
        sys.exit("--source, --build-files, --tag 가 필요합니다")
    tag = re.sub(r"[^A-Za-z0-9_.-]", "-", tag)[:128]

    print(f"[빌드] {PROJECT}  tag={tag}\n  source={source}\n  build_files={build_files}")
    t = time.time()
    override = Path(a.buildspec).read_text(encoding="utf-8") if a.buildspec else None
    build_id = start(source, build_files, tag, override)
    print(f"  build_id={build_id}", flush=True)
    b = wait(build_id)
    if a.result:
        Path(a.result).write_text(json.dumps(b, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in b.get("phases", []):
        if p.get("endTime") and p.get("startTime"):
            print(f"  {p['phaseType']}: {(p['endTime'] - p['startTime']).total_seconds():.3f}s")
    print(f"[결과] {b['buildStatus']} ({time.time() - t:.0f}초)")
    exported = {v["name"]: v.get("value") for v in b.get("exportedEnvironmentVariables") or []}
    for k, v in exported.items():
        print(f"  {k}={v}")
    if b["buildStatus"] != "SUCCEEDED":
        failed = next((p for p in b.get("phases", []) if p.get("phaseStatus") == "FAILED"), {})
        print(f"[실패 단계] {failed.get('phaseType')}  {failed.get('contexts')}")
        print("---- 로그 마지막 부분 ----\n" + log_tail(b))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
