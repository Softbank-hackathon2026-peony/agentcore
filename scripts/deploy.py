"""AgentCore Runtime 배포 스크립트 (boto3로 필요한 것만 만든다).

만드는 것 (이미 있으면 재사용):
  1. S3 버킷  pawploy-agent-<계정ID>      : 코드 zip + 에이전트 결과물(projects/...) 저장
  2. IAM 역할 ppw-agentcore-runtime   : 런타임이 쓰는 권한 (Bedrock 호출, 위 버킷 읽기/쓰기, 로그)
  3. 코드 zip (arm64 리눅스용 의존성 포함) → s3://<버킷>/agent-code/<버전>.zip
  4. AgentCore Runtime pawploy_agent (없으면 생성, 있으면 새 버전으로 업데이트)

사용: AWS_PROFILE=peony .venv/Scripts/python scripts/deploy.py [--dry-run]
테스트 런타임 (Main Server 가 쓰는 pawploy_agent 는 그대로 두고 따로 올림):
      AWS_PROFILE=peony .venv/Scripts/python scripts/deploy.py --name pawploy_agent_staging
      [--env PAWPLOY_PRELOAD_CHARS=0]   # 런타임 환경변수 추가 (켜고 끄며 비교할 때)
버킷·역할은 같은 것을 쓴다. 비교: scripts/compare_runtimes.py
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3

REGION = "ap-northeast-2"
ROOT = Path(__file__).resolve().parent.parent
RUNTIME_NAME = "pawploy_agent"
ROLE_NAME = "ppw-agentcore-runtime"
PY_VERSION = "3.13"
RUNTIME = "PYTHON_3_13"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="AWS에 아무것도 만들지 않고 할 일만 출력")
    ap.add_argument("--name", default=RUNTIME_NAME, help="런타임 이름. 테스트용은 pawploy_agent_staging")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="런타임 환경변수 추가 (여러 번 가능)")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,47}", args.name):
        ap.error("런타임 이름은 영문자로 시작하고 영문·숫자·_ 만, 48자 이하")
    if any("=" not in kv for kv in args.env):
        ap.error("--env 는 KEY=VALUE 형식")
    extra_env = dict(kv.split("=", 1) for kv in args.env)

    sts = boto3.client("sts", region_name=REGION)
    account = sts.get_caller_identity()["Account"]
    bucket = f"pawploy-agent-{account}"
    print(f"[계정] {account}  [리전] {REGION}  [버킷] {bucket}  [역할] {ROLE_NAME}  [런타임] {args.name}"
          + (f"  [환경변수 추가] {extra_env}" if extra_env else ""))
    if args.dry_run:
        print("dry-run: 여기서 멈춤")
        return

    ensure_bucket(bucket)
    role_arn = ensure_role(account, bucket)
    version = time.strftime("%Y%m%d-%H%M%S")
    key = f"agent-code/{version}.zip"
    zip_path = build_zip()
    boto3.client("s3", region_name=REGION).upload_file(str(zip_path), bucket, key)
    print(f"[코드] s3://{bucket}/{key} ({zip_path.stat().st_size // 1024}KB)")
    arn = ensure_runtime(role_arn, bucket, key, args.name, extra_env)
    print(f"\n✅ 배포 완료\nAGENT_RUNTIME_ARN={arn}\nPAWPLOY_ARTIFACT_BUCKET={bucket}")


def ensure_bucket(bucket: str):
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"[버킷] 있음: {bucket}")
        return
    except s3.exceptions.ClientError:
        pass
    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    print(f"[버킷] 만듦: {bucket} (공개 차단)")


def ensure_role(account: str, bucket: str) -> str:
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "Bedrock", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/anthropic.*",
                      f"arn:aws:bedrock:*:{account}:inference-profile/*",
                      "arn:aws:bedrock:*::inference-profile/*"]},
        {"Sid": "ArtifactBucket", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                    "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws/bedrock-agentcore/*"},
        {"Sid": "Observability", "Effect": "Allow",
         "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "cloudwatch:PutMetricData"],
         "Resource": "*"},
    ]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print(f"[역할] 있음: {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Pawploy AgentCore runtime")["Role"]["Arn"]
        print(f"[역할] 만듦: {arn}")
        time.sleep(10)  # IAM 전파 대기
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="pawploy-agent", PolicyDocument=json.dumps(policy))
    return arn


def build_zip() -> Path:
    """agent/ 코드 + vendor/(InfraFit) + arm64 리눅스용 의존성을 zip으로."""
    work = Path(tempfile.mkdtemp(prefix="pawploy-agent-"))
    pkg = work / "package"
    print("[패키징] 의존성 설치 (linux arm64)")
    # pip은 --platform 을 줘도 의존성 조건(sys_platform)을 지금 OS 기준으로 판단해서 Windows에서 깨진다 → uv 사용
    subprocess.run([sys.executable, "-m", "uv", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt"),
                    "--target", str(pkg), "--python-platform", "aarch64-manylinux2014",
                    "--python-version", PY_VERSION, "--only-binary", ":all:"], check=True)
    shutil.copytree(ROOT / "agent", pkg / "agent", ignore=shutil.ignore_patterns("__pycache__"))
    # InfraFit (agent/inventory.py 가 별도 프로세스로 실행, PYTHONPATH=vendor/infrafit)
    shutil.copytree(ROOT / "vendor", pkg / "vendor", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "main.py", pkg / "main.py")
    zip_path = work / "agent.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pkg.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts:
                z.write(f, f.relative_to(pkg).as_posix())
    return zip_path


def ensure_runtime(role_arn: str, bucket: str, key: str, name: str = RUNTIME_NAME, extra_env: dict | None = None) -> str:
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    artifact = {"codeConfiguration": {"code": {"s3": {"bucket": bucket, "prefix": key}},
                                      "runtime": RUNTIME, "entryPoint": ["main.py"]}}
    env = {"PAWPLOY_ARTIFACT_BUCKET": bucket, "PAWPLOY_REGION": REGION, **(extra_env or {})}
    existing = next((r for r in ctl.list_agent_runtimes().get("agentRuntimes", [])
                     if r["agentRuntimeName"] == name), None)
    common = dict(agentRuntimeArtifact=artifact, roleArn=role_arn, environmentVariables=env,
                  networkConfiguration={"networkMode": "PUBLIC"})
    if existing:
        rid = existing["agentRuntimeId"]
        resp = ctl.update_agent_runtime(agentRuntimeId=rid, **common)
        print(f"[런타임] 업데이트: {rid}")
    else:
        resp = ctl.create_agent_runtime(agentRuntimeName=name, **common)
        rid = resp["agentRuntimeId"]
        print(f"[런타임] 만듦: {rid}")
    for _ in range(60):
        st = ctl.get_agent_runtime(agentRuntimeId=rid)
        if st["status"] in ("READY", "CREATE_FAILED", "UPDATE_FAILED"):
            print(f"[런타임] 상태: {st['status']} {st.get('failureReason', '')}")
            break
        time.sleep(5)
    return resp["agentRuntimeArn"]


if __name__ == "__main__":
    main()
